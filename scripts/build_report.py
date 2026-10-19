"""Problem 9: build output/hw6_report.html, a self-contained results page, from the real output files only.

    python scripts/build_report.py          # gather data, write the report, run the checks
    python scripts/build_report.py check    # checks only (the report must already exist)

Every figure on the page is computed here from:
    data/shop_handbook.md, data/prices.json          Problem 2 estimate (recomputed with tiktoken)
    output/cache_test.json                           Problem 4 (latest valid batch)
    output/attack_results.json                       Problem 5
    output/defense_results.json                      Problem 6
    output/audit_trail.json, output/usage_log.json   Problem 7 red-team runs (documents in data/my_attacks/)
    output/exam_results.json                         Problem 8
The data is embedded in the HTML as JSON, and inline JavaScript draws the tables and SVG charts, so the file
works offline with no server and no other files. No external CSS, fonts, scripts or images.
"""

from __future__ import annotations

import json
import re
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import tiktoken

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
REPORT = OUT / "hw6_report.html"
MILLION = Decimal(1_000_000)
DOCS_PER_MONTH = 1000 * 30


def D(x) -> Decimal:
    return Decimal(str(x))


def q(x, places=8) -> float:
    return float(D(x).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def load(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def cost_of(usage: dict, prices: dict) -> Decimal:
    det = usage.get("input_tokens_details") or {}
    cached, write = det.get("cached_tokens", 0) or 0, det.get("cache_write_tokens", 0) or 0
    return ((usage["input_tokens"] - cached - write) * prices["input_per_1m"] + cached * prices["cached_input_per_1m"]
            + write * prices["cache_write_per_1m"] + usage["output_tokens"] * prices["output_per_1m"]) / MILLION


def gather() -> dict:
    prices = {k: D(v) for k, v in json.loads((ROOT / "data" / "prices.json").read_text(encoding="utf-8"))["gpt-6-luna"].items()}

    # ---- Problem 2: estimate (recomputed, same method as scripts/handbook_estimate.py)
    handbook = (ROOT / "data" / "shop_handbook.md").read_text(encoding="utf-8")
    T = len(tiktoken.get_encoding("o200k_base").encode(handbook))
    est = {
        "handbook_tokens": T,
        "per_call_uncached": q(T * prices["input_per_1m"] / MILLION),
        "per_call_cold": q(T * prices["cache_write_per_1m"] / MILLION),
        "per_call_warm": q(T * prices["cached_input_per_1m"] / MILLION),
    }
    est["calls_1000_uncached"] = q(1000 * D(est["per_call_uncached"]))
    est["calls_1000_cached"] = q(D(est["per_call_cold"]) + 999 * D(est["per_call_warm"]))
    est["saving_pct"] = q((1 - D(est["calls_1000_cached"]) / D(est["calls_1000_uncached"])) * 100, 3)

    # ---- Problem 4: caching (latest valid batch)
    ct = load("cache_test.json")
    a2 = [e for e in ct if e.get("attempt") == 2]
    batch = a2[-1]["batch_id"]
    rows = [e for e in a2 if e["batch_id"] == batch]
    calls, exact = [], {}
    for e in rows:
        c = {"setup": e["setup"], "seq": e["sequence_in_setup"], "doc": e["document"][:3],
             "input": e["tokens"]["input_tokens"], "cached": e["tokens"]["cached_tokens"],
             "write": e["tokens"]["cache_write_tokens"], "output": e["tokens"]["output_tokens"],
             "cost": q(cost_of(e["usage_raw"], prices)), "cls": e["cache_evidence"]["classification"]}
        exact[id(c)] = cost_of(e["usage_raw"], prices)  # sums use exact costs; "cost" is rounded for display
        calls.append(c)
    def tot(setup, key):
        return sum((exact[id(c)] if key == "cost" else D(c[key])) for c in calls if c["setup"] == setup)
    base, cache = tot("without_cache", "cost"), tot("with_cache", "cost")
    cold = [c for c in calls if c["setup"] == "with_cache" and c["cls"] == "cold_write"]
    warm = [c for c in calls if c["setup"] == "with_cache" and c["cls"].startswith("warm")]
    unc = [c for c in calls if c["setup"] == "without_cache"]
    def avg(cs, key="cost"):
        return sum((exact[id(c)] if key == "cost" else D(c[key])) for c in cs) / len(cs)
    unc_out = sum(D(c["output"]) * prices["output_per_1m"] / MILLION for c in unc)
    cold_out = sum(D(c["output"]) * prices["output_per_1m"] / MILLION for c in cold)
    warm_out = sum(D(c["output"]) * prices["output_per_1m"] / MILLION for c in warm)
    later = [c for c in calls if c["setup"] == "with_cache" and c["seq"] > 1]
    caching = {
        "batch": batch, "n_docs": 12,
        "total_without": q(base), "total_with": q(cache), "saving": q(base - cache), "saving_pct": q((base - cache) / base * 100, 2),
        "avg_without": q(base / 12), "avg_with": q(cache / 12),
        "cold_calls": len(cold), "warm_calls": len(warm), "cold_seqs": [c["seq"] for c in cold],
        "warm_cached_tokens": sorted({c["cached"] for c in warm}),
        "avg_cost_cold": q(avg(cold)), "avg_cost_warm": q(avg(warm)), "avg_cost_uncached": q(avg(unc)),
        "measured_input_uncached": q((sum(exact[id(c)] for c in unc) - unc_out) / len(unc)),
        "measured_input_cold": q((sum(exact[id(c)] for c in cold) - cold_out) / len(cold)),
        "measured_input_warm": q((sum(exact[id(c)] for c in warm) - warm_out) / len(warm)),
        "later_cold": sum(c in cold for c in later), "later_n": len(later),
        "uncached_input_tokens_total": int(tot("without_cache", "input")),
        "uncached_write_tokens_total": int(tot("without_cache", "write")),
        "counterfactual_no_write_total": q(tot("without_cache", "input") * prices["input_per_1m"] / MILLION + unc_out),
        "calls": calls,
    }
    caching["counterfactual_saving_pct"] = q((D(caching["counterfactual_no_write_total"]) - cache) / D(caching["counterfactual_no_write_total"]) * 100, 2)

    # ---- Problems 5 and 6
    atk = load("attack_results.json")
    dfn = load("defense_results.json")
    docs = []
    for e in dfn["documents"]:
        b = e["before_defenses"]
        before_doc = next(x for x in atk["documents"] if x["document"] == e["document"])
        docs.append({
            "doc": e["document"][:3], "file": e["document"],
            "attack": next((h["technique"] for h in before_doc["evidence"]["hidden_content_in_original_file"]
                            if not h.get("benign_page_code")), None),
            "before_fields": b["fields_correct"], "after_fields": e["fields_correct"],
            "before_decision_ok": b["decision_correct"], "after_decision_ok": e["decision"]["correct"],
            "key_decision": e["decision"]["key"],
            "before_failed": b["failed"], "after_failed": e["failed"],
            "before_traps": int(b["trap_tool_called"]), "after_traps": len(e["trap_calls"]),
            "after_review": bool(e["evidence"]["security_report"].get("needs_human_review")),
            "before_tokens": b["tokens"], "after_tokens": e["tokens"],
            "before_cost": b["cost_usd"], "after_cost": e["cost_usd"],
            "wrong_before": [f"{f}: {v['agent']} (key {v['key']})" for f, v in before_doc["fields"].items() if not v["correct"]],
        })
    configs = {}
    for g, v in {**dfn["ablation"], **dfn["isolation"]}.items():
        s = v["summary"]
        configs[g.split(":")[1]] = {
            "fields": s["field_accuracy"], "decisions": s["decision_accuracy"],
            "failed": [x[:3] for x in s["documents_with_any_failure"]], "traps": s["malicious_trap_calls"],
            "mean_input": q(s["mean_tokens_per_document"]["input_tokens"], 1),
            "mean_output": q(s["mean_tokens_per_document"]["output_tokens"], 1),
            "mean_cost": q(s["mean_cost_usd_per_document"]),
            "single_layer": g.startswith("isolation"),
            "per_doc_ok": {x["document"][:3]: not x["failed"] for x in v["documents"]},
        }
    inc = {k.split(" ")[0]: {"input": q(v["input_tokens"]["mean"], 1), "output": q(v["output_tokens"]["mean"], 1),
                             "cost": q(v["cost_usd"]["mean"]), "cost_min": q(v["cost_usd"]["min"]), "cost_max": q(v["cost_usd"]["max"]),
                             "input_min": v["input_tokens"]["min"], "input_max": v["input_tokens"]["max"]}
           for k, v in dfn["ablation_increments_per_document"].items()}
    vb, va = dfn["before_defenses"]["summary"], dfn["summary"]
    warm_b = [d for d in docs if d["before_tokens"]["cached_tokens"] > 0]
    warm_a = [d for d in docs if d["after_tokens"]["cached_tokens"] > 0]
    def mean(xs):
        xs = list(xs)
        return sum(D(x) for x in xs) / len(xs)
    defense = {
        "before": {"fields": vb["field_accuracy"], "decisions": vb["decision_accuracy"],
                   "compromised": len(vb["documents_with_any_failure"]), "traps": vb["malicious_trap_calls"],
                   "suspicious": len(vb["documents_with_suspicious_output"])},
        "after": {"fields": va["field_accuracy"], "decisions": va["decision_accuracy"],
                  "compromised": len(va["documents_with_any_failure"]), "traps": va["malicious_trap_calls"],
                  "suspicious": len(va["documents_with_suspicious_output"]),
                  "review_flags": [x[:3] for x in va["documents_flagged_for_human_review"]]},
        "regressions": len(dfn["regressions"]),
        "docs": docs, "configs": configs, "increments": inc,
        "cached_means": {
            "before_all_cost": q(mean(d["before_cost"] for d in docs)), "after_all_cost": q(mean(d["after_cost"] for d in docs)),
            "before_warm_cost": q(mean(d["before_cost"] for d in warm_b)), "after_warm_cost": q(mean(d["after_cost"] for d in warm_a)),
            "before_warm_cached": q(mean(d["before_tokens"]["cached_tokens"] for d in warm_b), 1),
            "after_warm_cached": q(mean(d["after_tokens"]["cached_tokens"] for d in warm_a), 1),
            "before_all_input": q(mean(d["before_tokens"]["input_tokens"] for d in docs), 1),
            "after_all_input": q(mean(d["after_tokens"]["input_tokens"] for d in docs), 1),
            "before_all_output": q(mean(d["before_tokens"]["output_tokens"] for d in docs), 1),
            "after_all_output": q(mean(d["after_tokens"]["output_tokens"] for d in docs), 1),
            "after_warm_n": len(warm_a), "after_cold_cost": q(next(d["after_cost"] for d in docs if d["after_tokens"]["cached_tokens"] == 0)),
        },
        "runs_total": sum(len(v) for v in dfn["runs"].values()),
        "code_version": dfn["agent_code_version"],
        "prompt_section_tokens": None,
    }
    tok = tiktoken.get_encoding("o200k_base")
    defense["prompt_section_tokens"] = (len(tok.encode((ROOT / "prompts" / "prompt.md").read_text(encoding="utf-8").strip()))
                                        - len(tok.encode((ROOT / "prompts" / "prompt_baseline.md").read_text(encoding="utf-8").strip())))

    # ---- Problem 7: red team runs, straight from the logs
    audits = load("audit_trail.json")
    usages = {u["run_id"]: u for u in load("usage_log.json")}
    redteam = []
    for a in audits:
        if not a["document"]["path"].startswith("data/my_attacks/"):
            continue
        u = usages[a["run_id"]]
        fr = a["final_response"]
        sec = a.get("security") or {}
        redteam.append({"file": a["document"]["file_name"], "defense": a["defense"], "run_id": a["run_id"],
                        "unit_price": fr["unit_price"], "min_order": fr["min_order"], "decision": fr["decision"],
                        "tool_calls": len(a["tool_calls"]), "review": bool(sec.get("needs_human_review")) if a["defense"] != "none" else None,
                        "input": u["totals"]["input_tokens"], "output": u["totals"]["output_tokens"],
                        "cost": q(sum((cost_of(r["usage"], prices) for r in u["api_requests"]), Decimal(0)))})
    css = (ROOT / "data" / "my_attacks" / "01_css_presentation_quote.html").read_text(encoding="utf-8")
    shown_price = re.search(r'::after\s*\{[^}]*content:\s*"\$([\d.]+)"', css).group(1)

    # ---- Problem 8: exam results
    ex = load("exam_results.json")
    exam = ex if isinstance(ex, list) else ex["results"]
    exam_runs = json.loads((OUT / "exam_runs.json").read_text(encoding="utf-8")) if (OUT / "exam_runs.json").exists() else {}
    verification = exam_runs.get("verification") or (ex.get("verification") if isinstance(ex, dict) else None) or []
    flags = {v["file"]: v for v in verification}

    # ---- monthly projections (30,000 documents)
    pv = dfn["documents"]
    after_warm = mean(d["after_cost"] for d in docs if d["after_tokens"]["cached_tokens"] > 0)
    after_cold = D(defense["cached_means"]["after_cold_cost"])
    def mix(p):
        return p * after_cold + (1 - p) * after_warm
    p_obs = D(caching["later_cold"]) / D(caching["later_n"])
    exam_avg = mean(r["cost_usd"] for r in exam)
    monthly = [
        {"label": "Defended, cached: optimistic (1 cold call a day)", "per_doc": q(mix(D(1) / 1000)), "kind": "cached", "basis": "projection from measured warm/cold costs (Problem 6 validation)"},
        {"label": "Defended, cached: Problem 6 validation run (measured mix)", "per_doc": q(D(defense["cached_means"]["after_all_cost"])), "kind": "cached", "basis": "measured, 12 practice documents"},
        {"label": "Defended, cached: Problem 8 exam run (measured mix)", "per_doc": q(exam_avg), "kind": "cached", "basis": "measured, 10 exam documents"},
        {"label": f"Defended, cached: observed miss rate ({caching['later_cold']} of {caching['later_n']})", "per_doc": q(mix(p_obs)), "kind": "cached", "basis": "projection: Problem 4 miss rate applied to Problem 6 costs"},
        {"label": "Undefended, uncached (no cache reuse)", "per_doc": configs["none"]["mean_cost"], "kind": "uncached", "basis": "measured, Problem 6 ablation 'none'"},
        {"label": "Defended, uncached (no cache reuse)", "per_doc": configs["full"]["mean_cost"], "kind": "uncached", "basis": "measured, Problem 6 ablation 'full'"},
    ]
    for m in monthly:
        m["monthly"] = q(D(m["per_doc"]) * DOCS_PER_MONTH, 2)
    overhead = {"per_doc_no_reuse": inc["total"]["cost"], "monthly_no_reuse": q(D(inc["total"]["cost"]) * DOCS_PER_MONTH, 2),
                "per_doc_cached_measured": q(D(defense["cached_means"]["after_all_cost"]) - D(defense["cached_means"]["before_all_cost"])),
                "monthly_cached_measured": q((D(defense["cached_means"]["after_all_cost"]) - D(defense["cached_means"]["before_all_cost"])) * DOCS_PER_MONTH, 2),
                "pct_no_reuse": q(D(inc["total"]["cost"]) / D(configs["none"]["mean_cost"]) * 100, 2)}

    return {
        "prices": {k: float(v) for k, v in prices.items()},
        "estimate": est, "caching": caching, "defense": defense,
        "redteam": {"runs": redteam, "attack1_shown_price": float(shown_price), "attack1_hidden_price": 4.90,
                    "attack2_shown_min_order": 480, "attack2_injected_min_order": 12, "sku_max_cost": 7.22},
        "exam": [{**r, "review": bool((flags.get(r["file"]) or {}).get("needs_human_review")),
                  "review_reasons": (flags.get(r["file"]) or {}).get("review_reasons") or [],
                  "blocked": (flags.get(r["file"]) or {}).get("blocked_tool_attempts") or []} for r in exam],
        "exam_total_cost": q(sum(D(r["cost_usd"]) for r in exam)),
        "monthly": monthly, "overhead": overhead, "docs_per_month": DOCS_PER_MONTH,
    }


TEMPLATE = (Path(__file__).resolve().parent / "report_template.html")


def render(data: dict) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return html.replace("/*__DATA__*/null", blob)


def check(data: dict, html: str) -> list[str]:
    fails = []

    def ok(cond, msg):
        if not cond:
            fails.append(msg)

    # self-contained: nothing loaded from outside the file
    ok(not re.search(r"<link\b|<script[^>]+src=|<img\b|<iframe\b|@import|url\(", html, re.I), "external resource reference found")
    ok(not re.search(r"""(?:src|href)\s*=\s*["']?https?://""", html, re.I), "absolute URL in src/href")
    for sid in ("summary", "caching", "attacks", "costs", "exam", "recommendation"):
        ok(f'id="{sid}"' in html, f"section #{sid} missing")
    embedded = json.loads(re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S).group(1).replace("<\\/", "</"))
    ok(embedded == json.loads(json.dumps(data)), "embedded data differs from freshly gathered data")

    # reconcile headline figures with the Markdown reports written in earlier problems
    md = {n: (OUT / n).read_text(encoding="utf-8") for n in ("cache_test.md", "attack_results.md", "defense_results.md", "red_team.md", "handbook_estimate.md")}
    c, d, e = data["caching"], data["defense"], data["estimate"]
    ok(f"{e['handbook_tokens']:,}" in md["handbook_estimate.md"], "handbook token count not in handbook_estimate.md")
    ok(f"${c['total_without']:.8f}" in md["cache_test.md"] and f"${c['total_with']:.8f}" in md["cache_test.md"], "cache totals not in cache_test.md")
    ok(f"{c['saving_pct']:.2f}%" in md["cache_test.md"], "cache saving % not in cache_test.md")
    ok(d["before"]["fields"] in md["attack_results.md"] and d["before"]["decisions"] in md["attack_results.md"], "before accuracy not in attack_results.md")
    ok(d["after"]["fields"] in md["defense_results.md"] and d["after"]["decisions"] in md["defense_results.md"], "after accuracy not in defense_results.md")
    ok(f"${data['redteam']['attack1_shown_price']:.2f}" in md["red_team.md"], "attack 1 shown price not in red_team.md")
    # internal arithmetic
    # each figure is rounded from the exact value, so rounded parts may differ by one unit in the last place
    ok(abs(D(c["total_without"]) - D(c["total_with"]) - D(c["saving"])) <= Decimal("1.5e-8"), "caching saving arithmetic")
    for m in data["monthly"]:
        ok(abs(D(m["per_doc"]) * data["docs_per_month"] - D(m["monthly"])) < Decimal("0.006"), f"monthly arithmetic: {m['label']}")
    ok(len(data["exam"]) == 10 and len({r["file"] for r in data["exam"]}) == 10, "exam must have 10 distinct files")
    ok(all(r["decision"] in ("recommend", "do_not_recommend") for r in data["exam"]), "exam decision enum")
    ok(not any(r["trap_tool_called"] for r in data["exam"]), "exam: a trap tool executed")
    ok(len(data["redteam"]["runs"]) == 4, "expected 4 red-team runs")
    ok(data["docs_per_month"] == 30000, "docs per month must be 30,000")
    return fails


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    data = gather()
    if not (len(sys.argv) > 1 and sys.argv[1] == "check"):
        REPORT.write_text(render(data), encoding="utf-8", newline="\n")
        print(f"wrote {REPORT} ({REPORT.stat().st_size:,} bytes)")
    fails = check(data, REPORT.read_text(encoding="utf-8"))
    print("checks:", "all passed" if not fails else f"{len(fails)} FAILED")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
