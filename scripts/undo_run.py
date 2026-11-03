#!/usr/bin/env python3
"""Reverse what the agent did in a time window. Phase 3 step 6.

    undo_run.py --since 2026-11-03T14:00Z --until 2026-11-03T14:30Z
    undo_run.py --since … --until … --apply [--verify]
    undo_run.py --since … --until … --log data/rehearsal/classifications.jsonl

`--since` and `--until` are REQUIRED, with no default (CLAUDE.md). They are
matched on the log row's timestamp, when the agent changed the label, not on
when the email arrived. At most `undo.MAX_WINDOW` apart; `--until` may not
be in the future.

Prints the plan by default. Writes only with `--apply`: one atomic
`modify_labels` per message, reversing exactly what the log says the agent
changed, measured against the labels the message has now. A message the
reader changed since keeps their changes. `--verify` then re-fetches every
message and checks it, exiting non-zero on any mismatch.

Always sweeps the whole mailbox for `Agent/Processed` and `Agent/Error`, and
reports any match with no log row as an orphan. Orphans are never touched.

Decisions live in `app/undo.py`; this script only calls Gmail and prints.
Writes its report to `data/undo/<stamp>.jsonl`, never to the classification
log. Exit 0 means clean: no failures, no mismatches, no orphans, and no
message held because its log rows contradict its prior labels.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from google.auth.exceptions import RefreshError

from app import auth, config, gmail_client, logbook, undo
from app.categories import AGENT_LABELS, INBOX

REPORT_DIR = config.DATA_DIR / "undo"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", required=True, help="ISO 8601 with timezone, inclusive")
    parser.add_argument("--until", required=True, help="ISO 8601 with timezone, exclusive")
    parser.add_argument("--apply", action="store_true", help="make the changes")
    parser.add_argument("--verify", action="store_true", help="after --apply, check every message")
    parser.add_argument(
        "--log", type=Path, default=None,
        help="the classification log to read (default: the real one)",
    )
    args = parser.parse_args()
    if args.verify and not args.apply:
        parser.error("--verify checks what --apply did, so it needs --apply")

    # 1. The window, before any Gmail call.
    try:
        window = undo.parse_window(args.since, args.until, now=dt.datetime.now(dt.UTC))
    except ValueError as error:
        parser.error(str(error))

    # 2-3. Scope as granted, then every label id. Both fail closed.
    if args.apply:
        auth.require_scopes(auth.MODIFY)
    else:
        auth.require_scopes(auth.READONLY)
    svc = gmail_client.service()
    ids_by_name = gmail_client.label_ids(svc, sorted(AGENT_LABELS | {INBOX}))

    # 4. The log. A skipped line could be a row that should be undone.
    log_path = args.log if args.log is not None else config.LOG_PATH
    records, skipped = logbook.read_all(log_path)
    print(f"Log: {log_path} - {len(records)} rows, {skipped} unreadable")
    if skipped and args.apply:
        print("Refusing --apply: an unreadable line could be a row that needs undoing.")
        return 1

    # 5. The rows to undo. A row undo cannot invert stops everything here.
    try:
        selected = undo.select(records, window)
    except undo.UnexpectedRow as error:
        print(f"Refusing to run: {error}")
        return 1
    print(f"Window: {window.since.isoformat()} to {window.until.isoformat()} - "
          f"{sum(map(len, selected.values()))} rows, {len(selected)} messages\n")

    # 6. The sweep: report only.
    orphan_ids = sorted(undo.orphans(gmail_client.search_ids(svc, undo.SWEEP_QUERY), records))

    # 7. Plan each message against its labels now.
    plans: dict[str, undo.MessagePlan] = {}
    before: dict[str, tuple[str, ...]] = {}
    failures = 0
    for message_id, rows in selected.items():
        try:
            message = gmail_client.fetch(svc, message_id)
        except (gmail_client.NotAuthenticated, RefreshError):
            raise  # an auth failure halts the run (DESIGN.md -> Error handling)
        except Exception as error:  # noqa: BLE001 - one message must not stop the rest
            failures += 1
            print(f"  {message_id}  FETCH FAILED: {error}")
            continue
        before[message_id] = message.label_ids
        plans[message_id] = undo.plan_message(rows, message.label_ids, ids_by_name)
        print(_describe(plans[message_id], message.subject, ids_by_name))
    held = [plan for plan in plans.values() if plan.held]

    _print_orphans(orphan_ids)

    if not args.apply:
        print("\nPlan only. Rerun with --apply to make these changes.")
        return 0 if not (failures or orphan_ids or held) else 1

    # 8. Apply: one atomic call per message, and its report line right after.
    report_path = REPORT_DIR / f"{logbook.new_run_id()}.jsonl"
    print(f"\nApplying. Report: {report_path}")
    changed = 0
    for message_id, plan in plans.items():
        rows = selected[message_id]
        entry = {
            "message_id": message_id,
            "removed": tuple(undo.label_names(plan.remove_ids, ids_by_name)),
            "added": tuple(undo.label_names(plan.add_ids, ids_by_name)),
            "stray": tuple(undo.label_names(plan.stray, ids_by_name)),
            "row_timestamps": tuple(row.timestamp for row in rows),
        }
        if plan.held:
            _report(report_path, undo.UndoReport(outcome="held", detail=(plan.held,), **entry))
            continue
        if plan.already_reverted:
            _report(report_path, undo.UndoReport(outcome="already_reverted", **entry))
            continue
        try:
            after = gmail_client.modify_labels(
                svc, message_id, add_ids=plan.add_ids, remove_ids=plan.remove_ids
            )
        except (gmail_client.NotAuthenticated, RefreshError):
            raise
        except Exception as error:  # noqa: BLE001
            failures += 1
            print(f"  {message_id}  MODIFY FAILED: {error}")
            _report(report_path, undo.UndoReport(outcome="failed", detail=(str(error),), **entry))
            continue
        changed += 1
        _report(report_path, undo.UndoReport(outcome="applied", labels_after=after, **entry))
    reverted = sum(plan.already_reverted for plan in plans.values())
    print(f"  {changed} changed, {reverted} already reverted, "
          f"{len(held)} held, {failures} failed")

    # 9. Verify: re-fetch, and check against the rule in undo.verify.
    mismatches = 0
    if args.verify:
        print("\nVerifying:")
        verified = 0
        for message_id, plan in plans.items():
            if plan.held:
                continue  # untouched on purpose; nothing to verify
            try:
                now_ids = gmail_client.fetch(svc, message_id).label_ids
            except (gmail_client.NotAuthenticated, RefreshError):
                raise
            except Exception as error:  # noqa: BLE001
                failures += 1
                print(f"  {message_id}  RE-FETCH FAILED: {error}")
                continue
            problems = undo.verify(selected[message_id], before[message_id], now_ids, ids_by_name)
            outcome = "mismatch" if problems else "verified"
            mismatches += bool(problems)
            verified += not problems
            print(f"  {message_id}  {'MISMATCH: ' + '; '.join(problems) if problems else 'ok'}")
            _report(report_path, undo.UndoReport(
                message_id=message_id, outcome=outcome, labels_after=now_ids, detail=tuple(problems),
            ))
        print(f"  {verified} ok, {mismatches} mismatched")

    clean = not (failures or mismatches or orphan_ids or held)
    print("\nClean." if clean else "\nNot clean - see above.")
    return 0 if clean else 1


def _describe(plan: undo.MessagePlan, subject: str, ids_by_name: dict[str, str]) -> str:
    head = f"  {plan.message_id}  {subject[:50]!r}"
    if plan.held:
        return f"{head}\n      HELD, not touched: {plan.held}"
    if plan.already_reverted:
        return f"{head}\n      already reverted - nothing to do"
    lines = [head]
    if plan.remove_ids:
        lines.append(f"      remove    {', '.join(undo.label_names(plan.remove_ids, ids_by_name))}")
    if plan.add_ids:
        lines.append(f"      add back  {', '.join(undo.label_names(plan.add_ids, ids_by_name))}")
    if plan.stray:
        lines.append(f"      (stray, not added by the agent: {', '.join(undo.label_names(plan.stray, ids_by_name))})")
    return "\n".join(lines)


def _print_orphans(orphan_ids: list[str]) -> None:
    if not orphan_ids:
        print("\nOrphans: none.")
        return
    print(f"\nOrphans: {len(orphan_ids)} labelled messages with no log row. Not touched:")
    for message_id in orphan_ids:
        print(f"  {message_id}  https://mail.google.com/mail/u/0/#all/{message_id}")


def _report(path: Path, entry: undo.UndoReport) -> None:
    """Append one line, right after the change it records, so a crash
    mid-run leaves a true account of what was done."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.model_dump_json() + "\n")


if __name__ == "__main__":
    sys.exit(main())
