"""Campus Customs vendor desk agent: one vendor document in, one structured recommendation out.

Usage:
    python agent.py --doc data/practice_docs/p01_bluehen_quote.html                    # fully defended (default)
    python agent.py --doc data/practice_docs/p01_bluehen_quote.html --defense none     # original Problem 3 agent
    python agent.py --doc data/practice_docs/p05_maplemills_quote.pdf --parse-only     # no AI call, no logs
    python agent.py --doc data/practice_docs/p05_maplemills_quote.pdf --parse-only --defense code   # sanitized text

Pipeline for one run:
    1. Parse (code, no AI): tools.extract_document (baseline) or defenses.sanitize_document (code layer).
    2. One PydanticAI agent run on gpt-6-luna through Portkey. The system prompt is the prompt file plus the full
       handbook, sent on every call. Output is validated against models.VendorDeskResult.
    3. Design layer only: code grounds the facts, applies the handbook checks itself and cleans the answer.
    4. Evidence goes to output/usage_log.json (raw API usage + cost from data/prices.json) and
       output/audit_trail.json (document text sent, tool calls, final response, security report).

Defense layers (--defense; see defenses.py and output/harness.md "Safety"):
    none          the original Problem 3 agent: raw parser, prompts/prompt_baseline.md, ungated decoy tools
    code          + visibility-aware sanitizer
    code+design   + untrusted-data envelope, gated tools (human approval), policy engine, grounding, output cleaning
    full          + defended prompt (prompts/prompt.md)
    design, prompt  single layers, for effectiveness tests

Cache modes (--cache), all with the same request layout:
    provider_default  no cache settings (the default)
    handbook / off    OpenAI's explicit cache controls; our Portkey-to-Azure route ignores them (Problem 4)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # keep terminal output to the result

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic_ai import Agent, NativeOutput, UsageLimits, capture_run_messages
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

import defenses
from defenses import PRESETS, DefenseConfig
from models import AgentDeps, PurchasingChecks, SecurityReport, VendorDeskResult, VendorDocument, consistency_flags
from tools import (
    DECOY_TOOLS, FORMATS, OUTPUT, ROOT, TRAP_CALLS, append_json, ensure_json_array, extract_document,
    read_json_array, utc_now,
)  # fmt: skip

MODEL_NAME = "gpt-6-luna"  # the only model allowed in HW6
PORTKEY_BASE_URL = "https://api.portkey.ai/v1"
BASE_SETTINGS = OpenAIResponsesModelSettings(openai_reasoning_effort="medium")  # the model's documented default
CACHE_MODES: dict[str, dict] = {
    "provider_default": {},
    "handbook": {"openai_cache_instructions": True, "openai_prompt_cache_options": {"mode": "explicit", "ttl": "30m"}},
    "off": {"openai_prompt_cache_options": {"mode": "explicit", "ttl": "30m"}},
}
LIMITS = UsageLimits(request_limit=6)  # normal run = 1 request; caps retry and tool loops, never blocks a tool call

PROMPT_FILE = ROOT / "prompts" / "prompt.md"  # defended prompt (prompt layer)
PROMPT_BASELINE = ROOT / "prompts" / "prompt_baseline.md"  # the original Problem 3 prompt, unchanged
HANDBOOK_FILE = ROOT / "data" / "shop_handbook.md"
PRICES_FILE = ROOT / "data" / "prices.json"
USAGE_LOG = OUTPUT / "usage_log.json"
AUDIT_TRAIL = OUTPUT / "audit_trail.json"
CODE_FILES = (ROOT / "agent.py", ROOT / "tools.py", ROOT / "models.py", ROOT / "defenses.py", PROMPT_FILE, PROMPT_BASELINE)
DECISION_FIELDS = ("vendor", "sku", "unit_price", "lead_time_days", "min_order")
MILLION = Decimal(1_000_000)


class EvidenceResponsesModel(OpenAIResponsesModel):
    """OpenAIResponsesModel that also keeps each API response's id, model, full `usage` object and prompt-cache
    diagnostics, exactly as the API returned them (PydanticAI's normalized usage drops some nested fields).

    Hooks the non-streaming path only, which is the one agent.run_sync uses.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_responses: list[dict] = []

    def _process_response(self, response, model_settings, model_request_parameters):
        if not isinstance(response, str):  # a non-JSON body is left to PydanticAI's own error handling
            diagnostics = getattr(response, "prompt_cache_diagnostics", None)
            self.api_responses.append(
                {
                    "response_id": response.id,
                    "model": response.model,
                    "usage": response.usage.model_dump(mode="json") if response.usage else None,
                    "prompt_cache_diagnostics": diagnostics.model_dump(mode="json") if diagnostics else None,
                }
            )
        return super()._process_response(response, model_settings, model_request_parameters)


def load_api_key() -> str:
    """PORTKEY_API_KEY from the environment or the nearest .env in this folder or a parent (never printed)."""
    for folder in [ROOT, *ROOT.parents]:
        if (folder / ".env").exists():
            load_dotenv(folder / ".env", override=False)
            break
    key = os.getenv("PORTKEY_API_KEY", "").strip()
    if not key:
        raise SystemExit("PORTKEY_API_KEY is not set. Put it in a .env file here or in a parent folder.")
    return key


def build_model(api_key: str) -> EvidenceResponsesModel:
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=PORTKEY_BASE_URL,
        # Portkey keeps a response cache: without force-refresh an identical request gets an earlier
        # response replayed (same response id) and never reaches the model. Every run must be a real call.
        default_headers={"x-portkey-provider": "openai", "x-portkey-cache-force-refresh": "true"},
        timeout=120.0,
        max_retries=2,
    )
    return EvidenceResponsesModel(MODEL_NAME, provider=OpenAIProvider(openai_client=client))


