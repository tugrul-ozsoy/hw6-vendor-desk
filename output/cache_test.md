# Problem 4: With and without cached tokens

What prompt caching saves when the ~10,000-token handbook goes out on every call. The test ran the Problem 3 agent on all 12 practice documents in two setups.
- **Source of the numbers:** every table comes from the API usage saved in `output/cache_test.json` and the prices in `data/prices.json`.
- **Validation:** `python scripts/cache_test.py check` recomputes every table from the JSON and confirms it matches this file exactly (section 10). The prose figures were checked separately.

## Summary

- **Valid result:** caching cut the cost of the 12 documents from **$0.01986010** to **$0.00769764**, which saves **$0.01216247 (61.24%)**.
  - Per document, the average went from $0.00165501 to $0.00064147.
  - On this route the uncached baseline still pays a 1.25× cache-write price. Measured against a baseline with no write premium, the saving is 52.83% (section 7).
- **Where the saving comes from:** 99.13% of it is 104,841 prompt-prefix tokens billed as cache reads ($0.01 per 1M) instead of cache writes ($0.125 per 1M).
- **The blocker:** our Portkey key sends gpt-6-luna to an **Azure OpenAI** deployment, and OpenAI's controls for turning caching off or placing breakpoints have no effect on that route.
  - Every request writes the part of its prompt that isn't already cached.
  - The first attempt therefore had cache activity in its "without cache" runs. It is recorded but **invalid** and isn't used for any result.
  - The valid comparison controls reuse through the first line of the prompt instead (section 1).
- **A second finding:** Portkey also has its own **response cache**. It replays an earlier response for an identical request without calling the model.
  - The measured batch can't contain replays, because every request in it is unique, and the check confirms no response ID repeats.
  - Some earlier probes and two Problem 3 re-runs were replays; they are marked as such (section 9). The agent now always bypasses this cache.
- **Answers didn't change:** every decision-relevant field was identical for every document: vendor, SKU, price, lead time, minimum order, the four checks, and the decision.
  - The free-text parts did vary from run to run: the `evidence` list differs for 5 documents and the `reason` wording for 9 (section 8).
- **At 1,000 documents a day for 30 days:** $49.65 without caching, as measured. With caching:
  - **$9.15** if the cache goes cold once a day
  - **$16.48** at the miss rate observed after the first call (2 of 11)
  - **$19.24** at this test's full mix (3 cold out of 12)

## 1. Method

**Agent.** The Problem 3 agent at code version `70412954419e6569`:
- `gpt-6-luna` through Portkey
- reasoning effort `medium`
- strict output schema
- the same prompt, handbook, tools and parser

The model requested was always `gpt-6-luna`. The API reported `gpt-6-luna-global` on every call: the name of the Azure deployment (served model `gpt-6-luna-2026-09-22`).

Later Problem 3 fixes came after this batch:
- a Portkey cache-bypass header
- parser edge cases
- one removed prompt sentence

None of them changes the extracted text of these 12 documents.

**Sample.** All 12 practice documents, one run each per setup: 24 runs in batch `0ee2d8ba`.
- The 12 `with_cache` runs went first, p01 to p12, from 23:52:59 to 23:53:40 UTC.
- The 12 `without_cache` runs followed, from 23:53:45 to 23:54:28 UTC.
- Every run made exactly one API call, with no tool calls and no retries, and all 24 response IDs are unique.

**How the provider caches (the blocker).** OpenAI's own API lets a request turn caching off (`prompt_cache_options.mode = "explicit"` with no breakpoint) or place a breakpoint right after the handbook. The evidence that this doesn't work on our route is in section 9:
- **Attempt 1:** with caching "off", all 12 calls still wrote cache, and 11 of them read 11,633 cached tokens. These were real calls with unique requests.
- **Probes:** two probes with a fresh prompt still wrote the whole prompt, one with explicit mode and one with a `prompt_cache_key`.
- **No acknowledgement:** no cache option was ever echoed back, and cache diagnostics never came back.

The routing metadata was saved in full on the response-cache probes A and C:
- `provider: azure-openai`
- `x-ms-region: East US 2`
- `x-ms-served-model: gpt-6-luna-2026-09-22`

