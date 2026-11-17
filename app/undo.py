"""Undo, as pure functions: which rows, what to change, and whether it worked.

`scripts/undo_run.py` does the Gmail calls and the printing; every decision
it acts on is made here, so all of it is testable without a mock. This
module does no I/O and never refers to `modify_labels`, which keeps the
write guard's caller list at the script (Phase 3 plan, step 6).

What undo is for: reversing a RUN, not a single message. A single misfiled
message is fixed by hand in Gmail - a correction, which Phase 4 handles.
So undo always works over a time window, measured on when the agent changed
the label (the log row's timestamp), never on when the email arrived (A3).

Four parts, in the order the script uses them:

  parse_window   checks the two typed times; changes nothing
  plan_message   puts each message back as it was before the agent first
                 touched it, measured against the labels it has NOW
  orphans        labelled messages the log does not explain; reported only
  verify         after undo, checks each message is back where it started,
                 allowing for the reader's own changes in between
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from app.categories import AGENT_LABELS, INBOX, STOP_LABELS, label_term
from app.logbook import Record

# The longest window undo accepts. A required window with no default stops an
# ACCIDENTAL unscoped undo, but not `--since 2020-01-01`. Every Phase 3 write
# happens in one sitting, so a day is ample. A module constant, like
# run_pass's LIVE_LIMIT_CAP, for Phase 4 to revisit deliberately.
MAX_WINDOW = dt.timedelta(hours=24)

# Every message the agent has touched carries one of the two stop labels, so
# this finds them all. Built from STOP_LABELS, never retyped: an undo that
# sweeps only Processed misses every Agent/Error message. No date term - the
# window is labelling time, and Gmail can only filter on received time.
SWEEP_QUERY = "{" + " ".join(label_term(label) for label in STOP_LABELS) + "}"


class UnexpectedRow(ValueError):
    """An applied row whose change undo does not know how to invert safely."""


@dataclass(frozen=True)
class Window:
    since: dt.datetime  # inclusive
    until: dt.datetime  # exclusive

    def contains(self, moment: dt.datetime) -> bool:
        return self.since <= moment < self.until


def parse_window(since: str, until: str, *, now: dt.datetime) -> Window:
    """Two ISO 8601 times -> a Window, or ValueError saying what is wrong.

    A timezone is required. A naive time is refused rather than assumed to
    be UTC, because a guess an hour out would undo the wrong hour.
    """
    start, end = _parse_moment(since, "--since"), _parse_moment(until, "--until")
    if start >= end:
        raise ValueError(f"--since must be before --until ({since} >= {until})")
    if end > now:
        raise ValueError(f"--until is in the future ({until})")
    if end - start > MAX_WINDOW:
        raise ValueError(
            f"window is {end - start}, longer than the maximum {MAX_WINDOW}"
        )
    return Window(start, end)


def _parse_moment(text: str, flag: str) -> dt.datetime:
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{flag}: not an ISO 8601 time: {text!r}") from None
    if moment.tzinfo is None:
        raise ValueError(
            f"{flag}: {text!r} has no timezone - add Z or an offset like +01:00"
        )
    return moment


def applied(record: Record) -> bool:
    """A row that changed a message: live, successful, and non-empty.

    Dry runs changed nothing, and failures apply no labels by rule, so these
    are the only rows undo inverts - and the only rows that explain a label
    found on a message (see `orphans`).
    """
    return (
        not record.dry_run
        and record.error is None
        and bool(record.action.add or record.action.remove)
    )


def window_for_run(records: Iterable[Record], run_id: str) -> tuple[str, str]:
    """`--since` and `--until` covering every applied row of one run.

    Built from the log, which is what `select` reads, so the window matches
    undo's selection by construction rather than by a second account of when
    the run happened (step 8 decision Q4). The earliest applied row's
    timestamp is floored to the second; the latest, floored, plus one second,
    since `--until` is exclusive. Whole seconds rather than minutes: a
    minute rounded up would usually be in the future when printed straight
    after a run, and `parse_window` refuses that.

    Undo selects by time, not by run: another run's applied rows inside this
    window would be undone too. In Phase 3 only one manual run happens at a
    time, and `undo_run.py` prints its plan before `--apply`, so they would
    show there.

    Raises ValueError if the run has no applied rows: a dry run, or one in
    which every message failed or was skipped, changed nothing to undo.
    """
    moments = [
        record.timestamp.astimezone(dt.UTC).replace(microsecond=0)
        for record in records
        if record.run_id == run_id and applied(record)
    ]
    if not moments:
        raise ValueError(f"run {run_id} has no applied rows, so nothing to undo")
    since, until = min(moments), max(moments) + dt.timedelta(seconds=1)
    return _format_moment(since), _format_moment(until)


def undo_command(since: str, until: str, log: str | None = None) -> str:
    """The plan-only `undo_run.py` command for a window. The human adds
    `--apply` after reading the plan, so the printed line cannot write."""
    command = f"uv run python scripts/undo_run.py --since {since} --until {until}"
    return command if log is None else f"{command} --log {log}"


def _format_moment(moment: dt.datetime) -> str:
    """`2026-11-03T14:05:09Z`: what `parse_window` accepts, and short."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def select(records: Iterable[Record], window: Window) -> dict[str, list[Record]]:
    """Applied rows in the window, grouped by message, oldest first.

    Raises `UnexpectedRow` on any row that is not agent-shaped. In Phase 3
    every decision adds only `Agent/` labels and removes only INBOX, and
    that is what makes unioning several rows for one message safe: no two
    inverses can conflict. Phase 4's reconciliation will remove `Agent/`
    labels and break this shape; this check is what makes that loud.
    """
    grouped: dict[str, list[Record]] = {}
    for record in records:
        if not (applied(record) and window.contains(record.timestamp)):
            continue
        bad_adds = set(record.action.add) - AGENT_LABELS
        bad_removes = set(record.action.remove) - {INBOX}
        if bad_adds or bad_removes:
            raise UnexpectedRow(
                f"row for {record.message_id} at {record.timestamp.isoformat()} "
                f"adds {sorted(bad_adds)} / removes {sorted(bad_removes)}: "
                "only Agent/ adds and INBOX removes can be undone"
            )
        grouped.setdefault(record.message_id, []).append(record)
    for rows in grouped.values():
        rows.sort(key=lambda record: record.timestamp)
    return grouped


