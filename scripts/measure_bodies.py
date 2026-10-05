#!/usr/bin/env python3
"""Phase 2 measurement: what does the model actually receive as a body?

`scripts/spike_gmail.py` measured body LENGTHS, and `docs/BACKLOG.md` recorded
the result (p50 = 7,445 chars) as the input to the truncation decision. But it
measured them with an extractor whose fallback, for an email carrying no
`text/plain` part, returns the raw HTML source. So for any HTML-only mail in
that 40-message sample, the recorded percentile is the length of the markup
rather than of the prose - and `body_chars` would be tuned against a mixture
of two completely different input types.

Three questions, all read-only:

  1. How much mail is HTML-only? That is the size of the problem.
  2. How much mail has a text/plain part that is a STUB - "View this email in
     your browser" - with the real content only in the HTML? This one is worse
     than case 1 because nothing looks wrong: the extractor succeeds, and the
     result is indistinguishable from a genuinely short email.
  3. What are the length percentiles per cohort, and how much of an HTML
     body's length survives stripping the tags?
  4. How often does a message carry a `message/rfc822` part - a forward "as
     attachment", which nests a whole message with its own text parts? That
     is the one shape where "longest text part wins" can select text from a
     DIFFERENT message than the one being classified.
  5. In reply mail, how much of the body is quoted history rather than new
     text, and does the new text survive a body_chars cut? Replies keep the
     quoted chain inline in the same part, so the walk cannot mis-select -
     but the model can still end up reading mostly old content.

The walk and the selection rule are imported from `app`, never reimplemented
here: this script's output is what `docs/BACKLOG.md` cites, so it has to
measure the policy that actually ships. Only `spike_extract` below is a local
copy, because its whole purpose is to reproduce the OLD behaviour for
comparison.

Prints aggregates and two short excerpts. Writes nothing to disk, and sends
nothing anywhere - the excerpts go to the terminal only.

    uv run python scripts/measure_bodies.py [--sample 150] [--seed 7]
"""

from __future__ import annotations

import argparse
import base64
import random
import re
import statistics
import sys
from collections import Counter

from app import gmail_client
from app.message_body import (
    EXTRACTION_VERSION,
    STUB_HTML_RATIO,
    STUB_MAX_CHARS,
    select_body,
    strip_html,
)

# The Phase 2 eval frame, from the approved plan.
#
# `newer_than:1y` is RELATIVE, so it names a different population every day and
# a seeded sample drawn through it is not reproducible across sessions - the
# mailbox grows ~18 messages a day. Pass --frame with absolute epoch bounds
# (`after:<epoch> before:<epoch>`, which Gmail accepts) for anything whose
# numbers get written down. The eval sample plan pins its frame the same way.
FRAME_QUERY = "newer_than:1y -in:sent -in:drafts -in:chats"

# What the model would see, per config.Config.body_chars.
BODY_CHARS = 1500

EXCERPT_CHARS = 300

# One `messages.get` costs 5 quota units and two sweeps died between the 150th
# and 175th fetch, so pace under the limit rather than reacting to it.
DEFAULT_DELAY = 0.4

# "On <date> <someone> wrote:" and its localised cousins, plus Outlook's
# divider. Only used to locate where quoted history STARTS.
QUOTE_MARKERS = re.compile(
    r"(^\s*On .{0,120}\bwrote:)|(^\s*-+\s*Original Message\s*-+)"
    r"|(^\s*_{10,})|(^\s*From:.*\n\s*Sent:)",
    re.MULTILINE | re.IGNORECASE,
)


def quoted_split(text: str) -> tuple[int, int]:
    """(chars before quoted history, total chars).

    The boundary is the earlier of the first `>`-prefixed line and the first
    attribution marker. Crude, and deliberately so: this measures roughly how
    much of a reply is history, not a production de-quoter.
    """
    marker = QUOTE_MARKERS.search(text)
    first_quote_line = None
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(">"):
            first_quote_line = offset
            break
        offset += len(line)
    candidates = [pos for pos in (marker.start() if marker else None,
                                  first_quote_line) if pos is not None]
    return (min(candidates) if candidates else len(text)), len(text)


def has_rfc822(node: dict) -> bool:
    if node.get("mimeType") == "message/rfc822":
        return True
    return any(has_rfc822(part) for part in node.get("parts", []) or ())


def text_depths(node: dict, depth: int = 0) -> list[tuple[int, int, str]]:
    """(depth, length, mimeType) for every text leaf, for the longest-vs-shallowest
    comparison."""
    found = []
    if node.get("mimeType") in ("text/plain", "text/html"):
        found.append((depth, len(_decode(node.get("body", {}))), node["mimeType"]))
    for part in node.get("parts", []) or ():
        found += text_depths(part, depth + 1)
    return found