The evidence can't tell whether Azure ignores `prompt_cache_options` or Portkey drops it before forwarding. One clue points to Portkey: its response cache treated two requests that differed only in `prompt_cache_options` as identical, which fits Portkey dropping the field. Either way, on this route caching is always implicit. Each request writes the part of its prompt that isn't already cached, and it may reuse a matching prefix written earlier. That reuse is opportunistic and not guaranteed (section 4).

**The two setups.** A cached prefix must match from the very first token. So both setups add one first line to the system prompt and change nothing else:

| Setup | First line of the system prompt | Effect |
|---|---|---|
| `with_cache` | `[request group: p4-0ee2d8ba-shared]`, the same on all 12 calls and new for this batch | Later calls can reuse the prompt prefix (scope line, instructions, handbook, tool and schema definitions) written by earlier calls. The first call starts cold |
| `without_cache` | `[request group: p4-0ee2d8ba-NN-xxxxxxxx]`, different on every call | No earlier cache entry can match, so nothing can be reused |

Everything else was identical: model, settings, prompt, handbook, document text, schema and tools. The only difference is the scope line, which is a few tokens longer in `without_cache`; per document the input differs by 5 to 8 tokens. The two setups ran one after the other, not interleaved.

**Validity rule.** A `without_cache` call is valid only if the API reports `cached_tokens = 0`. **All 12 are valid**, and the check recomputes this from the raw usage.
- **Why not "no cache activity":** attempt 1's off mode tried to have no cache activity at all. On this route that's impossible, because writes can't be turned off. "No reuse" (zero cached tokens) is the strongest condition the route allows.
- **Missing metadata:** it would never count as a hit, and no call was missing usage.

**Cost of one call** (prices per 1M tokens from `data/prices.json`):

```
uncached input = input_tokens − cached_tokens − cache_write_tokens
cost = (uncached × $0.10 + cached × $0.01 + cache_write × $0.125 + output × $0.50) ÷ 1,000,000
```

Reasoning tokens are part of `output_tokens` and are counted once.

## 2. Results

**Totals** (12 documents per setup):

| Setup | API calls | Input tokens | Cached input | Cache-write input | Uncached input | Output tokens (reasoning) | Total cost | Avg cost per document |
|---|---|---|---|---|---|---|---|---|
| with_cache | 12 | 141,594 | 104,841 | 36,717 | 36 | 4,112 (1,186) | $0.00769764 | $0.00064147 |
| without_cache | 12 | 141,668 | 0 | 141,632 | 36 | 4,305 (1,346) | $0.01986010 | $0.00165501 |

**Cost by token category:**

| Setup | Uncached input | Cached input | Cache write | Output | Total |
|---|---|---|---|---|---|
| with_cache | $0.00000360 | $0.00104841 | $0.00458963 | $0.00205600 | $0.00769764 |
| without_cache | $0.00000360 | $0.00000000 | $0.01770400 | $0.00215250 | $0.01986010 |

**Savings** (the baseline is `without_cache`; saving % = saving ÷ baseline):

| Measure | without_cache (baseline) | with_cache | Saving | Saving % of baseline |
|---|---|---|---|---|
| Total cost, 12 documents | $0.01986010 | $0.00769764 | $0.01216247 | 61.24% |
| Average cost per document | $0.00165501 | $0.00064147 | $0.00101354 | 61.24% |
| Input cost only | $0.01770760 | $0.00564164 | $0.01206597 | 68.14% |
| Output cost only | $0.00215250 | $0.00205600 | $0.00009650 | 4.48% |

## 3. Every call

Each cell is input / cached / cache-write / output tokens.

