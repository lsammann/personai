#!/usr/bin/env python3
"""Audit an extraction rule change against the eval cache. Entirely offline.

Two questions, neither answerable from a unit test:

  1. Which messages does the current rule select a DIFFERENT part for than the
     previous one did - and what were those messages labelled? A flip on a
     `Personal` message is the one that costs something, because `Personal`
     keeps the inbox. Flips are listed rather than counted: judged by reading,
     not by arithmetic.

  2. What does the body-length distribution look like now? `body_chars` is one
     of the three knobs the Phase 2 sweep tunes, and its candidate values have
     to straddle the real distribution or the sweep spends runs on a knob that
     is not moving. On CPU-only inference the same number also predicts how
     long that sweep takes, since the call emits one token and cost is
     essentially prompt length.

No Gmail, no Ollama. `data/eval_cache/` holds each message's raw `text_plain`
and `text_html` - which is exactly why A1 stores raw parts rather than a
finished body - so a rule change is re-scorable against the same 200 messages
as often as needed.

    uv run python scripts/measure_extraction.py
"""

from __future__ import annotations

import json
from collections import Counter

from app import evallabel, message_body
from app.config import DATA_DIR
from app.message_body import select_body

CACHE_DIR = DATA_DIR / "eval_cache"

# The rule as committed before 2026-09-12: prefer plain, fall back to HTML on a
# raw-length stub test, no zero-width stripping, no URL rewriting, no markup
# detection. Kept here rather than in git history so the comparison is one
# command.
V2_STUB_MAX_CHARS = 200
V2_STUB_HTML_RATIO = 2.0

# Candidate truncation points for the step 6 sweep.
BODY_CHARS = (0, 300, 800, 1500, 3000)


def v2_strip_html(html: str) -> str:
    """`strip_html` with zero-width removal disabled, so v2's lengths are v2's.

    Patched rather than reimplemented: a second copy of the parser would drift
    from the real one and the comparison would quietly stop being a comparison.
    """
    real = message_body.strip_zero_width
    message_body.strip_zero_width = lambda text: text
    try:
        return message_body.strip_html(html)[0]
    finally:
        message_body.strip_zero_width = real


def v2_select(text_plain: str, text_html: str) -> tuple[str, str]:
    plain = text_plain.strip()
    html_text = v2_strip_html(text_html)
    is_stub = (
        len(plain) < V2_STUB_MAX_CHARS
        and len(html_text) > V2_STUB_HTML_RATIO * len(plain)
    )
    if html_text and (not plain or is_stub):
        return ("stub_fallback" if plain else "html"), html_text
    return ("plain" if plain else "none"), plain


def percentile(values: list[int], q: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(int(q / 100 * len(ordered)), len(ordered) - 1)]


def main() -> int:
    records, _ = evallabel.load_records(evallabel.LABELED_PATH)
    labels = {m: r.label.value for m, r in evallabel.resolve(records).items()}

    cached = sorted(CACHE_DIR.glob("*.json"))
    if not cached:
        print(f"no cached messages in {CACHE_DIR}")
        return 1

    flips: list[tuple] = []
    lengths: list[int] = []
    by_source: dict[str, list[int]] = {}
    thin: list[tuple] = []

    for path in cached:
        row = json.loads(path.read_text(encoding="utf-8"))
        old_source, old_text = v2_select(row["text_plain"], row["text_html"])
        new = select_body(row["text_plain"], row["text_html"])
        label = labels.get(row["message_id"], "?")

        if old_source != new.source:
            flips.append((label, old_source, len(old_text), new.source,
                          len(new.text), row["sender"][:30], row["subject"][:44]))
        lengths.append(len(new.text))
        by_source.setdefault(new.source, []).append(len(new.text))
        # Sender and subject alone still reach the model, so this is thin
        # input rather than no input - but it is invisible in an accuracy
        # number unless it is counted here.
        if len(new.text) < 120:
            thin.append((label, len(new.text), row["sender"][:30],
                         row["subject"][:44]))

    print(f"{len(cached)} cached messages, {len(labels)} labelled\n")

    print(f"{'='*94}\nv2 -> v4 SOURCE FLIPS  ({len(flips)})\n{'='*94}")
    for label, a, la, b, lb, sender, subject in sorted(flips):
        print(f"  {label:<11}{a:>13} {la:>7} -> {b:<13}{lb:<7}  "
              f"{sender:<32}{subject}")
    personal = [f for f in flips if f[0] == "Personal"]
    print(f"\n  flips on a Personal message: {len(personal)} "
          f"(the ones that cost something - Personal keeps INBOX)")

    print(f"\n{'='*94}\nBODY LENGTH UNDER v4\n{'='*94}")
    print(f"{'cohort':<16}{'n':>5}{'p10':>9}{'p50':>9}{'p90':>9}")
    print(f"{'all':<16}{len(lengths):>5}{percentile(lengths,10):>9}"
          f"{percentile(lengths,50):>9}{percentile(lengths,90):>9}")
    for source in sorted(by_source):
        vals = by_source[source]
        print(f"{source:<16}{len(vals):>5}{percentile(vals,10):>9}"
              f"{percentile(vals,50):>9}{percentile(vals,90):>9}")

    print("\nHow much of the set each candidate body_chars sends WHOLE:")
    for limit in BODY_CHARS:
        whole = sum(1 for n in lengths if n <= limit)
        sent = sum(min(n, limit) for n in lengths)
        print(f"  {limit:>5}: {whole:>3}/{len(lengths)} complete "
              f"({100*whole/len(lengths):>4.1f}%)   "
              f"{sent:>9,} chars per run")

    print(f"\n{'='*94}\nTHIN BODIES - under 120 chars, effectively sender+subject "
          f"only  ({len(thin)})\n{'='*94}")
    for label, n, sender, subject in sorted(thin):
        print(f"  {label:<11}{n:>4} chars  {sender:<32}{subject}")
    print(f"\nbody_source across the set: "
          f"{dict(Counter({k: len(v) for k, v in by_source.items()}))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
