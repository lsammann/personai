#!/usr/bin/env python3
"""Review one dry run as a second eval. Phase 3 step 9 (A4).

    review_run.py label RUN_ID     call every message of the run, resumable
    review_run.py score RUN_ID     gate step 2's figures, from the calls so far

`label` shows each message of the run exactly as the eval labeller does -
the body the model saw, `m` for the rest - plus one line with the model's
answer and what the run would have done. **Not blind** (A5): judge what
the message is, by the Phase 2 labelling rules, not whether the model's
answer looks plausible. Every call is a digit, including agreeing with the
model; there is no Enter-to-accept (step 9 Q3). Keys as in `label_eval.py`:
`u` marks the next digit unsure, `s` skips for this sitting, `b` goes back,
`q` quits. Every decision is on disk before the next message is shown.

Messages are shown in a shuffle seeded from the run id, so a resumed sitting
keeps the same order. Each is fetched again (the log holds no bodies), 50 at
a time, into `data/review_cache/`. Calls go to `data/reviews/<RUN_ID>.jsonl`.
Neither is committed.

`score` makes no Gmail call. It prints A5's caveat with the figures.

Read-only: `gmail.readonly` is all it asks for, and it calls only
`gmail_client.fetch`. It is on neither callers list in
`tests/test_write_guard.py`, so it cannot refer to `modify_labels` or
`classify_and_label` without failing the suite. Decisions live in
`app/review.py`; this script only fetches, prompts and prints.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import deque

from app import auth, evallabel, gmail_client, labelloop, logbook, review
from app.categories import Category
from app.logbook import Record


def load_run(run_id: str) -> dict[str, Record]:
    records, damaged = logbook.read_all()
    if damaged:
        print(f"warning: {damaged} unreadable line(s) in the log", file=sys.stderr)
    rows = review.run_rows(records, run_id)
    if not rows:
        raise SystemExit(f"no rows for run {run_id} in the log")
    return rows


def load_labels(run_id: str) -> dict[str, evallabel.LabelRecord]:
    path = review.review_path(run_id)
    records, damaged = evallabel.load_records(path)
    if damaged:
        print(f"warning: {damaged} unreadable row(s) in {path}", file=sys.stderr)
    return evallabel.resolve(records)


def cmd_label(args: argparse.Namespace) -> int:
    path = review.review_path(args.run_id)
    rows = load_run(args.run_id)
    unscoreable = review.not_in_inbox(rows)
    if unscoreable:
        # Refused before any labelling: the run could not be scored after it.
        print(
            f"{len(unscoreable)} row(s) were not in the inbox when fetched; "
            "this run cannot be scored from the log.",
            file=sys.stderr,
        )
        return 1

    resolved = load_labels(args.run_id)
    order = review.ordering(list(rows), args.run_id)
    total = len(order)
    queue = deque(evallabel.pending(order, resolved))
    print(f"run {args.run_id}: {len(resolved)}/{total} reviewed, {len(queue)} to go")
    if not queue:
        print("review complete. Run `score`.")
        return 0

    auth.require_scopes(auth.READONLY)
    service: list = []
    unfetchable: set[str] = set()

    def get(message_id: str) -> gmail_client.Message | None:
        # Built lazily, so a sitting whose messages are all cached is offline.
        # A per-message error (a deleted message's 404) skips that message and
        # is remembered, so the next batch does not ask again; a 403 is about
        # the account and halts, as it does in the agent (step 9 Q4).
        if message_id in unfetchable:
            return None
        if not service:
            service.append(gmail_client.service())
        try:
            return gmail_client.fetch(service[0], message_id)
        except gmail_client.Forbidden:
            raise
        except gmail_client.GmailApiError as error:
            print(f"\n{message_id}: {error}")
            unfetchable.add(message_id)
            return None

    def show(message: evallabel.Cached, show_full: bool, position: int) -> str:
        text = evallabel.render(
            message, args.body_chars, show_full, position=position, total=total
        )
        return f"{text}\n\n{review.suggestion(rows[message.message_id])}"

    def record(message_id: str, label: Category, unsure: bool) -> evallabel.LabelRecord:
        row = evallabel.LabelRecord(
            message_id=message_id,
            label=label,
            unsure=unsure,
            labelled_at=dt.datetime.now(dt.UTC).isoformat(),
        )
        evallabel.append_record(row, path)
        return row

    skipped = labelloop.label_loop(
        queue,
        resolved,
        fetch=lambda message_id: evallabel.cache_get(message_id, review.CACHE_DIR),
        show=show,
        record=record,
        prefetch=lambda pending: labelloop.prefetch(
            get, pending, args.batch, review.CACHE_DIR
        ),
        limit=args.limit,
    )

    print(f"\n{len(resolved)}/{total} reviewed.")
    if unfetchable:
        print(f"{len(unfetchable)} could not be fetched: {' '.join(sorted(unfetchable))}")
    if skipped - unfetchable:
        others = sorted(skipped - unfetchable)
        print(f"{len(others)} skipped this sitting: {' '.join(others)}")
    print(f"{path} is up to date. It is not committed.")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    rows = load_run(args.run_id)
    try:
        report = review.score(rows, load_labels(args.run_id))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    print("\n".join(review.report_lines(report, args.run_id)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    label = sub.add_parser("label", help="review the run's messages (resumable)")
    label.add_argument("run_id", metavar="RUN_ID")
    label.add_argument(
        "--limit", type=int, default=None, help="stop after this many decisions"
    )
    label.add_argument(
        "--body-chars",
        type=int,
        default=1500,
        help=(
            "how much body to show before the truncation marker, as in "
            "label_eval.py. Informational only: judge what the email IS."
        ),
    )
    label.add_argument(
        "--batch", type=int, default=50, help="messages fetched per Gmail chunk"
    )
    label.set_defaults(func=cmd_label)

    score = sub.add_parser("score", help="print gate step 2's figures")
    score.add_argument("run_id", metavar="RUN_ID")
    score.set_defaults(func=cmd_score)

    args = parser.parse_args()
    try:
        review.review_path(args.run_id)
    except ValueError as error:
        parser.error(str(error))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
