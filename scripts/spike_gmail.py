#!/usr/bin/env python3
"""Phase 0 spike: read the real mailbox and close four open assumptions.

`docs/PLAN.md` only asks for "fetch 20 messages and print them", which proves
the pipe works. The same read can also answer four things DESIGN.md currently
guesses at:

  1. Real backfill volume - the ~3000 figure predates excluding
     sent/drafts/chats, and it drives the backfill duration estimate.
  2. Body truncation length - the 300/2000-char spike figures were invented.
     What do real bodies actually look like?
  3. Promotions allowlist seed - DESIGN.md wants a manually seeded list of
     repeat promotional senders. Derive it instead of guessing.
  4. Taxonomy fit - eyeball real subjects against the six categories, and see
     how much mail is Personal.

Read-only: the token carries `gmail.readonly` and nothing here writes.
Only aggregate data is written to disk - subjects and senders are printed to
the terminal but never saved.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import statistics
import sys
from collections import Counter

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app import auth
from app.config import DATA_DIR

# Everything the backfill would sweep. `-label:Agent/Processed` is omitted
# because that label does not exist yet.
BACKFILL_QUERY = "-in:sent -in:drafts -in:chats"

DOMAIN_SAMPLE = 150   # metadata fetches - one API call each
BODY_SAMPLE = 40      # full fetches - larger payloads
SUBJECTS_SHOWN = 20


def progress(i: int, total: int) -> None:
    """Carriage-return progress on a terminal, sparse lines when piped.

    `\r` overwrites in place on a tty but accumulates one line per step in a
    redirected file, which buries the actual output.
    """
    if sys.stdout.isatty():
        print(f"\r  fetched {i}/{total}", end="", flush=True)
    elif i % 25 == 0 or i == total:
        print(f"  fetched {i}/{total}", flush=True)


def finish_progress() -> None:
    if sys.stdout.isatty():
        print()


def service():
    creds = auth.load_credentials()
    if creds is None:
        sys.exit("Not authenticated. Run the server and visit /auth/start.")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def count_query(svc, query: str) -> int:
    """Exact count by pagination. resultSizeEstimate is only an estimate."""
    total, token = 0, None
    while True:
        resp = svc.users().messages().list(
            userId="me", q=query, maxResults=500, pageToken=token
        ).execute()
        total += len(resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token:
            return total


def label_total(svc, label_id: str) -> int | None:
    try:
        return svc.users().labels().get(
            userId="me", id=label_id
        ).execute().get("messagesTotal")
    except HttpError:
        return None


def message_ids(svc, query: str, limit: int) -> list[str]:
    ids, token = [], None
    while len(ids) < limit:
        resp = svc.users().messages().list(
            userId="me", q=query, maxResults=min(500, limit - len(ids)),
            pageToken=token,
        ).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids[:limit]


def header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def extract_text(payload: dict) -> str:
    """Walk the MIME tree for the best text representation."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})

    if mime == "text/plain" and body.get("data"):
        return base64.urlsafe_b64decode(body["data"]).decode(
            "utf-8", errors="replace"
        )

    best = ""
    for part in payload.get("parts", []):
        text = extract_text(part)
        # Prefer plain text; fall back to whatever is longest.
        if part.get("mimeType") == "text/plain" and text:
            return text
        if len(text) > len(best):
            best = text

    if not best and mime.startswith("text/") and body.get("data"):
        best = base64.urlsafe_b64decode(body["data"]).decode(
            "utf-8", errors="replace"
        )
    return best


def domain_of(sender: str) -> str:
    if "@" not in sender:
        return "(none)"
    return sender.split("@")[-1].strip(" <>\"'").lower()


