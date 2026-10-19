# Problem 2: Size up the handbook

How many tokens `data/shop_handbook.md` is, and what sending it to `gpt-6-luna` on every call costs, at the prices in `data/prices.json`. Every file fact, token count and dollar figure below is printed by `scripts/handbook_estimate.py`. Pricing-policy figures (272K, 1,024 tokens, 30 minutes and so on) come from the OpenAI sources listed at the end.

```bash
pip install -r requirements.txt
python scripts/handbook_estimate.py
```

`requirements.txt` pins tiktoken 0.14.0. The first run downloads tiktoken's encoding files, so it needs internet access.

## Summary

| | Handbook cost |
|---|---|
| Handbook tokens | **10,058** (o200k_base, counted locally; an **estimate** for gpt-6-luna) |
| One call, no caching | $0.0010058 |
| One call, cold cache (the first call writes the cache) | $0.00125725 |
| One call, warm cache hit (only if a hit happens) | $0.00010058 |
| 1,000 calls, no caching | **$1.0058** |
| 1,000 calls, caching works (1 cold + 999 warm) | **$0.10173667** |
| 1,000 calls, caching never hits (every call writes) | $1.25725 |

These cover handbook input tokens only. The cached rows are conditional: they need an explicit cache breakpoint after the handbook (section 6), and no hit has been observed yet.

## 1. Token count and method

**The file.** `data/shop_handbook.md` is byte-identical to the `data.zip` entry (SHA-256 `332eaabbc0b87d3eaf76719e7ef1bb2287fa33a70f1b9cc0dbf115f4d3957cb6`). It is:
- 29,241 bytes, ASCII only
- 338 Windows (CRLF) line breaks
- 6,127 words (whitespace-split)

**The text we count.** We assume the agent, which isn't built yet, will read the handbook in Python with `Path.read_text(encoding="utf-8")`. Text mode turns each CRLF into LF, so the text sent is 28,903 characters (29,241 − 338). The count is on that text. If the raw bytes were sent instead, the count would be 10,069 (+11). The agent should read the file this way so this estimate matches what it sends.

**Method.** `tiktoken` 0.14.0, `o200k_base` encoding: `len(tiktoken.get_encoding("o200k_base").encode(text))`.

- `tiktoken.encoding_for_model("gpt-6-luna")` raises `KeyError`, and OpenAI's gpt-6-luna model page gives no tokenizer information.
- `o200k_base` is the encoding tiktoken uses for OpenAI's current model families. tiktoken also ships `o200k_harmony` (the same vocabulary plus extra special tokens), which gives the same 10,058.
- **The most accurate method would measure with gpt-6-luna itself**, using OpenAI's input-token counting endpoint (`POST /v1/responses/input_tokens`) or the usage numbers in a real response. Both need an API call. This problem makes none, and Problem 4 does. So o200k_base is the best count available without calling the model.

**Measured vs estimated.** 10,058 is an exact count for o200k_base. For gpt-6-luna it is an **estimate**. No count here has been measured against the gpt-6-luna API yet.

| Count | Tokens | Role |
|---|---|---|
| o200k_base, text as sent (LF) | **10,058** | primary, used for every cost below |
| o200k_harmony, text as sent (LF) | 10,058 | same vocabulary, same count |
| o200k_base, file as stored (CRLF) | 10,069 | if the raw bytes were sent (+11) |
| cl100k_base, text as sent (LF) | 10,184 | older OpenAI tokenizer, for comparison (+1.253%) |
| characters ÷ 4 rule of thumb | 7,225.75 | rough heuristic only. It comes out 28.159% below the real count, because tables, SKUs and prices split into many short tokens |

The real tokenizers agree within about 1.3%. That fits the HW6 scenario's description of the handbook as "about 10,000 tokens".

## 2. Prices (read from `data/prices.json`)

`gpt-6-luna`, per 1,000,000 tokens. The source field reads "OpenAI Standard pricing, per 1M tokens (MGT 409 Lecture 11)".

| Token type | Price per 1M tokens | vs input |
|---|---|---|
| Input (uncached) | $0.10 | 1× |
| Cached input | $0.01 | 0.1× |
| Cache write | $0.125 | 1.25× |
| Output | $0.50 | 5× |