def system_prompt_text(cache_scope: str | None = None, defended_prompt: bool = False) -> str:
    # read_text turns the handbook's CRLF line endings into LF, the text Problem 2 counted (10,058 tokens)
    prompt = (PROMPT_FILE if defended_prompt else PROMPT_BASELINE).read_text(encoding="utf-8").strip()
    text = prompt + "\n\n" + HANDBOOK_FILE.read_text(encoding="utf-8")
    # Optional first line for cache experiments (Problem 4): a cached prefix must match from the first token,
    # so requests with different scope lines can never reuse each other's cache entries.
    return f"[request group: {cache_scope}]\n\n{text}" if cache_scope else text


def build_agent(model: OpenAIResponsesModel, cache_scope: str | None = None,
                config: DefenseConfig = PRESETS["none"]) -> Agent[AgentDeps, VendorDeskResult]:
    # A static system prompt (not top-level `instructions`) so every cache mode sends the same layout:
    # the handbook as a leading input message, which is where an explicit breakpoint can go.
    # Design layer: the model gets gated tools with the same names and schemas as the decoys.
    return Agent(
        model,
        output_type=NativeOutput(VendorDeskResult, name="vendor_desk_result", strict=True),
        system_prompt=system_prompt_text(cache_scope, defended_prompt=config.prompt),
        deps_type=AgentDeps,
        tools=defenses.GATED_TOOLS if config.design else DECOY_TOOLS,
        retries=1,
    )


def model_settings(cache: str, comparison_response_id: str | None = None) -> OpenAIResponsesModelSettings:
    settings = {**BASE_SETTINGS, **CACHE_MODES[cache]}
    if comparison_response_id:  # asks OpenAI for free cache diagnostics against that earlier response
        options = dict(settings.get("openai_prompt_cache_options") or {})
        options["comparison_response_id"] = comparison_response_id
        settings["openai_prompt_cache_options"] = options
    return OpenAIResponsesModelSettings(**settings)


def user_prompt(doc: VendorDocument, config: DefenseConfig = PRESETS["none"], nonce: str | None = None) -> str:
    # Only the format: practice file names carry vendor hints that are not document facts.
    if config.design:  # labeled, delimited untrusted data with a per-run nonce
        return defenses.envelope(doc, nonce or uuid.uuid4().hex[:12])
    return f"Vendor document ({doc.format}):\n\n{doc.text}"


_ENGINE: defenses.PolicyEngine | None = None


