"""Problem 2: count the Shop Handbook's tokens and estimate what sending it to gpt-6-luna costs.

Run: python scripts/handbook_estimate.py
Prints every file fact, token count and dollar figure in output/handbook_estimate.md. Every price
comes from data/prices.json; nothing is hard-coded. Decimal keeps the arithmetic exact.
"""

import hashlib
import json
import zipfile
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path

import tiktoken

ROOT = Path(__file__).resolve().parents[1]
HANDBOOK = ROOT / "data" / "shop_handbook.md"
PRICES = ROOT / "data" / "prices.json"
DATA_ZIP = ROOT / "data.zip"
MODEL = "gpt-6-luna"
CALLS = 1000
MILLION = Decimal(1_000_000)


def cost(tokens, price_per_1m):
    return Decimal(tokens) * price_per_1m / MILLION


def pct(part, whole):
    """Percentage to 3 decimal places, rounded half up."""
    value = (Decimal(part) / Decimal(whole) * 100).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return f"{value}%"


def main():
    raw = HANDBOOK.read_bytes()
    as_stored = raw.decode("utf-8")  # exact file contents, CRLF line endings kept
    as_sent = HANDBOOK.read_text(encoding="utf-8")  # Python text mode: CRLF -> LF
    rates = json.loads(PRICES.read_text(encoding="utf-8"), parse_float=Decimal)[MODEL]
    p_in = rates["input_per_1m"]
    p_cached = rates["cached_input_per_1m"]
    p_write = rates["cache_write_per_1m"]
    p_out = rates["output_per_1m"]

    print("== Handbook file ==")
    if DATA_ZIP.exists():
        same = zipfile.ZipFile(DATA_ZIP).read("data/shop_handbook.md") == raw
        print(f"identical to data.zip entry: {same}")
    crlf = raw.count(b"\r\n")
    print(f"SHA-256 {hashlib.sha256(raw).hexdigest()}")
    print(f"bytes {len(raw):,} | ASCII only: {raw.isascii()} | CRLF line breaks {crlf:,}")
    print(f"words (whitespace-split) {len(as_sent.split()):,}")
    print(f"characters as stored (CRLF) {len(as_stored):,} | as sent (LF) {len(as_sent):,}")
    print()

    try:
        mapped = tiktoken.encoding_for_model(MODEL).name
    except KeyError:
        mapped = "not mapped (KeyError)"
    o200k = tiktoken.get_encoding("o200k_base")
    t = len(o200k.encode(as_sent))  # primary count used for every cost below
    t_crlf = len(o200k.encode(as_stored))
    t_cl100k = len(tiktoken.get_encoding("cl100k_base").encode(as_sent))
    t_harmony = len(tiktoken.get_encoding("o200k_harmony").encode(as_sent))
    heuristic = Decimal(len(as_sent)) / 4
    print("== Token counts ==")
    print(f"tiktoken {tiktoken.__version__}; encoding_for_model('{MODEL}') -> {mapped}")
    print(f"o200k_base, as sent (LF)       {t:,}   <- primary")
    print(f"o200k_base, as stored (CRLF)   {t_crlf:,} ({t_crlf - t:+})")
    print(f"o200k_harmony, as sent (LF)    {t_harmony:,}")
    print(f"cl100k_base, as sent (LF)      {t_cl100k:,} (+{pct(t_cl100k - t, t)})")
    print(f"chars / 4 heuristic (LF)       {heuristic} (undercounts by {pct(t - heuristic, t)} of the real count)")
    print()

    print(f"== Prices from prices.json ({MODEL}, per 1M tokens) ==")
    print(f"input {p_in} | cached input {p_cached} | cache write {p_write} | output {p_out}")
    print(f"cache write / input = {p_write / p_in}x | cached / input = {p_cached / p_in}x")
    print()

    uncached = cost(t, p_in)
    warm = cost(t, p_cached)
    cold = cost(t, p_write)
    print("== One call, handbook tokens only ==")
    print(f"no caching (all uncached input)   {t:,} x {p_in} / 1M = ${uncached}")
    print(f"warm cache hit (cached input)     {t:,} x {p_cached} / 1M = ${warm}")
    print(f"cold call (cache write)           {t:,} x {p_write} / 1M = ${cold}")
    print(f"cold-call premium over no caching ${cold - uncached}")
    print()

    no_cache = CALLS * uncached
    warm_rest = (CALLS - 1) * warm
    with_cache = cold + warm_rest
    never_hit = CALLS * cold
    print(f"== {CALLS:,} calls, handbook tokens only ==")
    print(f"no caching            {CALLS:,} x ${uncached} = ${no_cache}")
    print(f"caching, 1 cold + {CALLS - 1} warm  ${cold} + {CALLS - 1} x ${warm} = ${cold} + ${warm_rest} = ${with_cache}")
    print(f"saving                ${no_cache - with_cache} = {pct(no_cache - with_cache, no_cache)} of no caching")
    print(f"writes every call, never a hit  {CALLS:,} x ${cold} = ${never_hit} ({never_hit / no_cache}x no caching)")
    print()

    print("== Break-even and cache expiry ==")
    saving_per_hit = uncached - warm
    print(f"saving per warm hit ${uncached} - ${warm} = ${saving_per_hit}")
    break_even = Fraction(cold - uncached) / Fraction(saving_per_hit)
    print(f"hits needed per write: ${cold - uncached} / ${saving_per_hit} = {break_even} = {float(break_even):.4f}")
    for writes in (1, 2, 5, 10, 50, CALLS):
        total = writes * cold + (CALLS - writes) * warm
        print(f"{writes:>5} cold writes + {CALLS - writes:>3} warm hits = ${total}")


if __name__ == "__main__":
    main()
