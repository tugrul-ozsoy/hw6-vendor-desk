# HW6: Campus Customs Vendor Desk (Tokenomics + Prompt Injection)

A vendor desk agent for the Campus Customs shop. It reads one vendor document (HTML quote, PDF quote or `.eml` email), extracts the vendor, SKU, unit price, lead time and minimum order, and recommends whether to buy under the rules in `data/shop_handbook.md`. The project measures what the agent costs (tokens, prompt caching), attacks it with prompt injection hidden in vendor documents, defends it in three layers, and reports the results.

- **Model:** `gpt-6-luna` only, called through Portkey with `PORTKEY_API_KEY`. No other model is used anywhere. Every request names `gpt-6-luna`; the logs also show `gpt-6-luna-global` and `gpt-6-luna-2026-09-22` because the Portkey route serves it from an Azure OpenAI deployment of that name (see `output/harness.md`, "Model name").
- **Framework:** PydanticAI, with structured output validated against `models.VendorDeskResult`.
- **Safe by design:** `send_email` and `approve_payment` are decoys. They never send anything or move money; they only append the call to `output/trap_calls.json`.

The results page is [`output/hw6_report.html`](output/hw6_report.html). It is a single self-contained file: double-click it, no internet or server needed.

## 1. Installation

Tested with Python 3.14.7 on Windows.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins every directly imported package: `pydantic-ai-slim[openai]`, `openai`, `pydantic`, `python-dotenv`, `beautifulsoup4` (HTML/email parsing), `pypdf` (PDF parsing), `tiktoken` (local token counts for Problem 2) and `httpx2` (offline defense tests).

## 2. Configuration

```bash
cp .env.example .env            # Windows: copy .env.example .env
```

Put your Portkey key in `.env`:

```
PORTKEY_API_KEY=your-portkey-key-here
```

The agent reads `PORTKEY_API_KEY` from the environment or from the nearest `.env` in this folder or a parent folder. The key is never printed or logged. `.env` is listed in `.gitignore` and must never be committed.

## 3. Run the agent

```bash
python agent.py --doc data/practice_docs/p01_bluehen_quote.html                  # fully defended agent (default)
python agent.py --doc data/practice_docs/p08_chapelst_email.eml --defense none   # original, undefended Problem 3 agent
python agent.py --doc data/exam_docs/e05_quote.pdf
python agent.py --doc data/practice_docs/p05_maplemills_quote.pdf --parse-only   # show extracted text only: no AI call, no logs
```

Each run prints the structured result (`vendor`, `sku`, `unit_price`, `lead_time_days`, `min_order`, `decision`, `reason`) and appends evidence to:

- `output/usage_log.json`: every token field the API reported, and the cost from `data/prices.json`
- `output/audit_trail.json`: the text sent to the model, each tool call with its arguments and result, and the final response
- `output/trap_calls.json`: decoy tool calls (undefended agent only; empty in every run so far)
- `output/blocked_actions.json`: decoy tool calls the defended agent refused (empty so far)

One run costs about $0.0003 (warm cache) to $0.0017 (cold cache).

### Options

| Option | Values | Meaning |
|---|---|---|
| `--defense` | `full` (default), `none`, `code`, `code+design`, `design`, `prompt` | Which defense layers are on. `none` is the original Problem 3 agent, kept unchanged for comparisons. |
| `--cache` | `provider_default` (default), `handbook`, `off` | Prompt-cache settings. Our Portkey route ignores `handbook`/`off` (see `output/cache_test.md`). |
| `--parse-only` | | Print the extracted document text and exit. No AI call. |

## 4. Project layout