def policy_engine() -> defenses.PolicyEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = defenses.PolicyEngine(HANDBOOK_FILE.read_text(encoding="utf-8"))
    return _ENGINE


def apply_design(output: VendorDeskResult, doc_text: str, report: SecurityReport) -> VendorDeskResult:
    """Design layer, after the model: ground the facts, let code apply the handbook checks, clean the answer."""
    grounding = defenses.ground(output, doc_text)
    report.grounding = grounding
    ungrounded = [f for f, ok in grounding.items() if not ok]
    update = {f: None for f in ungrounded}
    if "sku" in ungrounded:
        update["sku_source"] = None
    if "lead_time_days" in ungrounded:
        update["lead_time_basis"] = None
    facts = {f: update.get(f, getattr(output, f)) for f in DECISION_FIELDS}
    checks, decision, policy_reason = policy_engine().decide(
        facts["vendor"], facts["sku"], facts["unit_price"], facts["lead_time_days"], facts["min_order"])
    cleaned, notes = defenses.clean_output(output, doc_text)
    report.model_decision, report.policy_decision, report.policy_checks = output.decision, decision, checks
    report.output_cleaning = notes
    if decision != output.decision or ungrounded:
        report.decision_source = "policy_engine"
        reason = policy_reason
    else:
        reason = cleaned["reason"]
    update.update(checks=PurchasingChecks(**checks), decision=decision, reason=reason, evidence=cleaned["evidence"])
    return VendorDeskResult.model_validate({**output.model_dump(), **update})


def code_version() -> str:
    h = hashlib.sha256()
    for f in CODE_FILES:
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


# ---------- evidence ----------


def load_prices() -> dict[str, Decimal]:
    return json.loads(PRICES_FILE.read_text(encoding="utf-8"), parse_float=Decimal)[MODEL_NAME]


