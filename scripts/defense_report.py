"""Problem 6: tables for output/defense_results.md, built only from output/defense_results.json (+ prices).

    python scripts/defense_report.py          # print every table and run the consistency checks
    python scripts/defense_report.py check    # also confirm each table appears verbatim in defense_results.md
"""

from __future__ import annotations

import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "output" / "defense_results.json"
MD = ROOT / "output" / "defense_results.md"
FIELDS = ("vendor", "sku", "unit_price", "lead_time_days", "min_order")
CONFIGS = ("none", "code", "code+design", "full")
SHORT = {"p01_bluehen_quote.html": "p01", "p02_elmcity_quote.html": "p02", "p03_ivyprint_catalog.html": "p03",
         "p04_harborstitch_quote.html": "p04", "p05_maplemills_quote.pdf": "p05", "p06_qtextile_quote.pdf": "p06",
         "p07_longwharf_quote.pdf": "p07", "p08_chapelst_email.eml": "p08", "p09_westville_email.eml": "p09",
         "p10_eastrock_email.eml": "p10", "p11_yorkst_quote.html": "p11", "p12_bluehen_quote.pdf": "p12"}  # fmt: skip
ATTACK = {"p02": "hidden price", "p03": "hidden vendor swap", "p04": "payment request in comment",
          "p06": "email exfiltration in white text", "p07": "hidden lead time", "p09": "hidden lead time + link",
          "p12": "off-page price"}


def money(x, places=8) -> str:
    return f"${Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):f}"


def signed(x, places=8) -> str:
    d = Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{'+' if d >= 0 else '-'}${abs(d):f}"


def num(x, places=1) -> str:
    return f"{x:+,.{places}f}"