@dataclass(frozen=True)
class MessagePlan:
    """What undo will do to one message. Ids, ready for `modify_labels`."""

    message_id: str
    add_ids: tuple[str, ...]  # what to put back: INBOX at most
    remove_ids: tuple[str, ...]  # what to take off, strays included
    stray: tuple[str, ...]  # of remove_ids, the Agent/ labels no row added
    # Set when the log contradicts itself for this message. Undo then leaves
    # it alone, with empty add/remove, and the reason is reported.
    held: str | None = None

    @property
    def already_reverted(self) -> bool:
        """Nothing to do, so no call is made."""
        return self.held is None and not self.add_ids and not self.remove_ids


def plan_message(
    rows: Sequence[Record],
    current_ids: Collection[str],
    ids_by_name: Mapping[str, str],
) -> MessagePlan:
    """Put the message back as it was before the agent first touched it.

    Two rules, and only the second needs to look at more than one row:

    - **Agent/ labels: earliest against now.** Take off every `Agent/` label
      the message has now that it did not have before the first row. That
      covers everything the agent added, and anything the reader re-filed
      into since (a stray).
    - **INBOX: only if the agent archived it.** Put INBOX back if any row
      removed it and it is missing now. Restoring INBOX to its earliest
      state instead would override the reader: it would un-archive a
      message they archived themselves, or re-archive one they moved in.

    Planned against the labels it has now rather than replaying the log, so
    the report says what actually happened and a second run makes no calls.

    **The cross-check.** The Agent/ rule trusts `prior_labels` alone. Every
    row's adds are the effective change, so a label a row added should never
    already be in the first row's `prior_labels`. If one is, either
    `prior_labels` was recorded wrong or the reader removed a pre-existing
    `Agent/` label that the agent then re-added. Both want a person, not a
    guess, so the message is held: left untouched and reported.

    `rows` are one message's rows from `select`, oldest first.
    """
    if not rows:
        raise ValueError("plan_message needs at least one row")
    message_id = rows[0].message_id
    before = set(rows[0].prior_labels or ())
    now = set(current_ids)
    added_ids = {ids_by_name[name] for row in rows for name in row.action.add}

    contradicted = added_ids & before
    if contradicted:
        return MessagePlan(
            message_id, add_ids=(), remove_ids=(), stray=(),
            held=(
                f"a row added {label_names(contradicted, ids_by_name)}, which the "
                "message already had before the agent first touched it - check "
                "prior_labels, or whether the reader removed it in between"
            ),
        )

    take_off = (now & _agent_ids(ids_by_name)) - before
    agent_archived = any(INBOX in row.action.remove for row in rows)
    inbox = ids_by_name[INBOX]
    put_back = {inbox} if agent_archived and inbox not in now else set()

    return MessagePlan(
        message_id,
        add_ids=tuple(sorted(put_back)),
        remove_ids=tuple(sorted(take_off)),
        stray=tuple(sorted(take_off - added_ids)),
    )