def main() -> None:
    svc = service()

    # ---- 1. mailbox overview ----
    profile = svc.users().getProfile(userId="me").execute()
    print("=" * 70)
    print("MAILBOX")
    print("=" * 70)
    print(f"  account          {profile.get('emailAddress')}")
    print(f"  messages total   {profile.get('messagesTotal'):,}")
    print(f"  threads total    {profile.get('threadsTotal'):,}")
    for lid in ("INBOX", "SENT", "DRAFT", "SPAM", "TRASH"):
        n = label_total(svc, lid)
        if n is not None:
            print(f"  {lid.lower():<16} {n:,}")

    # ---- 2. real backfill volume ----
    print()
    print("=" * 70)
    print("BACKFILL SCOPE  (assumption: ~3000, pre-exclusion guess)")
    print("=" * 70)
    all_scope = count_query(svc, BACKFILL_QUERY)
    inbox_scope = count_query(svc, f"in:inbox {BACKFILL_QUERY}")
    print(f"  backfill would process   {all_scope:,}   ({BACKFILL_QUERY})")
    print(f"  of which currently inbox {inbox_scope:,}")
    print(f"  already archived         {all_scope - inbox_scope:,}")
    for label, secs in (("llama3.1:8b", 9.9), ("3B model", 4.3)):
        print(f"  at {secs}s/msg ({label:<11}) = {all_scope * secs / 3600:.1f} hours")

    # ---- 2b. age distribution ----
    #
    # Classifying a 2021 promotional email has close to zero value: for old
    # mail "archive it" is the right answer whatever category it belongs to,
    # and that needs no LLM. Sizing a date-scoped backfill is what decides
    # whether this is one overnight run or a week of them.
    print()
    print("=" * 70)
    print("AGE DISTRIBUTION  (how much would a date-scoped backfill cover?)")
    print("=" * 70)

    # Latency is prompt-bound: the logprob method emits a single token, so
    # cost is essentially prompt_tokens / prompt_eval_rate. These assume a
    # 1500-char truncation (~375 tokens) plus a ~300-token system prompt,
    # against the prompt-eval rates measured in Phase 0.
    RATE_8B, RATE_3B = 9.5, 3.8

    print(f"  {'scope':<14} {'messages':>9} {'% of all':>9} "
          f"{'8B hours':>9} {'3B hours':>9}")
    for label, q in (
        ("last 3 months", "newer_than:3m"),
        ("last 6 months", "newer_than:6m"),
        ("last 1 year", "newer_than:1y"),
        ("last 2 years", "newer_than:2y"),
        ("last 3 years", "newer_than:3y"),
        ("last 5 years", "newer_than:5y"),
        ("everything", ""),
    ):
        query = f"{q} {BACKFILL_QUERY}".strip()
        n = count_query(svc, query)
        print(f"  {label:<14} {n:>9,} {n / all_scope:>8.0%} "
              f"{n * RATE_8B / 3600:>9.1f} {n * RATE_3B / 3600:>9.1f}")

    print()
    print("  by year:")
    year_counts = []
    this_year = dt.datetime.now(tz=dt.UTC).year
    for year in range(this_year, 2009, -1):
        q = (f"after:{year}/01/01 before:{year + 1}/01/01 {BACKFILL_QUERY}")
        n = count_query(svc, q)
        year_counts.append((year, n))
        if n:
            bar = "#" * max(1, round(n / max(all_scope / 60, 1)))
            print(f"    {year}  {n:>7,}  {bar}")
    older = all_scope - sum(n for _, n in year_counts)
    if older > 0:
        print(f"    pre-2010 {older:>5,}")

    # ---- 3. body lengths ----
    print()
    print("=" * 70)
    print(f"BODY LENGTHS  (sample of {BODY_SAMPLE}, format=full)")
    print("=" * 70)
    lengths = []
    for i, mid in enumerate(message_ids(svc, BACKFILL_QUERY, BODY_SAMPLE), 1):
        msg = svc.users().messages().get(
            userId="me", id=mid, format="full"
        ).execute()
        lengths.append(len(extract_text(msg.get("payload", {}))))
        progress(i, BODY_SAMPLE)
    finish_progress()
    if lengths:
        lengths.sort()
        def pct(p): return lengths[min(int(len(lengths) * p / 100), len(lengths) - 1)]
        print(f"  median {statistics.median(lengths):,.0f} chars")
        for p in (10, 25, 50, 75, 90, 99):
            print(f"  p{p:<3} {pct(p):>8,} chars")
        print(f"  max  {max(lengths):>8,} chars")
        for cut in (500, 1000, 2000, 4000):
            share = sum(1 for x in lengths if x <= cut) / len(lengths)
            print(f"  {share:5.0%} of bodies fit in {cut:,} chars")

    # ---- 4. sender domains ----
    print()
    print("=" * 70)
    print(f"SENDER DOMAINS  (sample of {DOMAIN_SAMPLE}, allowlist seed)")
    print("=" * 70)
    domains: Counter[str] = Counter()
    for i, mid in enumerate(message_ids(svc, BACKFILL_QUERY, DOMAIN_SAMPLE), 1):
        msg = svc.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From"],
        ).execute()
        domains[domain_of(header(msg, "From"))] += 1
        progress(i, DOMAIN_SAMPLE)
    finish_progress()
    for dom, n in domains.most_common(25):
        print(f"  {n:>4}  {dom}")

    out = DATA_DIR / "sender_domains.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["domain", "count"])
        w.writerows(domains.most_common())
    print(f"\n  full tally -> {out} ({len(domains)} domains)")

    # ---- 5. taxonomy sanity check ----
    print()
    print("=" * 70)
    print(f"RECENT INBOX  ({SUBJECTS_SHOWN} subjects - do six categories fit?)")
    print("=" * 70)
    for mid in message_ids(svc, "in:inbox " + BACKFILL_QUERY, SUBJECTS_SHOWN):
        msg = svc.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        sender = header(msg, "From")[:34]
        print(f"  {sender:<36} {header(msg, 'Subject')[:60]}")


if __name__ == "__main__":
    main()
