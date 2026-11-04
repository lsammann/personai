#!/usr/bin/env python3
"""Classify and label a batch of received mail. Phase 3 step 8.

    run_pass.py --limit 50                               dry run: logs, writes nothing
    run_pass.py --limit 300 --query "after:2026/09/11"   the gate's large dry run
    run_pass.py --limit 12 --query newer_than:1d --live  labels real mail

A DRY RUN unless `--live`. A dry run does everything a live run does except
the one `modify_labels` call, so its log rows say exactly what a live run
would have done. `config.dry_run` is not read: in Phase 3 the flag on the
command line is the only way to write, so every write is in shell history.

`--limit` is required. With `--live` it is at most `agent.LIVE_LIMIT_CAP`.
The query is always received mail not yet `Agent/Processed` or `Agent/Error`
(`agent.queue_query`); `--query` only narrows it.

Before the first message, one synthetic classification checks that Ollama
answers, so a run with the model down stops before logging anything. The
ids are listed once, up front; a message labelled since is skipped by the
agent. Errors about the whole mailbox (auth, any 403), transport errors and
bugs halt the run. The summary still prints, and after a live run so does
the `undo_run.py` command for its window, built from the log.

Exit 0 when no message failed, 1 otherwise. Every decision is made in
`app/agent.py`; this script only wires it up and prints.
"""

from __future__ import annotations

import argparse
import sys
import time

from app import agent, auth, classifier, config, gmail_client, logbook, prefilter, undo
from app.categories import REQUIRED_LABELS
from app.logbook import Record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, required=True, help="most messages to take")
    parser.add_argument("--query", default=None, help="Gmail terms that narrow the queue")
    parser.add_argument("--live", action="store_true", help="label the messages")
    args = parser.parse_args()

    # 1. The limit, before any network call.
    try:
        agent.check_limit(args.limit, live=args.live)
    except ValueError as error:
        parser.error(str(error))

    # 2. Scope as granted. A dry run needs only read access.
    auth.require_scopes(auth.MODIFY if args.live else auth.READONLY)
    days_left = auth.status().get("days_remaining")

    # 3. Everything resolved once per run. label_ids fails closed.
    cfg = config.load()
    allowlist = prefilter.load_allowlist()
    svc = gmail_client.service()
    ids_by_name = gmail_client.label_ids(svc, sorted(REQUIRED_LABELS))
    run_id = logbook.new_run_id()
    query = agent.queue_query(args.query)

    # 4. The header: everything that decided this run's rows.
    prompt_id = classifier.DEFAULT_PROMPT_ID
    print(f"{'LIVE' if args.live else 'DRY RUN'}  run_id={run_id}")
    print(f"  model={cfg.ollama_model}  prompt={prompt_id} "
          f"({classifier.prompt_hash(prompt_id)})  body_chars={cfg.body_chars}  "
          f"T={cfg.confidence_threshold}  F={cfg.to_action_floor}")
    print(f"  query: {query}")
    print(f"  limit={args.limit}  allowlist={len(allowlist)} domains  "
          f"token days left={days_left if days_left is not None else 'unknown'}")

    # 5. Ollama must answer before anything is fetched or logged.
    print(f"  preflight: {agent.preflight(cfg):.1f}s (includes any model load)\n")

    # 6. One listing, up front.
    message_ids = gmail_client.search_ids(svc, query, args.limit)
    print(f"{len(message_ids)} messages listed\n")

    # 7-8. One message at a time; the summary prints even on a halt.
    tally = agent.RunTally()
    try:
        for n, message_id in enumerate(message_ids, start=1):
            started = time.perf_counter()
            row = agent.classify_and_label(
                svc, message_id,
                cfg=cfg, ids_by_name=ids_by_name, allowlist=allowlist,
                run_id=run_id, run_kind="manual", dry_run=not args.live,
            )
            seconds = time.perf_counter() - started
            tally.add(row, seconds)
            print(_describe(n, len(message_ids), message_id, row, seconds, live=args.live))
    finally:
        _summarise(tally, run_id, live=args.live)

    # 9.
    return 0 if not tally.failures else 1


def _describe(n: int, total: int, message_id: str, row: Record | None,
              seconds: float, *, live: bool) -> str:
    head = f"{n:>3}/{total}  {message_id}"
    if row is None:
        return f"{head}  skipped: already Processed or Error\n"
    sender = (row.sender or "?")[:40]
    subject = (row.subject or "")[:60]
    lines = [f"{head}  {sender}", f"         {subject!r}"]
    if row.error is not None:
        lines.append(f"         FAILED  {row.error}  {seconds:.1f}s")
    else:
        category = row.category.value if row.category is not None else "-"
        confidence = f" {row.confidence:.3f}" if row.source == "model" else ""
        change = " ".join(
            [f"+{name}" for name in row.action.add] + [f"-{name}" for name in row.action.remove]
        ) or "no change"
        verb = "" if live else "would: "
        lines.append(f"         {row.source:9} {category}{confidence}  "
                     f"{verb}{change}  {seconds:.1f}s")
    if row.note:
        lines.append(f"         note: {row.note}")
    return "\n".join(lines) + "\n"


def _summarise(tally: agent.RunTally, run_id: str, *, live: bool) -> None:
    print(f"run {run_id}: {tally.messages} messages - {tally.classified} classified, "
          f"{tally.skipped} skipped, {tally.failures} failed")
    if tally.by_source:
        print("  by source:   " + ", ".join(f"{s}={c}" for s, c in sorted(tally.by_source.items())))
    if tally.by_category:
        print("  by category: " + ", ".join(
            f"{c.value}={n}" for c, n in sorted(tally.by_category.items())
        ))
    print(f"  needs review: {tally.needs_review}")
    if tally.seconds:
        print(f"  seconds/message: mean {sum(tally.seconds) / len(tally.seconds):.1f}, "
              f"slowest {max(tally.seconds):.1f}; {tally.per_minute:.0f} messages/minute")
    print(f"  log: {config.LOG_PATH}")
    if not live:
        print("  dry run: nothing was written to Gmail.")
        return
    # Built from the log, which is what undo reads (step 8 Q4).
    records, _ = logbook.read_all(config.LOG_PATH)
    try:
        since, until = undo.window_for_run(records, run_id)
    except ValueError:
        print("  nothing was applied, so there is nothing to undo.")
        return
    print("  to undo this run (prints its plan; add --apply to act):")
    print(f"    {undo.undo_command(since, until)}")


if __name__ == "__main__":
    sys.exit(main())