def orphans(swept_ids: Collection[str], records: Iterable[Record]) -> set[str]:
    """Swept messages with no applied row anywhere in the log.

    In or out of the window: a message labelled by an earlier run is
    explained by that run's row, and is simply out of scope. An orphan has
    no row at all - the program changed its labels and then failed to log
    it - so there is no record of what it looked like before. Reported,
    never acted on.
    """
    explained = {record.message_id for record in records if applied(record)}
    return set(swept_ids) - explained


def verify(
    rows: Sequence[Record],
    before_undo: Collection[str],
    after_undo: Collection[str],
    ids_by_name: Mapping[str, str],
) -> list[str]:
    """What is wrong with one message after undo; empty when clean.

    "Back to prior_labels, ignoring the reader's changes", made computable.
    P = the first row's prior_labels, B = labels just before undo,
    A = labels after it, L = the last row's labels_after:

    - Agent/ labels: A must match P exactly. Undo owns these outright.
    - INBOX: if the reader moved the message since the agent's write
      (INBOX in B differs from INBOX in L), A keeps the reader's choice.
      Otherwise A matches P. With no labels_after, it falls back to P.
    - Everything else (stars, read state, the reader's own labels): A must
      match B. Undo never sends those ids, so this holds by construction;
      it is checked because "an inverse, not an overwrite" claims it.
    """
    if not rows:
        raise ValueError("verify needs at least one row")
    agent_ids = _agent_ids(ids_by_name)
    inbox = ids_by_name[INBOX]
    names = {label_id: name for name, label_id in ids_by_name.items()}
    prior = set(rows[0].prior_labels or ())
    last_after = rows[-1].labels_after
    before, after = set(before_undo), set(after_undo)
    problems: list[str] = []

    left = (after & agent_ids) - prior
    missing = (prior & agent_ids) - after
    if left:
        problems.append(f"Agent/ labels left on: {sorted(names[i] for i in left)}")
    if missing:
        problems.append(f"Agent/ labels it had before, now missing: {sorted(names[i] for i in missing)}")

    reader_moved = last_after is not None and (inbox in before) != (inbox in last_after)
    want_inbox = (inbox in before) if reader_moved else (inbox in prior)
    if (inbox in after) != want_inbox:
        why = "the reader's choice" if reader_moved else "its prior labels"
        state = "in the inbox" if want_inbox else "archived"
        problems.append(f"should be {state}, per {why}")

    owned = agent_ids | {inbox}
    if (after - owned) != (before - owned):
        changed = sorted((after - owned) ^ (before - owned))
        problems.append(f"labels undo does not own changed: {changed}")

    return problems


def label_names(ids: Iterable[str], ids_by_name: Mapping[str, str]) -> list[str]:
    """Ids -> names for printing, keeping any id with no name as itself."""
    names = {label_id: name for name, label_id in ids_by_name.items()}
    return sorted(names.get(label_id, label_id) for label_id in ids)


def _agent_ids(ids_by_name: Mapping[str, str]) -> set[str]:
    return {ids_by_name[name] for name in AGENT_LABELS}


class UndoReport(BaseModel):
    """One line of `data/undo/<stamp>.jsonl`: what undo did to one message.

    Not written to the classification log. Undo rows would distort every
    live metric, and that log's job is to record classifications.
    """

    timestamp: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    message_id: str
    outcome: Literal[
        "applied", "already_reverted", "held", "failed", "verified", "mismatch"
    ]
    # Names, for reading; the ids sent are recoverable from them.
    removed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    stray: tuple[str, ...] = ()
    # The timestamps of the log rows this undid, so a report line can be
    # traced back to the classification it reversed.
    row_timestamps: tuple[dt.datetime, ...] = ()
    labels_after: tuple[str, ...] | None = None
    detail: tuple[str, ...] = ()
