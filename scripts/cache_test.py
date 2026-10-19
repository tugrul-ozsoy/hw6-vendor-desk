"""Problem 4: measure what prompt caching saves on the 12 practice documents.

    python scripts/cache_test.py run      # 24 real agent runs: 12 with_cache, then 12 without_cache
    python scripts/cache_test.py report   # recompute everything from output/cache_test.json and print it
    python scripts/cache_test.py check    # report, then confirm every table in output/cache_test.md matches

Our route (Portkey -> Azure OpenAI deployment "gpt-6-luna-global") ignores OpenAI's prompt_cache_options,
explicit breakpoints and cache diagnostics, and always caches implicitly (attempt 1 and the diagnostic probes
in cache_test.json show this). So the two setups control reuse through the prefix itself. A cached prefix must
match from the first token, so each request starts with a "[request group: ...]" line:

    with_cache     the same group line on all 12 calls (new for each batch): the handbook prefix can be reused
    without_cache  a unique group line on every call: no earlier cache entry can match, so nothing is reused

Everything else is identical: agent, model, prompt, handbook, documents, schema, tools and settings.
"""

from __future__ import annotations

import json
import sys
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tiktoken  # noqa: E402

from agent import MODEL_NAME, code_version, cost_usd, load_prices, run_document, system_prompt_text, token_counts  # noqa: E402
from tools import OUTPUT, ROOT, append_json, read_json_array  # noqa: E402

CACHE_TEST = OUTPUT / "cache_test.json"
CACHE_MD = OUTPUT / "cache_test.md"
DOCS = sorted((ROOT / "data" / "practice_docs").iterdir())
SETUP_ORDER = ("with_cache", "without_cache")
DOCS_PER_DAY, DAYS = 1000, 30
MILLION = Decimal(1_000_000)
TOKEN_KEYS = ("input_tokens", "cached_tokens", "cache_write_tokens", "uncached_input_tokens", "output_tokens",
              "reasoning_tokens", "total_tokens")  # fmt: skip
COST_KEYS = ("uncached_input", "cached_input", "cache_write", "output", "total")
# o200k_base tokens of the system prompt (scope line + prompt.md + handbook) as sent, per agent code version.
# The batch was measured with 70412954419e6569; prompt.md changed afterwards, so that count is pinned here.
SYSTEM_PROMPT_TOKENS = {"70412954419e6569": 10928}
ESTIMATE = {  # output/handbook_estimate.md (Problem 2), handbook tokens only
    "handbook_tokens": 10058,
    "per_call_uncached": Decimal("0.0010058"),
    "per_call_cold": Decimal("0.00125725"),
    "per_call_warm": Decimal("0.00010058"),
    "calls_1000_no_cache": Decimal("1.0058"),
    "calls_1000_cache": Decimal("0.10173667"),
}


def scope_for(setup: str, batch: str, seq: int) -> str:
    return f"p4-{batch}-shared" if setup == "with_cache" else f"p4-{batch}-{seq:02d}-{uuid.uuid4().hex[:8]}"


def classify(setup: str, usage: dict | None) -> tuple[str, bool]:
    """Cache evidence class for one API call, and whether the call is valid for its setup."""
    if usage is None:
        return "usage_missing", False  # missing metadata is never treated as a hit
    t = token_counts(usage)
    if setup == "without_cache":
        return ("no_reuse", True) if t["cached_tokens"] == 0 else ("INVALID_cached_tokens_reported", False)
    if t["cached_tokens"] > 0:
        return ("warm_hit_plus_write" if t["cache_write_tokens"] > 0 else "warm_hit"), True
    if t["cache_write_tokens"] > 0:
        return "cold_write", True
    return "no_cache_activity", True