| # | Document | with_cache: input / cached / write / output | cost | evidence | without_cache: input / cached / write / output | cost | valid |
|---|---|---|---|---|---|---|---|
| 1 | p01_bluehen_quote.html | 11,786 / 0 / 11,783 / 329 | $0.00163768 | cold_write | 11,793 / 0 / 11,790 / 237 | $0.00159255 | yes |
| 2 | p02_elmcity_quote.html | 11,786 / 0 / 11,783 / 384 | $0.00166518 | cold_write | 11,794 / 0 / 11,791 / 372 | $0.00166018 | yes |
| 3 | p03_ivyprint_catalog.html | 11,783 / 11,649 / 131 / 386 | $0.00032617 | warm_hit_plus_write | 11,789 / 0 / 11,786 / 323 | $0.00163505 | yes |
| 4 | p04_harborstitch_quote.html | 11,810 / 11,649 / 158 / 369 | $0.00032104 | warm_hit_plus_write | 11,815 / 0 / 11,812 / 335 | $0.00164430 | yes |
| 5 | p05_maplemills_quote.pdf | 11,774 / 11,649 / 122 / 288 | $0.00027604 | warm_hit_plus_write | 11,780 / 0 / 11,777 / 281 | $0.00161293 | yes |
| 6 | p06_qtextile_quote.pdf | 11,818 / 11,649 / 166 / 318 | $0.00029654 | warm_hit_plus_write | 11,824 / 0 / 11,821 / 501 | $0.00172843 | yes |
| 7 | p07_longwharf_quote.pdf | 11,793 / 11,649 / 141 / 363 | $0.00031592 | warm_hit_plus_write | 11,799 / 0 / 11,796 / 397 | $0.00167330 | yes |
| 8 | p08_chapelst_email.eml | 11,777 / 11,649 / 125 / 304 | $0.00028442 | warm_hit_plus_write | 11,785 / 0 / 11,782 / 339 | $0.00164255 | yes |
| 9 | p09_westville_email.eml | 11,911 / 11,649 / 259 / 453 | $0.00037567 | warm_hit_plus_write | 11,917 / 0 / 11,914 / 534 | $0.00175655 | yes |
| 10 | p10_eastrock_email.eml | 11,796 / 11,649 / 144 / 260 | $0.00026479 | warm_hit_plus_write | 11,802 / 0 / 11,799 / 320 | $0.00163518 | yes |
| 11 | p11_yorkst_quote.html | 11,766 / 11,649 / 114 / 281 | $0.00027154 | warm_hit_plus_write | 11,771 / 0 / 11,768 / 292 | $0.00161730 | yes |
| 12 | p12_bluehen_quote.pdf | 11,794 / 0 / 11,791 / 377 | $0.00166268 | cold_write | 11,799 / 0 / 11,796 / 374 | $0.00166180 | yes |

## 4. Cold vs warm

- A **cold** call found no reusable entry: it read nothing and wrote the whole prompt.
- A **warm** call read the shared prefix of 11,649 tokens. It then wrote only the part after it, about 150 tokens, which is the vendor document.

| Group | Calls | Avg input tokens | Avg cached | Avg cache write | Avg uncached | Avg output | Avg input cost | Avg output cost | Avg total cost |
|---|---|---|---|---|---|---|---|---|---|
| without_cache (all) | 12 | 11,805.7 | 0.0 | 11,802.7 | 3.0 | 358.8 | $0.00147563 | $0.00017938 | $0.00165501 |
| with_cache, cold (no hit, cache write) | 3 | 11,788.7 | 0.0 | 11,785.7 | 3.0 | 363.3 | $0.00147351 | $0.00018167 | $0.00165518 |
| with_cache, warm (cache hit) | 9 | 11,803.1 | 11,649.0 | 151.1 | 3.0 | 335.8 | $0.00013568 | $0.00016789 | $0.00030357 |
| with_cache, other | 0 | - | - | - | - | - | - | - | - |

**Three of the 12 `with_cache` calls were cold:**
- **Call 1:** cold by design, since its scope line was new.
- **Call 2:** sent 4 s after call 1. **The cause is unknown.** A plausible guess is that the entry wasn't readable yet, but attempt 1 contradicts it: there, the p02 call sent 4 s after the first write did reuse 11,633 tokens.
- **Call 12:** sent 41 s after the first write, while calls 3 to 11 all hit. **The cause is also unknown.**

OpenAI's guide notes that "cached states live on individual machines", so a request that reaches a server without the entry misses. That fits both misses, but cache diagnostics, which would give the reason, aren't available on this route. On this route, reuse happened often but not always.

## 5. Measurements vs the Problem 2 estimate