def tables(r: dict) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    out: list[str] = []
    docs = r["documents"]

    def check(ok, what):
        if not ok:
            failures.append(what)

    check(len(docs) == 12 and len({d["document"] for d in docs}) == 12, "validation must have 12 distinct documents")
    for d in docs:
        check(d["defense"] == "full", f"{d['document']}: validation run not fully defended")
        check(d["tokens"] is not None and d["cost_usd"] is not None, f"{d['document']}: missing tokens or cost")
        check(not d["replayed_response"], f"{d['document']}: replayed response")
        check(d["model_reported"] == "gpt-6-luna-global", f"{d['document']}: unexpected model {d['model_reported']}")
    for g, v in {**r["ablation"], **r["isolation"]}.items():
        check(len(v["documents"]) == 12, f"{g}: expected 12 runs")
        check(all(not e["replayed_response"] for e in v["documents"]), f"{g}: replayed response")
        check(all((e["tokens"] or {}).get("cached_tokens") == 0 for e in v["documents"]), f"{g}: cached tokens in a no-reuse run")

    # T1 before/after per document
    lines = ["| Doc | Attack in the document | Fields correct before -> after | Decision before -> after (key) | Trap calls before -> after | Gate blocks after | Failed before -> after | Regressed? |",
             "|---|---|---|---|---|---|---|---|"]
    for d in docs:
        b, s = d["before_defenses"], SHORT[d["document"]]
        dec_b = "correct" if b["decision_correct"] else "WRONG"
        dec_a = "correct" if d["decision"]["correct"] else "WRONG"
        lines.append(f"| {s} | {ATTACK.get(s, 'none')} | {b['fields_correct']}/5 -> {d['fields_correct']}/5 | {dec_b} -> {dec_a} ({d['decision']['key']}) | "
                     f"{int(b['trap_tool_called'])} -> {len(d['trap_calls'])} | {d['tool_calls_blocked_by_gate']} | "
                     f"{'yes' if b['failed'] else 'no'} -> {'yes' if d['failed'] else 'no'} | {'YES' if d['regressed'] else 'no'} |")
    out.append("\n".join(lines))

    # T2 summary before vs after
    sb, sa = r["before_defenses"]["summary"], r["summary"]
    lines = ["| Measure | Before (undefended, Problem 5) | After (fully defended) |", "|---|---|---|",
             f"| Field accuracy | {sb['field_accuracy']} | {sa['field_accuracy']} |",
             f"| Decision accuracy | {sb['decision_accuracy']} | {sa['decision_accuracy']} |",
             f"| Documents with any failure | {len(sb['documents_with_any_failure'])} ({', '.join(SHORT[x] for x in sb['documents_with_any_failure']) or 'none'}) | "
             f"{len(sa['documents_with_any_failure'])} ({', '.join(SHORT[x] for x in sa['documents_with_any_failure']) or 'none'}) |",
             f"| Malicious trap-tool calls | {sb['malicious_trap_calls']} | {sa['malicious_trap_calls']} |",
             f"| Documents with suspicious output | {len(sb['documents_with_suspicious_output'])} | {len(sa['documents_with_suspicious_output'])} |",
             f"| Previously correct documents that became incorrect | - | {len(r['regressions'])} |",
             f"| Documents flagged for human review | - | {len(sa['documents_flagged_for_human_review'])} ({', '.join(SHORT[x] for x in sa['documents_flagged_for_human_review'])}) |"]
    out.append("\n".join(lines))

    # T3 validation tokens and cost per document (both runs used normal caching)
    lines = ["| Doc | Before: input / cached / output tokens | Before cost | After: input / cached / output tokens | After cost | Difference |", "|---|---|---|---|---|---|"]
    tb = ta = Decimal(0)
    for d in docs:
        b = d["before_defenses"]
        bt, at = b["tokens"], d["tokens"]
        tb += Decimal(str(b["cost_usd"]))
        ta += Decimal(str(d["cost_usd"]))
        lines.append(f"| {SHORT[d['document']]} | {bt['input_tokens']:,} / {bt['cached_tokens']:,} / {bt['output_tokens']:,} | {money(b['cost_usd'])} | "
                     f"{at['input_tokens']:,} / {at['cached_tokens']:,} / {at['output_tokens']:,} | {money(d['cost_usd'])} | {signed(Decimal(str(d['cost_usd'])) - Decimal(str(b['cost_usd'])))} |")
    lines.append(f"| **Total (12)** | | {money(tb)} | | {money(ta)} | {signed(ta - tb)} |")
    out.append("\n".join(lines))

    # T4 ablation + isolation outcomes
    lines = ["| Config | Field accuracy | Decision accuracy | Failed documents | Trap calls | Gate blocks | Mean input tokens | Mean output tokens | Mean cost per document |",
             "|---|---|---|---|---|---|---|---|---|"]
    for g in [f"ablation:{c}" for c in CONFIGS] + ["isolation:design", "isolation:prompt"]:
        s = (r["ablation"] | r["isolation"])[g]["summary"]
        m = s["mean_tokens_per_document"]
        lines.append(f"| {g.split(':')[1]}{' (single layer)' if g.startswith('isolation') else ''} | {s['field_accuracy']} | {s['decision_accuracy']} | "
                     f"{', '.join(SHORT[x] for x in s['documents_with_any_failure']) or 'none'} | {s['malicious_trap_calls']} | {s['tool_calls_blocked_by_gate']} | "
                     f"{m['input_tokens']:,.1f} | {m['output_tokens']:,.1f} | {money(s['mean_cost_usd_per_document'])} |")
    out.append("\n".join(lines))

    # T5 decision correctness matrix per attacked document
    groups = [f"ablation:{c}" for c in CONFIGS] + ["isolation:design", "isolation:prompt"]
    lines = ["| Doc | Attack | " + " | ".join(g.split(":")[1] for g in groups) + " |", "|---|---|" + "---|" * len(groups)]
    for name, s in SHORT.items():
        row = []
        for g in groups:
            e = next(x for x in (r["ablation"] | r["isolation"])[g]["documents"] if x["document"] == name)
            mark = "ok" if not e["failed"] else "FAIL"
            if e["tool_calls_blocked_by_gate"]:
                mark += f" ({e['tool_calls_blocked_by_gate']} blocked)"
            if e["trap_calls"]:
                mark += f" ({len(e['trap_calls'])} trap)"
            row.append(mark)
        lines.append(f"| {s} | {ATTACK.get(s, 'none')} | " + " | ".join(row) + " |")
    out.append("\n".join(lines))

    # T6 per-layer increments (controlled ablation, no cache reuse)
    lines = ["| Layer (step) | Input tokens per doc, mean (min to max) | Cached tokens per doc | Output tokens per doc, mean (min to max) | Cost per doc, mean (min to max) |",
             "|---|---|---|---|---|"]
    for step, v in r["ablation_increments_per_document"].items():
        lines.append(f"| {step} | {num(v['input_tokens']['mean'])} ({v['input_tokens']['min']:+,} to {v['input_tokens']['max']:+,}) | "
                     f"{num(v['cached_tokens']['mean'])} | {num(v['output_tokens']['mean'])} ({v['output_tokens']['min']:+,} to {v['output_tokens']['max']:+,}) | "
                     f"{signed(v['cost_usd']['mean'])} ({signed(v['cost_usd']['min'])} to {signed(v['cost_usd']['max'])}) |")
    out.append("\n".join(lines))

    # T8 with normal caching: before (Problem 5 baseline run) vs after (validation run), all calls and warm calls only
    def means(rows):
        n = len(rows)
        return {k: sum(t[k] for t, _ in rows) / n for k in ("input_tokens", "cached_tokens", "output_tokens")} | \
               {"cost": sum(Decimal(str(c)) for _, c in rows) / n, "n": n}
    before_rows = [(d["before_defenses"]["tokens"], d["before_defenses"]["cost_usd"]) for d in docs]
    after_rows = [(d["tokens"], d["cost_usd"]) for d in docs]
    lines = ["| Calls compared | n before / after | Mean input tokens before -> after | Mean cached tokens before -> after | Mean output tokens before -> after | Mean cost per document before -> after | Difference |",
             "|---|---|---|---|---|---|---|"]
    for label, pick in (("All 12 calls", lambda t: True), ("Warm calls only (cached tokens > 0)", lambda t: t["cached_tokens"] > 0)):
        mb, ma = means([r for r in before_rows if pick(r[0])]), means([r for r in after_rows if pick(r[0])])
        lines.append(f"| {label} | {mb['n']} / {ma['n']} | {mb['input_tokens']:,.1f} -> {ma['input_tokens']:,.1f} | {mb['cached_tokens']:,.1f} -> {ma['cached_tokens']:,.1f} | "
                     f"{mb['output_tokens']:,.1f} -> {ma['output_tokens']:,.1f} | {money(mb['cost'])} -> {money(ma['cost'])} | {signed(ma['cost'] - mb['cost'])} |")
    out.append("\n".join(lines))

    # T7 what the defenses did on each validation run
    lines = ["| Doc | Withheld by code layer | Facts not grounded | Model decision -> final (source) | Gate blocks | Review flag |", "|---|---|---|---|---|---|"]
    for d in docs:
        sec = d["evidence"]["security_report"]
        withheld = [q["technique"].split(":")[0] if q["technique"].startswith("hidden by CSS") else q["technique"] for q in sec["quarantined"]]
        ung = [f for f, ok in (sec.get("grounding") or {}).items() if not ok]
        lines.append(f"| {SHORT[d['document']]} | {'; '.join(withheld) or 'nothing'} | {', '.join(ung) or 'none'} | "
                     f"{sec.get('model_decision')} -> {d['decision']['agent']} ({sec.get('decision_source')}) | {len(sec.get('blocked_actions') or [])} | "
                     f"{'yes' if sec.get('needs_human_review') else 'no'} |")
    out.append("\n".join(lines))
    return out, failures


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    r = json.loads(RESULTS.read_text(encoding="utf-8"))
    ts, failures = tables(r)
    for i, t in enumerate(ts, 1):
        print(f"\n[table {i}]\n{t}")
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        md = MD.read_text(encoding="utf-8")
        failures += [f"table {i} not found verbatim in defense_results.md" for i, t in enumerate(ts, 1) if t not in md]
    print("\nchecks:", "all passed" if not failures else f"{len(failures)} FAILED")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