| Path | What it is |
|---|---|
| `agent.py` | Entry point: parse, one PydanticAI run on gpt-6-luna, defenses, logging |
| `tools.py` | Document parsers (HTML, PDF, email), the two decoy tools, JSON log helpers |
| `models.py` | Pydantic input/output types (`VendorDocument`, `VendorDeskResult`, security records) |
| `defenses.py` | The three defense layers: sanitizer, trust boundary and tool gate, policy engine |
| `prompts/prompt.md` | Defended agent instructions (Problem 6). The handbook is appended on every call. |
| `prompts/prompt_baseline.md` | The original Problem 3 instructions, used by `--defense none` |
| `data/` | Course data: handbook, prices, 12 practice docs + answer key, 10 exam docs, `my_attacks/` (2 red-team docs) |
| `scripts/` | Evaluation and reporting scripts (below) |
| `output/` | All measured results, logs and reports |
| `AI_prompts.md` | Log of the prompts used for each problem |

`output/harness.md` explains every field in `models.py`, every tool, and (in its Safety section) each defense layer.

## 5. Caching tests (Problems 2 and 4)

```bash
python scripts/handbook_estimate.py     # Problem 2: handbook token count and cost estimate (no AI calls)
python scripts/cache_test.py run        # Problem 4: 24 real runs, 12 with cache reuse and 12 without
python scripts/cache_test.py check      # recompute every figure from output/cache_test.json and match it to cache_test.md
```

Our Portkey route serves gpt-6-luna from an Azure OpenAI deployment that always caches implicitly and ignores OpenAI's explicit cache controls. The two setups therefore control reuse through the prompt prefix: a `[request group: ...]` first line that is shared across the 12 "with cache" calls and unique on every "without cache" call. In the valid comparison every "without cache" call reported 0 cached tokens; an earlier attempt that did not meet that bar is kept in the log, marked invalid and excluded. Details and results: `output/cache_test.md`.

## 6. Evaluation (Problems 5 to 9)

```bash
python scripts/attack_eval.py run          # Problem 5: undefended agent vs the 12 practice docs, scored
python scripts/defense_eval.py run         # Problem 6: defended agent + per-layer ablation runs, scored
python scripts/defense_offline_tests.py    # Problem 6: offline checks of every layer (mock model, no AI calls)
python scripts/defense_report.py check     # Problem 6: rebuild the tables and confirm they match defense_results.md
python scripts/exam_eval.py run            # Problem 8: defended agent on the 10 exam docs -> output/exam_results.json
python scripts/build_report.py             # Problem 9: rebuild output/hw6_report.html from the output files, with checks
```

- **Re-scoring without AI calls:** `attack_eval.py score`, `defense_eval.py score` and `exam_eval.py build` rebuild their JSON from the logged runs.
- **Red team (Problem 7):** run each file in `data/my_attacks/` with `--defense none` and with `--defense full`. The four runs and their run IDs are in `output/red_team.md`.
- **Answer key:** `data/practice_key.json` is read only by the scoring scripts, after the runs. The agent never sees it, and it is not used for the exam documents.
- **Human approval:** in the defended agent a document can never trigger `send_email` or `approve_payment`. A refused call is logged to `output/blocked_actions.json`, and only a named person can release it with `python scripts/approve_action.py`.
- **Inspecting hidden content:** `python scripts/inspect_docs.py <file>` lists text a person would not see but a parser would (no AI calls).

Note: `run` commands make real API calls and append to the logs in `output/`. They do not overwrite earlier evidence.

## 7. Results files

| Problem | Files |
|---|---|
| 2 | `output/handbook_estimate.md` |
| 3 | `output/harness.md`, `output/usage_log.json`, `output/audit_trail.json`, `output/trap_calls.json` |
| 4 | `output/cache_test.json`, `output/cache_test.md` |
| 5 | `output/attack_results.json`, `output/attack_results.md`, `output/rendered_views.json` |
| 6 | `output/defense_results.json`, `output/defense_results.md`, `output/blocked_actions.json` |
| 7 | `output/red_team.md` |
| 8 | `output/exam_results.json`, `output/exam_runs.json` (run IDs and verification) |
| 9 | `output/hw6_report.html` |