`output/handbook_estimate.md` priced **the handbook alone** (10,058 tokens). The real requests carry more than that.

| Quantity | Problem 2 estimate (handbook only) | Measured (whole request) |
|---|---|---|
| Tokens per call | 10,058 handbook tokens (o200k_base) | 11,766 to 11,917 input tokens; system prompt alone 10,928 (o200k_base) |
| Tokens reused on a warm call | 10,058 | 11,649 cached tokens |
| Tokens written on a cold call | 10,058 | 11,783, 11,783, 11,791 cache-write tokens (3 cold calls) |
| Input cost per call, no caching | $0.00100580 | $0.00147563 average (without_cache) |
| Input cost of a cold call | $0.00125725 | $0.00147351 average of 3 cold calls |
| Input cost per warm call | $0.00010058 | $0.00013568 average |
| 1,000 calls, no caching (input) | $1.00580000 | $1.47563333 |
| 1,000 calls, caching (input, 1 cold + 999 warm) | $0.10173667 | $0.13701672 |

**Why the numbers differ:**
- **More tokens are cached than the handbook.** The reused prefix is 11,649 tokens. tiktoken counts the system prompt as it was sent (scope line + `prompt.md` + handbook) at 10,928 tokens. The remaining ~721 cached tokens are most likely the tool definitions, the output schema and message framing, which the provider places in front of the document. All of them are the same on every call, so they are cached too.
- **The baseline is billed as cache writes.** Problem 2 priced "no caching" at the plain input rate ($0.10 per 1M). On this route every baseline call reports nearly all of its input as `cache_write_tokens`, which `prices.json` prices at $0.125 per 1M (1.25×). That is why no-caching input averages $0.00147563 per call, not 11,805.7 × $0.10 ÷ 1M = $0.00118057.
  - What the measurement confirms is that every baseline call is a cache write. The 1.25× price itself comes from `prices.json`, in Problem 2 and here alike.
- **Warm calls aren't pure reads.** Implicit caching also writes the vendor document (about 151 tokens) on every warm call. That adds about $0.0000189 per call on top of the 11,649 × $0.01 ÷ 1M = $0.00011649 read.
- **"1 cold + 999 warm" was optimistic.** Problem 2 assumed a single cold call. Here 3 of 12 calls were cold, so the input saving was 68.14% rather than Problem 2's 89.885%. Output tokens can't be cached at all, which brings the total saving down to 61.24%.
- **Problem 2's "never hits" warning didn't hold on this route.** Problem 2 predicted, from OpenAI's guide, that the default layout would never get a hit. On this Azure route, implicit caching **did** reuse the shared prefix even though the last message (the document) changed every time. Problem 3's first runs show the same: p08 reused 11,580 tokens after p01, in run 4c03314a6307.

## 6. Projection: 1,000 documents a day for 30 days

Assumptions:
- Each document costs what this test measured: the same document mix, output lengths and prices.
- A cold call costs the average of the 3 cold calls, and a warm call the average of the 9 warm calls.
- Cost per document = p × cold + (1 − p) × warm, where p is the share of calls that miss the cache.
- **The 30-minute cache lifetime from OpenAI's guide isn't verified on this route.** The longest gap measured here between a write and a reuse is under a minute.

| Scenario | Cold-call share | Formula | 30-day cost | Saving vs measured no-caching |
|---|---|---|---|---|
| Without caching, as measured (no reuse; every call billed as a cache write) | - | 30,000 x $0.00165501 | $49.65 | - |
| Without caching, counterfactual with writes switched off | - | 30,000 x $0.00135994 | $40.80 | (reference) |
| With caching, Optimistic: 1 cold call a day | 1/1,000 (0.10%) | 30,000 x (1/1,000 x $0.00165518 + (1 - 1/1,000) x $0.00030357) | $9.15 | $40.50 (81.58%) |
| With caching, Observed miss rate after the first call (2 of 11) | 2/11 (18.18%) | 30,000 x (2/11 x $0.00165518 + (1 - 2/11) x $0.00030357) | $16.48 | $33.17 (66.81%) |
| With caching, This test's whole mix (3 of 12) | 3/12 (25.00%) | 30,000 x (3/12 x $0.00165518 + (1 - 3/12) x $0.00030357) | $19.24 | $30.41 (61.24%) |