def spike_extract(payload: dict) -> str:
    """What `scripts/spike_gmail.py` returned - the OLD behaviour, for contrast.

    Copied rather than imported: `scripts/` is not a package, and this is the
    one thing here that must NOT track the current implementation.
    """
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime == "text/plain" and body.get("data"):
        return _decode(body)
    best = ""
    for part in payload.get("parts", []):
        text = spike_extract(part)
        if part.get("mimeType") == "text/plain" and text:
            return text
        if len(text) > len(best):
            best = text
    if not best and mime.startswith("text/") and body.get("data"):
        best = _decode(body)
    return best


def _decode(body: dict) -> str:
    data = body.get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def percentiles(values: list[int]) -> str:
    if not values:
        return "(none)"
    ordered = sorted(values)

    def at(fraction: float) -> int:
        return ordered[min(int(fraction * len(ordered)), len(ordered) - 1)]

    return (
        f"n={len(ordered):<4} p10={at(0.10):>7,} p25={at(0.25):>7,} "
        f"p50={int(statistics.median(ordered)):>7,} p75={at(0.75):>7,} "
        f"p90={at(0.90):>7,}"
    )


def progress(i: int, total: int) -> None:
    if sys.stdout.isatty():
        print(f"\r  fetched {i}/{total}", end="", flush=True)
    elif i % 25 == 0 or i == total:
        print(f"  fetched {i}/{total}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=150)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY,
        help="seconds between fetches; paces under the Gmail quota",
    )
    parser.add_argument(
        "--frame", default=FRAME_QUERY,
        help="Gmail query defining the population. Pin it with absolute "
             "epoch bounds for a reproducible sample.",
    )
    args = parser.parse_args()

    svc = gmail_client.service()

    print(f"frame:      {args.frame}")
    print(f"extraction: {EXTRACTION_VERSION}  "
          f"(stub: < {STUB_MAX_CHARS} chars plain, "
          f"> {STUB_HTML_RATIO:g}x that in stripped html)")
    ids = gmail_client.search_ids(svc, args.frame)
    print(f"frame size: {len(ids):,}")
    sample = random.Random(args.seed).sample(ids, min(args.sample, len(ids)))
    print(f"sampling {len(sample)} (seed {args.seed})\n")

    structure: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    plain_lengths: list[int] = []
    html_raw_lengths: list[int] = []
    html_stripped_lengths: list[int] = []
    spike_lengths: list[int] = []
    selected_lengths: list[int] = []
    stubs: list[tuple[str, str, int, int]] = []
    examples: dict[str, tuple[str, str, str, str]] = {}
    rfc822_cases: list[tuple[str, str, bool]] = []
    quoted_shares: list[float] = []
    new_text_fits = 0
    replies = 0
    pace = gmail_client.throttle(args.delay)

    for i, message_id in enumerate(sample, start=1):
        response = (
            svc.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = response.get("payload", {})
        parts = gmail_client.collect_text_parts(payload)
        plain = parts.get("text/plain", "")
        html = parts.get("text/html", "")
        sender = gmail_client.header(payload, "From")
        subject = gmail_client.header(payload, "Subject")

        stripped, parsed_ok = strip_html(html)
        if not parsed_ok:
            structure["parse_failed"] += 1

        selection = select_body(plain, html)
        sources[selection.source] += 1
        selected_lengths.append(len(selection.text))
        spike_lengths.append(len(spike_extract(payload)))

        if plain:
            structure["has_plain"] += 1
            plain_lengths.append(len(plain))
        elif html:
            structure["html_only"] += 1
        else:
            structure["neither"] += 1

        if html:
            html_raw_lengths.append(len(html))
            html_stripped_lengths.append(len(stripped))

        if selection.source == "stub_fallback":
            stubs.append((sender, subject, len(plain.strip()), len(stripped)))

        if selection.source in ("html", "stub_fallback"):
            key = selection.source
            if key not in examples:
                before = spike_extract(payload)
                examples[key] = (sender, subject, before, selection.text)

        # 4. forward-as-attachment: does the longest text leaf sit inside a
        #    nested message rather than in the message being classified?
        if has_rfc822(payload):
            leaves = text_depths(payload)
            plain_leaves = [x for x in leaves if x[2] == "text/plain"]
            if plain_leaves:
                longest = max(plain_leaves, key=lambda x: x[1])
                shallowest = min(plain_leaves, key=lambda x: (x[0], -x[1]))
                rfc822_cases.append((sender, subject, longest != shallowest))

        # 5. reply mail: how much of the body is quoted history?
        if plain:
            new_chars, total = quoted_split(plain)
            if new_chars < total:
                replies += 1
                quoted_shares.append(1 - new_chars / total)
                if new_chars <= BODY_CHARS:
                    new_text_fits += 1

        progress(i, len(sample))
        pace()

    if sys.stdout.isatty():
        print()
    total = len(sample)

    print("\n" + "=" * 70)
    print("STRUCTURE  (what the message carries)")
    print("=" * 70)
    for name in ("has_plain", "html_only", "neither"):
        print(f"  {name:<12} {structure[name]:>4}   {structure[name] / total:>6.1%}")

    print("\n" + "=" * 70)
    print("SELECTION  (what the rule actually chose)")
    print("=" * 70)
    for name in ("plain", "html", "stub_fallback", "none"):
        print(f"  {name:<14} {sources[name]:>4}   {sources[name] / total:>6.1%}")

    print("\n" + "=" * 70)
    print("LENGTHS")
    print("=" * 70)
    print(f"  old extractor            {percentiles(spike_lengths)}")
    print(f"  SELECTED (what ships)    {percentiles(selected_lengths)}")
    print(f"  text/plain parts         {percentiles(plain_lengths)}")
    print(f"  text/html raw            {percentiles(html_raw_lengths)}")
    print(f"  text/html stripped       {percentiles(html_stripped_lengths)}")
    if sum(html_raw_lengths):
        ratio = sum(html_stripped_lengths) / sum(html_raw_lengths)
        print(f"\n  markup overhead: stripping keeps {ratio:.1%} of the source")
    if structure["parse_failed"]:
        print(
            f"\n  WARNING: {structure['parse_failed']} body/bodies failed to "
            "parse fully;\n  their stripped lengths are understated."
        )

    fits = sum(1 for n in selected_lengths if n <= BODY_CHARS)
    print(f"\n  bodies that fit whole in {BODY_CHARS:,} chars: {fits / total:.1%}")

    print("\n" + "=" * 70)
    print("FORWARD-AS-ATTACHMENT  (message/rfc822 nesting)")
    print("=" * 70)
    print(f"  messages carrying a nested message   {len(rfc822_cases):>4}"
          f"   {len(rfc822_cases) / total:>6.1%}")
    mis = sum(1 for _, _, differs in rfc822_cases if differs)
    print(f"  ...where longest != shallowest text  {mis:>4}"
          "   <- 'longest wins' would read the ATTACHED message")
    for sender, subject, differs in rfc822_cases[:5]:
        print(f"    {'DIFFERS' if differs else 'same   '}  "
              f"{sender[:30]:<30} {subject[:32]}")

    print("\n" + "=" * 70)
    print("QUOTED HISTORY  (reply chains, inline in one part)")
    print("=" * 70)
    print(f"  bodies containing quoted history     {replies:>4}"
          f"   {replies / total:>6.1%}")
    if quoted_shares:
        mean_share = sum(quoted_shares) / len(quoted_shares)
        print(f"  mean share of body that is history   {mean_share:>6.1%}")
        print(f"  new text fitting in {BODY_CHARS:,} chars     "
              f"{new_text_fits}/{replies}"
              f"   {new_text_fits / replies:>6.1%}")

    if gmail_client.retry_count:
        print(f"\n  ({gmail_client.retry_count} quota retries served)")

    if stubs:
        print("\n" + "=" * 70)
        print(f"STUB PLAIN PARTS ({len(stubs)}) - real content is HTML-only")
        print("=" * 70)
        for sender, subject, plain_len, stripped_len in stubs[:10]:
            print(f"  {sender[:38]:<38} plain={plain_len:>4} "
                  f"stripped={stripped_len:>6,}")
            print(f"    {subject[:66]}")

    for key, title in (
        ("html", "HTML-ONLY"),
        ("stub_fallback", "STUB PLAIN PART"),
    ):
        if key not in examples:
            continue
        sender, subject, before, after = examples[key]
        print("\n" + "=" * 70)
        print(f"{title} - first {EXCERPT_CHARS} chars of what the model gets")
        print("=" * 70)
        print(f"  from:    {sender[:60]}")
        print(f"  subject: {subject[:60]}")
        print(f"\n  BEFORE (old extractor, of a {BODY_CHARS} char budget):")
        print(f"    {before[:EXCERPT_CHARS]!r}")
        print("\n  NOW:")
        print(f"    {after[:EXCERPT_CHARS]!r}")

    print(
        "\nNothing was written to disk. Excerpts above are terminal-only.\n"
        "These figures are what docs/BACKLOG.md -> Body structure records."
    )


if __name__ == "__main__":
    main()