def run() -> None:
    batch = uuid.uuid4().hex[:8]
    prices = load_prices()
    print(f"batch {batch}: {len(DOCS)} documents x {len(SETUP_ORDER)} setups")
    for setup in SETUP_ORDER:
        for seq, doc in enumerate(DOCS, start=1):
            scope = scope_for(setup, batch, seq)
            o = run_document(doc, "provider_default", cache_scope=scope, defense="none")
            if not o.api_responses:
                print(f"{setup:<13} {seq:>2} {doc.name:<30} NO API RESPONSE: {o.error}")
            for idx, resp in enumerate(o.api_responses):
                cls, valid = classify(setup, resp["usage"])
                counts = token_counts(resp["usage"]) if resp["usage"] else None
                append_json(CACHE_TEST, {
                    "attempt": 2,
                    "batch_id": batch,
                    "setup": setup,
                    "cache_mode": "provider_default",
                    "cache_scope": scope,
                    "sequence_in_setup": seq,
                    "request_index_in_run": idx,
                    "document": doc.name,
                    "run_id": o.run_id,
                    "timestamp_utc": o.timestamp_utc,
                    "model_requested": MODEL_NAME,
                    "model_reported": resp["model"],
                    "response_id": resp["response_id"],
                    "usage_raw": resp["usage"],
                    "tokens": counts,
                    "cost_usd": cost_usd(counts, prices) if counts else None,
                    "cache_evidence": {
                        "classification": cls,
                        "cached_tokens": counts["cached_tokens"] if counts else None,
                        "cache_write_tokens": counts["cache_write_tokens"] if counts else None,
                        "prompt_cache_diagnostics": resp["prompt_cache_diagnostics"],
                    },
                    "valid_for_setup": valid,
                    "final_response": o.output.model_dump(mode="json") if o.output else None,
                    "error": o.error,
                    "agent_code_version": o.usage.get("agent_code_version"),
                })  # fmt: skip
                print(f"{setup:<13} {seq:>2} {doc.name:<30} {cls:<30} in {counts['input_tokens']:>6} "
                      f"cached {counts['cached_tokens']:>6} write {counts['cache_write_tokens']:>6} out {counts['output_tokens']:>4}")


# ---------- report ----------


def d(x) -> Decimal:
    return Decimal(str(x))


def money(x: Decimal, places: int = 8) -> str:
    return f"${x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):f}"


def pct(part: Decimal, whole: Decimal) -> str:
    return f"{(part / whole * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"


def recompute_cost(usage: dict, prices: dict[str, Decimal]) -> dict[str, Decimal]:
    """Independent of agent.cost_usd: straight from the raw usage object and prices.json."""
    inp = usage["input_tokens"]
    det = usage.get("input_tokens_details") or {}
    cached, written = det.get("cached_tokens", 0), det.get("cache_write_tokens", 0)
    parts = {
        "uncached_input": (inp - cached - written) * prices["input_per_1m"] / MILLION,
        "cached_input": cached * prices["cached_input_per_1m"] / MILLION,
        "cache_write": written * prices["cache_write_per_1m"] / MILLION,
        "output": usage["output_tokens"] * prices["output_per_1m"] / MILLION,
    }
    parts["total"] = sum(parts.values())
    return parts