def token_counts(usage: dict) -> dict[str, int]:
    """The billing-relevant counts from one raw Responses API usage object."""
    inp = usage.get("input_tokens") or 0
    in_details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    cached = in_details.get("cached_tokens") or 0
    written = in_details.get("cache_write_tokens") or 0
    return {
        "input_tokens": inp,
        "cached_tokens": cached,
        "cache_write_tokens": written,
        "uncached_input_tokens": inp - cached - written,
        "output_tokens": usage.get("output_tokens") or 0,
        "reasoning_tokens": out_details.get("reasoning_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
    }


def cost_usd(counts: dict[str, int], prices: dict[str, Decimal]) -> dict[str, float]:
    """Dollar cost from data/prices.json. Reasoning tokens are already inside output_tokens."""
    parts = {
        "uncached_input": counts["uncached_input_tokens"] * prices["input_per_1m"],
        "cached_input": counts["cached_tokens"] * prices["cached_input_per_1m"],
        "cache_write": counts["cache_write_tokens"] * prices["cache_write_per_1m"],
        "output": counts["output_tokens"] * prices["output_per_1m"],
    }
    parts = {k: v / MILLION for k, v in parts.items()}
    parts["total"] = sum(parts.values())
    return {k: float(v) for k, v in parts.items()}


@dataclass
class RunOutcome:
    run_id: str
    timestamp_utc: str
    cache: str
    doc: VendorDocument
    output: VendorDeskResult | None
    error: str | None
    api_responses: list[dict]
    usage: dict = field(default_factory=dict)
    audit: dict = field(default_factory=dict)
    decoy_calls_logged: int = 0
    cache_scope: str | None = None
    defense: str = "none"
    model_output: VendorDeskResult | None = None  # the model's own answer, before design-layer processing
    security: SecurityReport | None = None
    blocked_actions_logged: int = 0


def usage_record(o: RunOutcome, prices: dict[str, Decimal], run_usage, version: str) -> dict:
    requests, totals, missing = [], {}, False
    earlier_ids = {r["response_id"] for e in read_json_array(USAGE_LOG) for r in e.get("api_requests", [])}
    for resp in o.api_responses:
        if resp["usage"] is None:  # not reported: no counts, no cost, never a measured zero
            missing = True
            requests.append({**resp, "cost_usd": None})
            continue
        counts = token_counts(resp["usage"])
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v
        requests.append({**resp, "cost_usd": cost_usd(counts, prices)})
    return {
        "run_id": o.run_id,
        "timestamp_utc": o.timestamp_utc,
        "document": o.doc.path,
        "document_sha256": o.doc.sha256,
        "cache_mode": o.cache,
        "cache_scope": o.cache_scope,
        "defense": o.defense,
        "agent_code_version": version,
        "model_requested": MODEL_NAME,
        "models_reported_by_api": sorted({r["model"] for r in o.api_responses}),
        "api_requests": requests,
        # a response id seen in an earlier run means a gateway replay, not a new model call
        "replayed_response_ids": [r["response_id"] for r in o.api_responses if r["response_id"] in earlier_ids],
        "usage_missing_on_some_request": missing,
        "totals": totals or None,
        "cost_usd": cost_usd(totals, prices) if totals else None,
        "prices_per_1m_usd": {k: float(v) for k, v in prices.items()},
        "pydantic_ai_run_usage": _plain(run_usage) if run_usage is not None else None,
        "error": o.error,
    }


def audit_record(o: RunOutcome, messages, version: str) -> dict:
    calls, returns, tool_retries, output_retries, raw_texts = [], {}, [], [], []
    for msg in messages:
        for part in msg.parts:
            if isinstance(msg, ModelResponse) and isinstance(part, ToolCallPart):
                calls.append(part)
            elif isinstance(msg, ModelRequest) and isinstance(part, ToolReturnPart):
                returns[part.tool_call_id] = part
            elif isinstance(msg, ModelRequest) and isinstance(part, RetryPromptPart):
                # a retry tied to a tool name is about a tool call (bad arguments, unknown tool); otherwise the output
                target = tool_retries if part.tool_name else output_retries
                target.append(f"{part.tool_name + ': ' if part.tool_name else ''}{str(part.content)[:1000]}")
        if isinstance(msg, ModelResponse):
            raw_texts += [p.content[:2000] for p in msg.parts if getattr(p, "part_kind", "") == "text"]
    tool_calls = [
        {
            "tool": c.tool_name,
            "arguments": c.args_as_dict(),
            "result": str(returns[c.tool_call_id].content)[:200] if c.tool_call_id in returns else None,
        }
        for c in calls
    ]
    flat_text = " ".join(o.doc.text.split()).casefold()
    evidence_check = [
        {"snippet": s, "found_in_document_text": " ".join(s.split()).casefold() in flat_text}
        for s in (o.output.evidence if o.output else [])
    ]
    parser = "defenses.sanitize_document (code layer)" if o.security and o.security.layers["code"] else "tools.extract_document"
    steps = [
        {"step": "extract_document", "by": f"code ({parser}), not the model",
         "result": f"{o.doc.format}, {o.doc.char_count} characters"
                   + (f"; {len(o.security.quarantined)} item(s) withheld" if o.security and o.security.layers["code"] else "")},
        {"step": "agent_run", "by": f"{MODEL_NAME} via PydanticAI",
         "result": f"{sum(isinstance(m, ModelResponse) for m in messages)} model response(s)"},
    ]
    if o.security and o.security.layers["design"]:
        steps.append({"step": "design_layer", "by": "code (grounding, policy engine, output cleaning)",
                      "result": f"decision from {o.security.decision_source}"})
    return {
        "run_id": o.run_id,
        "timestamp_utc": o.timestamp_utc,
        "cache_mode": o.cache,
        "cache_scope": o.cache_scope,
        "defense": o.defense,
        "agent_code_version": version,
        "document": o.doc.model_dump(exclude={"text"}),
        "document_text_sent_to_model": o.doc.text,
        "steps": steps,
        "tool_calls": tool_calls,
        "decoy_calls_attempted": sum(c["tool"] in {"send_email", "approve_payment"} for c in tool_calls),
        "decoy_calls_logged": o.decoy_calls_logged,
        "blocked_actions_logged": o.blocked_actions_logged,
        "tool_call_retries": tool_retries,
        "output_validation_retries": output_retries,
        "final_response": o.output.model_dump(mode="json") if o.output else None,
        "model_response_before_design_layer": (o.model_output.model_dump(mode="json")
                                               if o.model_output is not None and o.model_output != o.output else None),
        "consistency_flags": consistency_flags(o.output) if o.output else [],
        "evidence_check": evidence_check,
        "security": o.security.model_dump(mode="json") if o.security else None,
        "raw_model_text": raw_texts if o.output is None else [],
        "error": o.error,
    }


def _plain(obj):
    """Dataclass usage objects -> JSON-friendly dict."""
    data = {k: getattr(obj, k) for k in getattr(obj, "__dataclass_fields__", {})}
    return {k: v for k, v in data.items() if isinstance(v, (int, float, str, dict)) and v not in ({}, None)}


# ---------- one run ----------


def run_document(
    doc_path: str | Path,
    cache: str = "provider_default",
    comparison_response_id: str | None = None,
    cache_scope: str | None = None,
    defense: str = "full",
) -> RunOutcome:
    """Parse, run the agent once, append usage + audit evidence. Never raises for model/API errors."""
    if cache not in CACHE_MODES:
        raise ValueError(f"cache must be one of {sorted(CACHE_MODES)}")
    if defense not in PRESETS:
        raise ValueError(f"defense must be one of {list(PRESETS)}")
    config = PRESETS[defense]
    for log in (TRAP_CALLS, USAGE_LOG, AUDIT_TRAIL, defenses.BLOCKED_ACTIONS):  # fail before the billed call
        ensure_json_array(log)
        read_json_array(log)
    prices, version = load_prices(), code_version()
    report = SecurityReport(defense=defense, layers=config.layers())
    if config.code:  # code layer: only what a person would see reaches the model
        sanitized = defenses.sanitize_document(doc_path)
        doc = sanitized.document
        report.quarantined = sanitized.quarantined
        report.invisible_characters_removed = sanitized.invisible_characters_removed
        report.injection_indicators = sanitized.injection_indicators
        report.review_reasons += defenses.review_reasons(sanitized)
    else:
        doc = extract_document(doc_path)
    model = build_model(load_api_key())
    agent = build_agent(model, cache_scope, config)
    traps_before = len(read_json_array(TRAP_CALLS))
    blocked_before = len(read_json_array(defenses.BLOCKED_ACTIONS))

    o = RunOutcome(uuid.uuid4().hex[:12], utc_now(), cache, doc, None, None, model.api_responses,
                   cache_scope=cache_scope, defense=defense, security=report)
    result = None
    with capture_run_messages() as messages:
        try:
            result = agent.run_sync(
                user_prompt(doc, config, nonce=o.run_id),
                deps=AgentDeps(run_id=o.run_id, document=doc),
                model_settings=model_settings(cache, comparison_response_id),
                usage_limits=LIMITS,
            )
        except Exception as exc:  # record the failure as evidence, then report it
            o.error = f"{type(exc).__name__}: {exc}"
    if result is not None:
        messages, o.output = result.all_messages(), result.output
        o.model_output = result.output
    o.decoy_calls_logged = len(read_json_array(TRAP_CALLS)) - traps_before
    blocked = read_json_array(defenses.BLOCKED_ACTIONS)[blocked_before:]
    o.blocked_actions_logged = len(blocked)
    report.blocked_actions = [{"tool": b["tool"], "arguments": b["arguments"]} for b in blocked if b["run_id"] == o.run_id]
    if report.blocked_actions:
        report.review_reasons.append(f"{len(report.blocked_actions)} tool call(s) blocked by the authorization gate")
    if config.design and o.output is not None:
        try:
            o.output = apply_design(o.output, doc.text, report)
        except Exception as exc:  # the design layer must never silently pass an unchecked answer
            o.error = f"design layer failed: {type(exc).__name__}: {exc}"
            o.output = None
        else:
            ungrounded = [f for f, ok in report.grounding.items() if not ok]
            if ungrounded:
                report.review_reasons.append(f"fact(s) not found in the document text: {', '.join(ungrounded)}")
            if report.model_decision != report.policy_decision:
                report.review_reasons.append(f"model said {report.model_decision}, policy engine said {report.policy_decision}")
            report.review_reasons += report.output_cleaning
    if o.model_output is not None:
        report.review_reasons += [f"model answer: {f}" for f in consistency_flags(o.model_output)]
    report.needs_human_review = bool(report.review_reasons)

    o.usage = usage_record(o, prices, result.usage if result is not None else None, version)
    o.audit = audit_record(o, messages, version)
    log_errors = []
    for path, record in ((USAGE_LOG, o.usage), (AUDIT_TRAIL, o.audit)):
        try:
            append_json(path, record)
        except Exception as exc:  # one broken log must not lose the other
            log_errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
    if log_errors:
        o.error = "; ".join(filter(None, [o.error, *log_errors]))
    return o


def print_outcome(o: RunOutcome) -> None:
    print(json.dumps(o.output.model_dump(mode="json") if o.output else {"error": o.error}, indent=2, ensure_ascii=False))
    if o.usage.get("totals"):
        t, c = o.usage["totals"], o.usage["cost_usd"]
        print(
            f"\nrun {o.run_id} | cache {o.cache} | model {', '.join(o.usage['models_reported_by_api'])} | "
            f"{len(o.usage['api_requests'])} request(s) | input {t['input_tokens']:,} "
            f"(cached {t['cached_tokens']:,}, cache write {t['cache_write_tokens']:,}) | "
            f"output {t['output_tokens']:,} (reasoning {t['reasoning_tokens']:,}) | cost ${c['total']:.8f}"
        )
    print(
        f"tool calls: {len(o.audit['tool_calls'])} | decoy calls attempted: {o.audit['decoy_calls_attempted']}, "
        f"logged: {o.decoy_calls_logged} | consistency flags: {len(o.audit['consistency_flags'])}"
    )
    if o.usage.get("usage_missing_on_some_request"):
        print("note: usage was missing on at least one request; the totals above cover reported requests only")
    if o.usage.get("replayed_response_ids"):
        print(f"warning: replayed response id(s) {o.usage['replayed_response_ids']}: not a new model call")
    s = o.security
    if s is not None and o.defense != "none":
        print(f"defense {o.defense} | withheld {len(s.quarantined)} item(s) | blocked actions {len(s.blocked_actions)} | "
              f"decision from {s.decision_source} | needs human review: {'yes' if s.needs_human_review else 'no'}")
        for r in s.review_reasons:
            print(f"  review: {r}")
    if o.error:
        print(f"error: {o.error}", file=sys.stderr)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Campus Customs vendor desk agent")
    parser.add_argument("--doc", required=True, help="Path to one vendor document (.html, .pdf or .eml)")
    parser.add_argument("--defense", choices=list(PRESETS), default="full",
                        help="Defense layers (default full; 'none' is the original Problem 3 agent)")
    parser.add_argument("--cache", choices=sorted(CACHE_MODES), default="provider_default", help="Prompt cache mode")
    parser.add_argument("--parse-only", action="store_true", help="Print the extracted text; no AI call, no logs")
    args = parser.parse_args()
    doc_path = Path(args.doc)
    if not doc_path.exists() and (ROOT / doc_path).exists():
        doc_path = ROOT / doc_path
    if not doc_path.is_file():
        parser.error(f"document not found: {args.doc}")
    if doc_path.suffix.lower() not in FORMATS:
        parser.error(f"unsupported document type {doc_path.suffix!r}; expected one of {sorted(FORMATS)}")
    if args.parse_only:
        if PRESETS[args.defense].code:
            sanitized = defenses.sanitize_document(doc_path)
            doc = sanitized.document
            print(json.dumps({**doc.model_dump(exclude={"text"}),
                              "withheld": [q.model_dump() for q in sanitized.quarantined],
                              "invisible_characters_removed": sanitized.invisible_characters_removed,
                              "injection_indicators": sanitized.injection_indicators}, indent=2, ensure_ascii=False))
        else:
            doc = extract_document(doc_path)
            print(json.dumps(doc.model_dump(exclude={"text"}), indent=2))
        print("\n" + doc.text)
        return 0
    outcome = run_document(doc_path, args.cache, defense=args.defense)
    print_outcome(outcome)
    return 1 if outcome.error else 0


if __name__ == "__main__":
    sys.exit(main())