**Reading the table:**
- **Optimistic bound:** "1 cold call a day" is the best case. It assumes the cache stays warm all day and never misses on routing.
- **Most realistic estimate:** this test saw 2 misses in 11 calls even while warm. If that rate holds at scale, expect about **$16.48 instead of $49.65**, a 66.81% saving. This rests on only 11 calls.
- **Alternative baseline:** against the counterfactual baseline with writes switched off ($40.80), the same caching scenarios save roughly $22 to $32 a month.

## 7. Sensitivity: a baseline without the write premium

The measured baseline includes a cost that only exists because this route can't switch writes off. Repricing the same `without_cache` tokens at the plain input rate shows how much of the saving comes from that:

| Baseline | How input is priced | Input cost | Total cost, 12 documents | with_cache saving vs this baseline |
|---|---|---|---|---|
| Measured without_cache | as billed: 141,632 cache-write tokens x $0.125 + 36 x $0.10 per 1M | $0.01770760 | $0.01986010 | $0.01216247 (61.24%) |
| Counterfactual, writes switched off | all 141,668 input tokens x $0.10 per 1M | $0.01416680 | $0.01631930 | $0.00862167 (52.83%) |

Caching still saves **52.83%** against that counterfactual. The measured figure, 61.24%, is the one supported by the actual usage.

## 8. Differences in the model's answers

| Document | Decision with_cache / without_cache | Decision-relevant fields that differ | evidence list identical? | reason text identical? |
|---|---|---|---|---|
| p01_bluehen_quote.html | recommend / recommend | none | no | no |
| p02_elmcity_quote.html | recommend / recommend | none | yes | no |
| p03_ivyprint_catalog.html | recommend / recommend | none | no | yes |
| p04_harborstitch_quote.html | recommend / recommend | none | no | no |
| p05_maplemills_quote.pdf | do_not_recommend / do_not_recommend | none | yes | no |
| p06_qtextile_quote.pdf | do_not_recommend / do_not_recommend | none | yes | no |
| p07_longwharf_quote.pdf | recommend / recommend | none | yes | no |
| p08_chapelst_email.eml | recommend / recommend | none | no | no |
| p09_westville_email.eml | recommend / recommend | none | yes | no |
| p10_eastrock_email.eml | recommend / recommend | none | yes | no |
| p11_yorkst_quote.html | do_not_recommend / do_not_recommend | none | yes | yes |
| p12_bluehen_quote.pdf | recommend / recommend | none | no | yes |
| **Total** | | 0 of 12 documents | 7 of 12 identical | 3 of 12 identical |

- **Decision-relevant fields:** caching changed **none** of them (vendor, SKU, price, lead time, minimum order, checks, decision) for any of the 12 documents.
- **Free-text parts:**
  - the `evidence` list (which snippets the model quoted, and in what order) differs for 5 documents
  - the `reason` sentence differs for 9 documents, in wording and in how many numbers it mentions
- **Output length:** it varies too (4,112 vs 4,305 output tokens in total).

This is ordinary run-to-run variation of a reasoning model. Caching changes how the input is billed, not what the model reads.

These are the undefended-baseline answers. Some recommendations follow instructions hidden in the documents, the same way in both setups. Problem 5 evaluates that.

## 9. The invalid first attempt and the diagnostic probes

These calls are recorded in `output/cache_test.json` because they really happened, but **none of them is used in the results above**:

| Record set | Calls | Input tokens | Cached input | Cache-write input | Output tokens | Cost | Valid as configured? |
|---|---|---|---|---|---|---|---|
| Attempt 1, with_cache (handbook mode) | 12 | 141,402 | 141,366 | 0 | 4,338 | $0.00358626 | no: 12 of 12 reused the whole prompt, document included, written by the invalid without_cache pass |
| Attempt 1, without_cache (off mode) | 12 | 141,402 | 127,963 | 13,403 | 4,010 | $0.00496361 | no: 12 of 12 show cache activity |
| Provider-cache probes (console-transcribed) | 7 (gateway replays: 3) | 65,762 (one call not recorded) | 0 | 65,744 | not captured (at most 16 each) | n/a | n/a: diagnostics only |
| Portkey response-cache probes (saved in full) | 3 (gateway replays: 1) | 66 | 0 | 0 | 15 | n/a | n/a: diagnostics only |