def report() -> tuple[list[str], list[str]]:
    """Returns (markdown tables, failed checks). Every number comes from cache_test.json + prices.json."""
    entries, prices = read_json_array(CACHE_TEST), load_prices()
    attempt2 = [e for e in entries if e.get("attempt") == 2]
    if not attempt2:
        raise SystemExit("no attempt-2 records in output/cache_test.json; run `python scripts/cache_test.py run`")
    batch = attempt2[-1]["batch_id"]
    rows = [e for e in attempt2 if e["batch_id"] == batch]
    attempt1 = [e for e in entries if e.get("attempt") == 1]
    probes = [e for e in entries if e.get("setup") == "diagnostic_probe"]
    failures: list[str] = []
    tables: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    # validate every record of the measured batch
    for e in rows:
        check(e["model_requested"] == MODEL_NAME, f"{e['document']}: requested model {e['model_requested']}")
        check(e["usage_raw"] is not None, f"{e['setup']} {e['document']}: usage missing")
        check(e["error"] is None, f"{e['setup']} {e['document']}: run error {e['error']}")
        if e["usage_raw"] is None:
            continue
        rc = recompute_cost(e["usage_raw"], prices)
        for k in COST_KEYS:
            check(abs(rc[k] - d(e["cost_usd"][k])) < Decimal("1e-12"), f"{e['document']} {e['setup']}: cost {k} mismatch")
        t = token_counts(e["usage_raw"])
        check(t == e["tokens"], f"{e['document']} {e['setup']}: token fields mismatch")
        check(t["total_tokens"] == t["input_tokens"] + t["output_tokens"], f"{e['document']}: total != input + output")
        check(t["uncached_input_tokens"] >= 0, f"{e['document']}: negative uncached input")
        check(e["valid_for_setup"], f"{e['setup']} {e['document']}: {e['cache_evidence']['classification']}")
        if e["setup"] == "without_cache":  # recomputed from the raw usage, not the stored flag
            check(t["cached_tokens"] == 0, f"without_cache {e['document']}: API reports {t['cached_tokens']} cached tokens")
    check(len({e["model_reported"] for e in rows}) == 1, "the API reported more than one model name in the batch")
    check(len({e["agent_code_version"] for e in rows}) == 1, "the batch mixes agent code versions")
    ids = [e["response_id"] for e in rows]
    check(len(set(ids)) == len(ids), "duplicate response ids in the measured batch (gateway replay)")
    earlier_ids = {e.get("response_id") for e in entries if e.get("batch_id") != batch}
    check(not (set(ids) & earlier_ids), "a measured response id also appears in an earlier record (gateway replay)")
    by_setup = {s: [e for e in rows if e["setup"] == s] for s in SETUP_ORDER}
    for s, es in by_setup.items():
        check(sorted(e["document"] for e in es if e["request_index_in_run"] == 0) == [p.name for p in DOCS],
              f"{s}: not exactly one run per practice document")
    check(len({e["cache_scope"] for e in by_setup["with_cache"]}) == 1, "with_cache: scope line not shared")
    check(len({e["cache_scope"] for e in by_setup["without_cache"]}) == len(by_setup["without_cache"]),
          "without_cache: scope line not unique per call")

    def totals(es):
        tok = {k: sum(e["tokens"][k] for e in es) for k in TOKEN_KEYS}
        cost = {k: sum((recompute_cost(e["usage_raw"], prices)[k] for e in es), Decimal(0)) for k in COST_KEYS}
        return tok, cost

    T = {s: totals(es) for s, es in by_setup.items()}
    n_docs = len(DOCS)
    base, cached = T["without_cache"][1]["total"], T["with_cache"][1]["total"]

    # 1: totals per setup
    lines = ["| Setup | API calls | Input tokens | Cached input | Cache-write input | Uncached input | Output tokens (reasoning) | Total cost | Avg cost per document |",
             "|---|---|---|---|---|---|---|---|---|"]
    for s in SETUP_ORDER:
        tok, cost = T[s]
        lines.append(f"| {s} | {len(by_setup[s])} | {tok['input_tokens']:,} | {tok['cached_tokens']:,} | {tok['cache_write_tokens']:,} | "
                     f"{tok['uncached_input_tokens']:,} | {tok['output_tokens']:,} ({tok['reasoning_tokens']:,}) | "
                     f"{money(cost['total'])} | {money(cost['total'] / n_docs)} |")
    tables.append("\n".join(lines))

    # 2: cost by token category
    lines = ["| Setup | Uncached input | Cached input | Cache write | Output | Total |", "|---|---|---|---|---|---|"]
    for s in SETUP_ORDER:
        c = T[s][1]
        lines.append(f"| {s} | {money(c['uncached_input'])} | {money(c['cached_input'])} | {money(c['cache_write'])} | {money(c['output'])} | {money(c['total'])} |")
    tables.append("\n".join(lines))

    # 3: savings, baseline = without_cache
    def inp(s):
        return T[s][1]["total"] - T[s][1]["output"]
    out_b, out_a = T["without_cache"][1]["output"], T["with_cache"][1]["output"]
    tables.append("\n".join([
        "| Measure | without_cache (baseline) | with_cache | Saving | Saving % of baseline |",
        "|---|---|---|---|---|",
        f"| Total cost, 12 documents | {money(base)} | {money(cached)} | {money(base - cached)} | {pct(base - cached, base)} |",
        f"| Average cost per document | {money(base / n_docs)} | {money(cached / n_docs)} | {money((base - cached) / n_docs)} | {pct(base - cached, base)} |",
        f"| Input cost only | {money(inp('without_cache'))} | {money(inp('with_cache'))} | {money(inp('without_cache') - inp('with_cache'))} | {pct(inp('without_cache') - inp('with_cache'), inp('without_cache'))} |",
        f"| Output cost only | {money(out_b)} | {money(out_a)} | {money(out_b - out_a)} | {pct(out_b - out_a, out_b)} |",
    ]))

    # 4: per-call detail
    lines = ["| # | Document | with_cache: input / cached / write / output | cost | evidence | without_cache: input / cached / write / output | cost | valid |",
             "|---|---|---|---|---|---|---|---|"]
    for doc in DOCS:
        a = [e for e in by_setup["with_cache"] if e["document"] == doc.name]
        b = [e for e in by_setup["without_cache"] if e["document"] == doc.name]
        def cell(es):
            return " + ".join(f"{e['tokens']['input_tokens']:,} / {e['tokens']['cached_tokens']:,} / {e['tokens']['cache_write_tokens']:,} / {e['tokens']['output_tokens']:,}" for e in es)
        def cst(es):
            return money(sum((recompute_cost(e["usage_raw"], prices)["total"] for e in es), Decimal(0)))
        lines.append(f"| {a[0]['sequence_in_setup']} | {doc.name} | {cell(a)} | {cst(a)} | {', '.join(e['cache_evidence']['classification'] for e in a)} | "
                     f"{cell(b)} | {cst(b)} | {'yes' if all(e['valid_for_setup'] for e in b) else 'NO'} |")
    tables.append("\n".join(lines))

    # 5: cold vs warm
    a = by_setup["with_cache"]
    cold = [e for e in a if e["cache_evidence"]["classification"] == "cold_write"]
    warm = [e for e in a if e["cache_evidence"]["classification"].startswith("warm_hit")]
    other = [e for e in a if e not in cold and e not in warm]
    unc = by_setup["without_cache"]

    def avg(es, key):
        return sum((recompute_cost(e["usage_raw"], prices)[key] for e in es), Decimal(0)) / len(es) if es else Decimal(0)

    lines = ["| Group | Calls | Avg input tokens | Avg cached | Avg cache write | Avg uncached | Avg output | Avg input cost | Avg output cost | Avg total cost |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, es in (("without_cache (all)", unc), ("with_cache, cold (no hit, cache write)", cold),
                     ("with_cache, warm (cache hit)", warm), ("with_cache, other", other)):
        if not es:
            lines.append(f"| {name} | 0 | - | - | - | - | - | - | - | - |")
            continue
        n = len(es)
        def mean_tok(k):
            return f"{Decimal(sum(e['tokens'][k] for e in es)) / n:,.1f}"
        lines.append(f"| {name} | {n} | {mean_tok('input_tokens')} | {mean_tok('cached_tokens')} | {mean_tok('cache_write_tokens')} | "
                     f"{mean_tok('uncached_input_tokens')} | {mean_tok('output_tokens')} | {money(avg(es, 'total') - avg(es, 'output'))} | "
                     f"{money(avg(es, 'output'))} | {money(avg(es, 'total'))} |")
    tables.append("\n".join(lines))

    # 6: Problem 2 estimate vs measurement
    version = rows[0]["agent_code_version"]
    if version in SYSTEM_PROMPT_TOKENS:
        sys_tokens = SYSTEM_PROMPT_TOKENS[version]
    elif version == code_version():
        sys_tokens = len(tiktoken.get_encoding("o200k_base").encode(system_prompt_text(rows[0]["cache_scope"])))
    else:
        raise SystemExit(f"no system-prompt token count recorded for code version {version}")
    warm_cached = sorted({e["tokens"]["cached_tokens"] for e in warm})
    cold_writes = sorted(e["tokens"]["cache_write_tokens"] for e in cold)
    in_tokens = [e["tokens"]["input_tokens"] for e in rows]
    unc_avg, warm_avg = avg(unc, "total"), avg(warm, "total")
    cold_cost = avg(cold, "total") if cold else None
    unc_in_avg = avg(unc, "total") - avg(unc, "output")
    warm_in_avg = avg(warm, "total") - avg(warm, "output")
    lines = ["| Quantity | Problem 2 estimate (handbook only) | Measured (whole request) |", "|---|---|---|",
             f"| Tokens per call | {ESTIMATE['handbook_tokens']:,} handbook tokens (o200k_base) | {min(in_tokens):,} to {max(in_tokens):,} input tokens; system prompt alone {sys_tokens:,} (o200k_base) |",
             f"| Tokens reused on a warm call | {ESTIMATE['handbook_tokens']:,} | {', '.join(f'{x:,}' for x in warm_cached) or 'none'} cached tokens |",
             f"| Tokens written on a cold call | {ESTIMATE['handbook_tokens']:,} | {', '.join(f'{x:,}' for x in cold_writes) or 'no cold call'} cache-write tokens ({len(cold)} cold calls) |",
             f"| Input cost per call, no caching | {money(ESTIMATE['per_call_uncached'])} | {money(unc_in_avg)} average (without_cache) |",
             f"| Input cost of a cold call | {money(ESTIMATE['per_call_cold'])} | {money(avg(cold, 'total') - avg(cold, 'output')) + f' average of {len(cold)} cold calls' if cold else 'no cold call'} |",
             f"| Input cost per warm call | {money(ESTIMATE['per_call_warm'])} | {money(warm_in_avg) if warm else 'no warm call'} average |",
             f"| 1,000 calls, no caching (input) | {money(ESTIMATE['calls_1000_no_cache'])} | {money(unc_in_avg * 1000)} |",
             f"| 1,000 calls, caching (input, 1 cold + 999 warm) | {money(ESTIMATE['calls_1000_cache'])} | "
             f"{money((avg(cold, 'total') - avg(cold, 'output')) + 999 * warm_in_avg) if cold and warm else 'n/a'} |"]
    tables.append("\n".join(lines))

    # 7: 30-day projection, 1,000 documents a day; cold share p -> cost per doc = p x cold + (1 - p) x warm
    n_total = DOCS_PER_DAY * DAYS
    b_tok0 = T["without_cache"][0]
    plain_avg = (Decimal(b_tok0["input_tokens"]) * prices["input_per_1m"] / MILLION + out_b) / n_docs
    lines = ["| Scenario | Cold-call share | Formula | 30-day cost | Saving vs measured no-caching |", "|---|---|---|---|---|",
             f"| Without caching, as measured (no reuse; every call billed as a cache write) | - | {n_total:,} x {money(unc_avg)} | {money(unc_avg * n_total, 2)} | - |",
             f"| Without caching, counterfactual with writes switched off | - | {n_total:,} x {money(plain_avg)} | {money(plain_avg * n_total, 2)} | (reference) |"]
    if cold and warm:
        later = [e for e in a if e["sequence_in_setup"] > 1]
        later_cold = sum(e in cold for e in later)
        scenarios = (("Optimistic: 1 cold call a day", Decimal(1) / DOCS_PER_DAY, "1/1,000"),
                     (f"Observed miss rate after the first call ({later_cold} of {len(later)})", Decimal(later_cold) / len(later), f"{later_cold}/{len(later)}"),
                     (f"This test's whole mix ({len(cold)} of {len(a)})", Decimal(len(cold)) / len(a), f"{len(cold)}/{len(a)}"))
        for name, p, label in scenarios:
            per_doc = p * cold_cost + (1 - p) * warm_avg
            total = per_doc * n_total
            lines.append(f"| With caching, {name} | {label} ({pct(p, Decimal(1))}) | {n_total:,} x ({label} x {money(cold_cost)} + (1 - {label}) x {money(warm_avg)}) | "
                         f"{money(total, 2)} | {money(unc_avg * n_total - total, 2)} ({pct(unc_avg * n_total - total, unc_avg * n_total)}) |")
    tables.append("\n".join(lines))

    # 8: response differences between setups
    fields = ("vendor", "sku", "sku_source", "unit_price", "lead_time_days", "lead_time_basis", "min_order", "checks", "decision")
    lines = ["| Document | Decision with_cache / without_cache | Decision-relevant fields that differ | evidence list identical? | reason text identical? |",
             "|---|---|---|---|---|"]
    n_diff = n_ev = n_reason = 0
    for doc in DOCS:
        fa = next(e["final_response"] for e in by_setup["with_cache"] if e["document"] == doc.name) or {}
        fb = next(e["final_response"] for e in by_setup["without_cache"] if e["document"] == doc.name) or {}
        diff = [f"{k}: {fa.get(k)!r} vs {fb.get(k)!r}" for k in fields if fa.get(k) != fb.get(k)]
        n_diff += bool(diff)
        n_ev += fa.get("evidence") != fb.get("evidence")
        n_reason += fa.get("reason") != fb.get("reason")
        lines.append(f"| {doc.name} | {fa.get('decision')} / {fb.get('decision')} | {'; '.join(diff) if diff else 'none'} | "
                     f"{'yes' if fa.get('evidence') == fb.get('evidence') else 'no'} | {'yes' if fa.get('reason') == fb.get('reason') else 'no'} |")
    lines.append(f"| **Total** | | {n_diff} of {n_docs} documents | {n_docs - n_ev} of {n_docs} identical | {n_docs - n_reason} of {n_docs} identical |")
    tables.append("\n".join(lines))

    # 9: attempt 1 (invalid) and diagnostic probes
    lines = ["| Record set | Calls | Input tokens | Cached input | Cache-write input | Output tokens | Cost | Valid as configured? |", "|---|---|---|---|---|---|---|---|"]
    for s in SETUP_ORDER:
        es = [e for e in attempt1 if e["setup"] == s]
        if es:
            tok, cost = totals(es)
            n_bad = sum(not e["valid_for_setup"] for e in es)
            full = sum(e["tokens"]["cached_tokens"] >= e["tokens"]["input_tokens"] - 5 for e in es)
            verdict = (f"no: {n_bad} of {len(es)} show cache activity" if n_bad else
                       f"no: {full} of {len(es)} reused the whole prompt, document included, written by the invalid without_cache pass")
            lines.append(f"| Attempt 1, {s} ({es[0]['cache_mode']} mode) | {len(es)} | {tok['input_tokens']:,} | {tok['cached_tokens']:,} | "
                         f"{tok['cache_write_tokens']:,} | {tok['output_tokens']:,} | {money(cost['total'])} | {verdict} |")
    for title, group in (("Provider-cache probes (console-transcribed)", [p for p in probes if not p["probe"].startswith("response-cache")]),
                         ("Portkey response-cache probes (saved in full)", [p for p in probes if p["probe"].startswith("response-cache")])):
        if not group:
            continue
        counted = [p for p in group if p["usage_raw"]]
        replays = [p for p in group if "replay" in str(p.get("replay_assessment", "")).split(":")[0]]
        tok_in = sum(p["usage_raw"]["input_tokens"] for p in counted)
        tok_w = sum(p["usage_raw"]["input_tokens_details"]["cache_write_tokens"] for p in counted)
        tok_c = sum(p["usage_raw"]["input_tokens_details"]["cached_tokens"] for p in counted)
        outs = [p["usage_raw"].get("output_tokens") for p in counted]
        known_out = all(o is not None for o in outs)
        lines.append(f"| {title} | {len(group)} (gateway replays: {len(replays)}) | {tok_in:,}{'' if len(counted) == len(group) else ' (one call not recorded)'} | "
                     f"{tok_c:,} | {tok_w:,} | {sum(outs) if known_out else 'not captured (at most 16 each)'} | n/a | n/a: diagnostics only |")
    tables.append("\n".join(lines))

    # 10: counterfactual - the without_cache tokens repriced as if this route could switch cache writes off
    b_tok = T["without_cache"][0]
    plain_in = Decimal(b_tok["input_tokens"]) * prices["input_per_1m"] / MILLION
    plain = plain_in + T["without_cache"][1]["output"]
    tables.append("\n".join([
        "| Baseline | How input is priced | Input cost | Total cost, 12 documents | with_cache saving vs this baseline |",
        "|---|---|---|---|---|",
        f"| Measured without_cache | as billed: {b_tok['cache_write_tokens']:,} cache-write tokens x $0.125 + {b_tok['uncached_input_tokens']:,} x $0.10 per 1M | "
        f"{money(inp('without_cache'))} | {money(base)} | {money(base - cached)} ({pct(base - cached, base)}) |",
        f"| Counterfactual, writes switched off | all {b_tok['input_tokens']:,} input tokens x $0.10 per 1M | {money(plain_in)} | {money(plain)} | "
        f"{money(plain - cached)} ({pct(plain - cached, plain)}) |",
    ]))

    # 11: what the saving is made of
    a_tok = T["with_cache"][0]
    saving = base - cached
    reads = Decimal(a_tok["cached_tokens"]) * (prices["cache_write_per_1m"] - prices["cached_input_per_1m"]) / MILLION
    in_delta = (inp("without_cache") - inp("with_cache")) - reads
    out_delta = out_b - out_a
    check(abs(reads + in_delta + out_delta - saving) < Decimal("1e-12"), "saving decomposition does not add up")
    tables.append("\n".join([
        "| Component | Calculation | Amount | Share of saving |",
        "|---|---|---|---|",
        f"| Cache reads instead of cache writes | {a_tok['cached_tokens']:,} cached tokens x ($0.125 - $0.01) per 1M | {money(reads)} | {pct(reads, saving)} |",
        f"| Fewer input tokens in with_cache | {b_tok['input_tokens'] - a_tok['input_tokens']:,} tokens (shorter scope line), billed as cache writes in without_cache | {money(in_delta)} | {pct(in_delta, saving)} |",
        f"| Fewer output tokens in with_cache | {b_tok['output_tokens'] - a_tok['output_tokens']:,} tokens x $0.50 per 1M (reasoning length varies run to run) | {money(out_delta)} | {pct(out_delta, saving)} |",
        f"| Total saving | | {money(saving)} | 100.00% |",
    ]))

    print(f"batch {batch}: {len(rows)} API calls; models reported: {sorted({e['model_reported'] for e in rows})}")
    print(f"documents whose structured answer differs between setups: {n_diff}")
    spend = sum((recompute_cost(e["usage_raw"], prices)["total"] for e in rows + attempt1), Decimal(0))
    print(f"spend on agent runs for Problem 4 (attempt 1 + measured batch): {money(spend)}")
    return tables, failures


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "run":
        run()
        return 0
    tables, failures = report()
    for i, t in enumerate(tables, 1):
        print(f"\n[table {i}]\n{t}")
    if cmd == "check":
        md = CACHE_MD.read_text(encoding="utf-8")
        for i, t in enumerate(tables, 1):
            if t not in md:
                failures.append(f"table {i} in cache_test.md does not match cache_test.json")
    print("\nchecks:", "all passed" if not failures else f"{len(failures)} FAILED")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
