#!/usr/bin/env python3
"""Check the label queries against hand-applied labels. Phase 3 step 3.

    verify_labels.py --expect labelled    after labelling M1-M6 by hand
    verify_labels.py --expect clean       after removing those labels again

Read-only. Every query is built by `label_term` and `UNPROCESSED_TERMS`,
the helpers the agent itself will use, so this tests the real query
builders rather than hand-typed strings - and it is the first test of how
Gmail search parses a quoted, nested, multi-word label name.

The expected counts below come from the table in `docs/PHASE3_PLAN.md` ->
Step 3 implementation, fixed before the first run. They come from a human
count, not from this code, so a bug here most likely shows as a FAIL rather
than a false PASS; the `clean` run is the second, independent check.

Exit status: 0 all pass, 1 any fail, 2 void (mail arrived mid-check).
"""

from __future__ import annotations

import argparse
import sys

from app import auth, gmail_client
from app.categories import (
    AGENT_LABELS,
    ERROR,
    LABEL_PARENT,
    NEEDS_REVIEW,
    PROCESSED,
    RECEIVED_ONLY,
    UNPROCESSED_TERMS,
    Category,
    label_for,
    label_term,
)

# The exclusions run_pass uses, from the one definition. Sent mail is
# excluded, which is why M1-M6 must be received messages.
BASE = RECEIVED_ONLY

# M1, M2: To Action + Processed. M3: Needs Review + Processed.
# M4: the five other categories + Processed. M5: Error. M6: nothing.
LABELLED: dict[str, int] = {
    label_for(Category.TO_ACTION): 2,
    NEEDS_REVIEW: 1,
    label_for(Category.RECEIPTS): 1,
    label_for(Category.BOOKINGS): 1,
    label_for(Category.PERSONAL): 1,
    label_for(Category.PROMOTIONS): 1,
    label_for(Category.UPDATES): 1,
    PROCESSED: 4,
    ERROR: 1,
}
CLEAN: dict[str, int] = {label: 0 for label in AGENT_LABELS}
EXPECTATIONS = {"labelled": LABELLED, "clean": CLEAN}

# Multi-word names: the ones most likely to be mis-parsed when negated.
NEGATION_CHECKED = (label_for(Category.TO_ACTION), NEEDS_REVIEW)


def query(*terms: str) -> str:
    return " ".join(terms)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--expect", choices=sorted(EXPECTATIONS), required=True)
    args = parser.parse_args()
    expected = EXPECTATIONS[args.expect]
    if set(expected) != AGENT_LABELS:
        raise SystemExit("expectations do not cover exactly the agent's labels")

    auth.require_scopes(auth.READONLY)
    svc = gmail_client.service()

    def count(q: str) -> int:
        return gmail_client.count(svc, q)

    total = count(BASE)
    # (check, query, expected, actual)
    rows: list[tuple[str, str, int, int]] = []

    # 1. Positive: each label finds exactly the messages it is on.
    actual_by_label: dict[str, int] = {}
    for label in sorted(AGENT_LABELS):
        q = query(BASE, label_term(label))
        actual_by_label[label] = count(q)
        rows.append(("label", q, expected[label], actual_by_label[label]))

    # 2. Negation on the multi-word names.
    for label in NEGATION_CHECKED:
        q = query(BASE, label_term(label, negate=True))
        rows.append(("negation", q, total - expected[label], count(q)))

    # 3. The queue, and the identity that catches a wrong negation silently
    # changing it. The identity uses actual counts only, so it holds or fails
    # independently of the hand count.
    q = query(BASE, UNPROCESSED_TERMS)
    unprocessed = count(q)
    rows.append(
        ("unprocessed", q, total - expected[PROCESSED] - expected[ERROR], unprocessed)
    )
    rows.append((
        "identity",
        "unprocessed + Processed + Error == base",
        total,
        unprocessed + actual_by_label[PROCESSED] + actual_by_label[ERROR],
    ))

    # 4. Reported, not checked: does the parent match its children's messages?
    parent = count(query(BASE, label_term(LABEL_PARENT)))

    total_after = count(BASE)

    failures = 0
    print(f"base: {BASE!r} -> {total}\n")
    for check, q, want, got in rows:
        ok = want == got
        failures += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {check:<11} {want:>6} {got:>6}  {q}")
    print(f"\n  info  label_term({LABEL_PARENT!r}) matches {parent} message(s)")

    if total_after != total:
        print(f"\nVOID: base count moved {total} -> {total_after} mid-check. Rerun.")
        return 2
    print(f"\n{len(rows) - failures} passed, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