- **Currency.** `prices.json` doesn't name a currency. Its figures match OpenAI's gpt-6-luna model page and pricing page, which list prices in $ (OpenAI's API prices are in US dollars), so every cost here is in USD.
- **Tier.** These are Standard-tier, short-context rates. Prompts over 272K input tokens cost 2× the input and cache rates and 1.5× output. A handbook call (about 10K tokens plus one document) is far below that.

## 3. Formula

```
cost (USD) = tokens × (price per 1M tokens) ÷ 1,000,000
T = 10,058 handbook tokens
```

With the usage numbers from a real response (Problem 4), the full bill for one call is:

```
uncached input = input_tokens − cached_tokens − cache_write_tokens
cost = (uncached × 0.10 + cached × 0.01 + cache_write × 0.125 + output × 0.50) ÷ 1,000,000
```

## 4. One API call (handbook tokens only)

| Case | Uncached input | Cached input | Cache write | Output | Arithmetic | Cost |
|---|---|---|---|---|---|---|
| No caching | 10,058 | 0 | 0 | 0 | 10,058 × $0.10 ÷ 1,000,000 | **$0.0010058** |
| Cold cache, first call | 0 | 0 | 10,058 | 0 | 10,058 × $0.125 ÷ 1,000,000 | **$0.00125725** |
| Warm cache hit | 0 | 10,058 | 0 | 0 | 10,058 × $0.01 ÷ 1,000,000 | **$0.00010058** |

- A warm hit costs 1/10 of an uncached call: a 90% saving on the handbook.
- The cold first call costs **more** than an uncached call: $0.00125725 − $0.0010058 = **$0.00025145** extra.
- That write premium is real for this model. OpenAI's prompt-caching guide says that for its newer model generation, which includes gpt-6-luna, cache writes cost 1.25× the uncached input rate. The gpt-6-luna model page lists the $0.125 write price, matching `prices.json`. Older models had no write charge, but that doesn't apply here.

## 5. 1,000 calls (handbook tokens only)

**Without caching:** 1,000 × $0.0010058 = **$1.0058**

**With caching that works** (1 cold call + 999 warm hits):

```
  $0.00125725                     (1 cold call: 10,058 × $0.125 ÷ 1,000,000)
+ 999 × $0.00010058 = $0.10047942 (999 warm calls)
= $0.10173667
```

**Saving:** $1.0058 − $0.10173667 = **$0.90406333**, exactly 89.885% of the uncached cost.

**With caching that never hits** (every call writes, see section 6): 1,000 × $0.00125725 = **$1.25725**, 1.25× the uncached cost.

## 6. Assumptions: when does a cache hit actually happen?

**A cache hit is not assumed.** gpt-6-luna belongs to the newer model generation, so the guide's caching rules for those models apply; its $0.125 write price is the sign of that. Under those rules, caching works through **breakpoints**:

- **The default layout misses.** In implicit mode (the default), OpenAI puts the breakpoint at the end of the latest eligible message. Here that is the user message holding the vendor document.
  - Picture the handbook in a developer message, followed by the vendor document in a user message. Every call writes the whole prompt, handbook plus that call's document, to the cache.
  - The next call has a different document, so it doesn't match that entry. The guide's own example warns that "there is no separate breakpoint after the static content."
  - The result is the never-hit case: **$1.25725 per 1,000 calls, more than not caching at all.**
- **What 1 cold + 999 warm needs.** The agent must:
  1. Put the handbook in a developer-message `input_text` block that carries `"prompt_cache_breakpoint": {"mode": "explicit"}`, before any per-document content. "Top-level `instructions` cannot contain an explicit breakpoint," so the handbook can't go there.
  2. Set `prompt_cache_options.mode` to `"explicit"`, so only that breakpoint is written and the vendor document is never written to the cache.
  3. Send byte-identical text up to the breakpoint on every call, with the same line endings and no per-call edits.
  4. Keep every request setting that renders into the prefix identical on every call:
     - model
     - tool definitions and their order
     - `parallel_tool_calls`
     - the structured-output schema (`text.format`)
     - `reasoning.effort`
     - `text.verbosity`

     Changing any of these stops reuse even with the breakpoint in place.
  5. Keep that prefix at 1,024 tokens or more, the minimum for these models. It is 10,058.
  6. Call again within 30 minutes. A cache entry for these models stays eligible for 30 minutes after its most recent write or reuse. OpenAI may keep it longer, but only 30 minutes is promised.
  7. Stay in the same organization and processing region: caches aren't shared across organizations or regional processing boundaries. Even then, a hit isn't certain. The guide notes that "traffic above 15 requests per minute can lead to overflow routing" to a machine that doesn't hold the entry.
- **No caching at all.** Explicit mode with no breakpoint caches nothing and writes nothing. That gives the "no caching" row: every token at $0.10.

**Evidence for a hit.** It comes from `usage.input_tokens_details.cached_tokens` and `usage.input_tokens_details.cache_write_tokens` in the response. For these models, `cached_tokens` reports the exact eligible boundary (excluding OpenAI's hidden tokens). Rounding down to 128-token steps applied only to older models. Until Problem 4 reads these fields, every cached figure here is an estimate.

**Cache expiry.** If the cache lapses, the next call is a cold write again. That can happen after a gap of more than 30 minutes since the last write or reuse, or after overflow routing. With *w* cold writes in 1,000 calls, the cost is *w* × $0.00125725 + (1,000 − *w*) × $0.00010058:

| Cold writes | Warm hits | Handbook cost for 1,000 calls |
|---|---|---|
| 1 | 999 | $0.10173667 |
| 2 | 998 | $0.10289334 |
| 5 | 995 | $0.10636335 |
| 10 | 990 | $0.11214670 |
| 50 | 950 | $0.15841350 |
| 1,000 (never a hit) | 0 | $1.25725 |

**Break-even.** Each write costs $0.00025145 more than an uncached call. Each warm hit saves $0.0010058 − $0.00010058 = $0.00090522. A write pays for itself after $0.00025145 ÷ $0.00090522 = 5/18 ≈ **0.2778 hits**. So a write followed by even one hit pays off, and a write that is never reused costs 25% more than not caching.

## 7. What these estimates include and exclude

**Included:** the 10,058 tokens of handbook text, billed as input: uncached, cache write or cached, as labeled.

**Excluded:**
- The rest of each request:
  - any fixed instructions outside the handbook
  - role and message framing (a few tokens per message, which also land in the cached prefix)
  - the vendor document
  - tool or function definitions
  - any structured-output schema
- **All output tokens**, at $0.50 per 1M. The handbook produces no output by itself. The agent's answer comes on top, and so do its reasoning tokens: gpt-6-luna is a reasoning model, and OpenAI counts reasoning tokens as output tokens.
- Other pricing modes: Flex and Batch (50%), fast mode (2×), regional processing (+10%), and long-context rates (over 272K input tokens).
- Retries, failed calls, Portkey platform fees and taxes.
- Tokenizer error: the real gpt-6-luna count may differ from o200k_base.

## 8. Why a stable handbook prefix cuts cost

Before writing anything, the model runs every input token through its layers and builds internal state for each one. Prompt caching stores that state for the start of a prompt. When a later request **starts with exactly the same tokens up to a cache breakpoint**, the provider reuses the stored work instead of recomputing it. It bills those tokens at the cached rate, $0.01 instead of $0.10 per 1M here.

The handbook is the same on every call, which makes it the ideal cached prefix:
- **Static part first, breakpoint right after it.** The handbook (and any fixed instructions) goes at the top with an explicit breakpoint at its end. The vendor document goes after the breakpoint. Anything that changes before the breakpoint breaks the match: a timestamp, the document, a whitespace or line-ending difference.
- **Write once, read many.** The first call pays the 1.25× write. Each later call made within 30 minutes of the previous write or reuse can read it at 0.1×.
- **Get the layout wrong and you pay more.** Without the breakpoint, every call writes a new entry that is never reused, and the handbook costs 1.25× instead of 0.1×.

If every call after the first hits the cache, the handbook goes from $1.0058 per 1,000 calls to $0.10173667, about 90% off.

## 9. Next: check against real usage in Problem 4

These are estimates. In **Problem 4** we will call `gpt-6-luna` through Portkey (the HW6 brief's `PORTKEY_API_KEY`) and read the usage the API actually reports, on one cold call and one warm call:
- `input_tokens`, to compare with 10,058 plus framing
- `cached_tokens` and `cache_write_tokens`
- `output_tokens`, including reasoning tokens

Then we'll recompute the bill with the formula in section 3.

## Sources

- `data/prices.json` and `data/shop_handbook.md`
- OpenAI, [Prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching): breakpoints, implicit vs explicit mode, the 1.25× write rate for the newer model generation, the 1,024-token minimum, 30-minute eligibility, usage fields, overflow routing
- OpenAI, [GPT-6 Luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna): prices, the 272K long-context threshold, reasoning support
- OpenAI, [API pricing](https://developers.openai.com/api/docs/pricing)