**Attempt 1** used the agent's `--cache off` mode (explicit mode, no breakpoint) and `--cache handbook` mode (explicit breakpoint after the handbook). Its 24 records are marked `excluded_from_results`.
- **"Without cache" was invalid.** All 12 calls reported cache writes, and calls 2 to 12 reported 11,633 cached tokens each.
- **"With cache" ran second and was contaminated.** It re-sent exactly the prompts the first pass had just written, so each call reused the whole prompt, document included.

**Provider-cache probes.** Seven small raw API calls tried to find out why caching couldn't be controlled. Each one's `replay_assessment` is in the JSON.
- **Copied by hand:** their numbers were copied from the console, and their response IDs, timestamps and output tokens weren't captured.
- **Real calls that matter:**
  - **1.1 and 3.1:** two fresh prompts. One used explicit mode, the other a `prompt_cache_key`. Both still wrote 10,968 and 10,952 tokens.
  - **3.3:** a different key. It wrote everything and read nothing.
- **Gateway replays:**
  - **1.2 and 3.4:** almost certainly replays of an earlier probe. Their requests differed only in `prompt_cache_options`, and their usage is identical.
  - **3.2:** an identical repeat of 3.1, so a certain replay.

  These three say nothing about the provider's cache, so the earlier claim that identical probes never hit is withdrawn.

**Portkey response-cache probes** (saved in full with headers). A tiny prompt, under the 1,024-token cache minimum, was sent three times:
- **A:** went to Azure, with Azure headers.
- **B:** an identical request. It came back with **A's response ID and timestamp and no Azure headers**: a replay that never reached the model.
- **C:** the same request with `x-portkey-cache-force-refresh: true`. It got a new response.

The same replay happened to two Problem 3 re-runs (p01 and p08 at 23:57, which reused attempt-1 response IDs). The agent now always sends the force-refresh header and flags any repeated response ID.

**Total spend for Problem 4:**
- **Agent runs:** $0.03610760 (attempt 1 plus the measured batch).
- **Provider-cache probes:** about $0.0082 of input if every one was billed. The replays most likely weren't.
- **Response-cache probes:** under $0.00002.

## 10. What caused the savings, and how it was validated

| Component | Calculation | Amount | Share of saving |
|---|---|---|---|
| Cache reads instead of cache writes | 104,841 cached tokens x ($0.125 - $0.01) per 1M | $0.01205672 | 99.13% |
| Fewer input tokens in with_cache | 74 tokens (shorter scope line), billed as cache writes in without_cache | $0.00000925 | 0.08% |
| Fewer output tokens in with_cache | 193 tokens x $0.50 per 1M (reasoning length varies run to run) | $0.00009650 | 0.79% |
| Total saving | | $0.01216247 | 100.00% |

The saving is almost entirely the price gap between writing and reading the cached prefix:
- **What gets reused:** on each of the 9 warm calls, 11,649 tokens of unchanged prefix (scope line, instructions, handbook, tool and schema definitions) are read at $0.01 per 1M. In the baseline they are written at $0.125 per 1M.
- **The other two rows are noise:** a slightly shorter scope line, and random variation in reasoning length. Neither is a caching effect.

**Validation** (`python scripts/cache_test.py check`, all passed). The checks are:
- every call's cost is recomputed from its raw `usage` object and matches the logged cost to 10⁻¹²
- the token categories match the raw usage, `total = input + output`, and uncached input is never negative
- every `without_cache` call reports zero cached tokens, recomputed from the raw usage
- each setup has exactly one run per document
- the scope line is shared in `with_cache` and unique in `without_cache`
- the batch uses a single agent code version and a single reported model
- no response ID repeats, either within the batch or anywhere else in the file, so there are no gateway replays
- the saving breakdown adds up to the total
- **each table in this file is identical to the one regenerated from the JSON**

The prose figures outside the tables are not part of the automatic check.
