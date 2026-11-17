"""Reviewing one dry run as a second eval: which rows, in what order, scored how.

Phase 3 step 9 (A4). Gate step 2 reads the large dry run's predictions
against a human's call on every message. This module is the pure half: it
takes log rows and label records and returns figures. `scripts/review_run.py`
does the terminal, the fetching and the files.

**Not blind** (A5, 2026-11-04). The reviewer sees the model's category and
intended action (`suggestion`), so every figure here is an upper bound of
unknown size on what a blind review would give, and is not comparable with
Phase 2's blind dev figures. `report_lines` prints that caveat with the
figures, every time, so they cannot be quoted without it.

**The predicted action is read from the log**, not recomputed: a row's
`action.remove` holds `INBOX` exactly when the run would have archived the
message - provided the message was in the inbox when it was fetched. The
log stores the *effective* change (`decision.effective_change`), so a
message that was already archived gets no `INBOX` remove whatever was
decided. Such a row cannot be scored from the log, and `score` refuses a
run that has one (`not_in_inbox`) rather than counting it as kept.

No I/O. The two paths are constants for the script; nothing here opens them.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from app.categories import INBOX, KEEPS_INBOX, NEEDS_REVIEW, Category
from app.config import DATA_DIR
from app.evallabel import LabelRecord
from app.evalscore import wilson
from app.logbook import Record

# Neither is committed (§9, step 9 Q7). The cache is apart from the eval's
# so a review can never put a message into `eval_cache/`.
REVIEW_DIR = DATA_DIR / "reviews"
CACHE_DIR = DATA_DIR / "review_cache"

# Gate step 2's pass marks, agreed 2026-11-02 as point figures (step 9 Q6).
# Fractions, so a result exactly on the mark compares exactly.
ACTION_ACCURACY_PASS = Fraction(84, 100)
COSTLY_RATE_MAX = Fraction(2, 100)

CAVEAT = (
    "NOT BLIND (A5): the reviewer saw the model's answer. These figures are an "
    "upper bound of unknown size on a blind review's, and are not comparable "
    "with Phase 2's blind dev figures (0.900 action, 0.771 six-way)."
)

# `logbook.new_run_id`'s shape. The run id becomes a filename.
_RUN_ID = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{6}$")


def review_path(run_id: str) -> Path:
    """`data/reviews/<run_id>.jsonl`. ValueError for anything not a run id."""
    if not _RUN_ID.match(run_id):
        raise ValueError(f"not a run id: {run_id!r}")
    return REVIEW_DIR / f"{run_id}.jsonl"


def run_rows(records: Iterable[Record], run_id: str) -> dict[str, Record]:
    """One run's rows, by message id, in log order.

    ValueError if the run has two rows for one message: `run_pass` lists
    each id once, so a duplicate means the log is not what a run writes, and
    which row to score would be a guess.
    """
    rows: dict[str, Record] = {}
    for record in records:
        if record.run_id != run_id:
            continue
        if record.message_id in rows:
            raise ValueError(f"run {run_id} has two rows for {record.message_id}")
        rows[record.message_id] = record
    return rows


def ordering(message_ids: Sequence[str], run_id: str) -> list[str]:
    """The review order: a shuffle seeded from the run id (step 9 Q5).

    Shuffled so repeated senders, which the log's newest-first order puts
    side by side, are judged one at a time rather than copied from the
    previous call. Seeded from the run id so a resumed sitting keeps its
    order. A string seed is hashed by `random` itself, not by Python's
    per-process string hash, so it is stable across runs; `sorted` first
    because `shuffle` permutes by position.
    """
    ids = sorted(set(message_ids))
    random.Random(run_id).shuffle(ids)
    return ids


def not_in_inbox(rows: Mapping[str, Record]) -> list[str]:
    """Ids of rows fetched without `INBOX`, whose predicted action the log
    cannot show (see the module docstring). Fetch failures have no prior
    labels and are not counted: they applied nothing, so they kept."""
    return sorted(
        message_id
        for message_id, row in rows.items()
        if row.prior_labels is not None and INBOX not in row.prior_labels
    )


def archived(row: Record) -> bool:
    """Whether the run would have taken this message out of the inbox."""
    return INBOX in row.action.remove


def suggestion(row: Record) -> str:
    """The one line shown above the keys (step 9 Q2): source, category,
    confidence for model rows, and the action."""
    action = "archive" if archived(row) else "keep"
    if row.error is not None:
        said = "failed"
    elif row.source == "reply":
        said = "reply rule"
    elif row.source == "prefilter":
        said = "allowlist"
    elif NEEDS_REVIEW in row.action.add:
        said = "Needs Review"
    else:
        category = row.category.value if row.category else "-"
        confidence = f" {row.confidence:.3f}" if row.confidence is not None else ""
        said = f"{category}{confidence}"
    return f"model:  {said} -> {action}"


@dataclass(frozen=True)
class Proportion:
    k: int
    n: int

    @property
    def value(self) -> float | None:
        return self.k / self.n if self.n else None

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.k, self.n)


@dataclass(frozen=True)
class GateReport:
    """Gate step 2's figures for one reviewed run (step 9 Q6).

    Every proportion is over reviewed rows only; an unreviewed row counts in
    `not_reviewed` and nowhere else.
    """

    rows: int
    reviewed: int
    not_reviewed: int
    # Gated.
    action: Proportion          # predicted kept/archived matches the truth
    costly: Proportion          # archived, of truth that keeps INBOX
    # Reported, not gated.
    six_way: Proportion
    to_action_retention: Proportion
    personal_archived: int      # the watch trigger: any is one too many
    reply: Proportion           # rule_action_correct over reply rows
    reply_label_exact: int
    needs_review: int
    failures: int
    unsure: int
    # Review rows for messages not in the run; ignored by every figure.
    stray_labels: int

    @property
    def action_passes(self) -> bool:
        a = self.action
        return a.n > 0 and Fraction(a.k, a.n) >= ACTION_ACCURACY_PASS

    @property
    def costly_passes(self) -> bool:
        c = self.costly
        return c.n > 0 and Fraction(c.k, c.n) <= COSTLY_RATE_MAX

    @property
    def passes(self) -> bool:
        return self.action_passes and self.costly_passes


def score(
    rows: Mapping[str, Record], labels: Mapping[str, LabelRecord]
) -> GateReport:
    """Join the run's rows to the reviewer's calls and count.

    `labels` is `evallabel.resolve`d. A failure row applied nothing, so it
    counts as kept (step 9 Q4) and is never six-way correct. ValueError if
    any row was fetched without `INBOX` (`not_in_inbox`).
    """
    unscoreable = not_in_inbox(rows)
    if unscoreable:
        raise ValueError(
            f"{len(unscoreable)} row(s) were not in the inbox when fetched, so the "
            f"log cannot show whether the run would have archived them: "
            f"{' '.join(unscoreable[:12])}"
        )

    counts = dict.fromkeys(
        ("action", "keeps", "costly", "six_way", "to_action", "to_action_kept",
         "personal_archived", "reply", "reply_kept", "reply_exact",
         "needs_review", "failures", "unsure"),
        0,
    )
    reviewed = 0
    for message_id, row in rows.items():
        label = labels.get(message_id)
        if label is None:
            continue
        reviewed += 1
        truth = Category(label.label)
        truth_keeps = truth in KEEPS_INBOX
        gone = archived(row)

        counts["action"] += truth_keeps != gone
        counts["keeps"] += truth_keeps
        counts["costly"] += truth_keeps and gone
        counts["six_way"] += row.error is None and row.category == truth
        if truth is Category.TO_ACTION:
            counts["to_action"] += 1
            counts["to_action_kept"] += not gone
        counts["personal_archived"] += truth is Category.PERSONAL and gone
        if row.source == "reply" and row.error is None:
            counts["reply"] += 1
            counts["reply_kept"] += truth_keeps
            counts["reply_exact"] += truth is Category.PERSONAL
        counts["needs_review"] += NEEDS_REVIEW in row.action.add
        counts["failures"] += row.error is not None
        counts["unsure"] += label.unsure

    return GateReport(
        rows=len(rows),
        reviewed=reviewed,
        not_reviewed=len(rows) - reviewed,
        action=Proportion(counts["action"], reviewed),
        costly=Proportion(counts["costly"], counts["keeps"]),
        six_way=Proportion(counts["six_way"], reviewed),
        to_action_retention=Proportion(counts["to_action_kept"], counts["to_action"]),
        personal_archived=counts["personal_archived"],
        reply=Proportion(counts["reply_kept"], counts["reply"]),
        reply_label_exact=counts["reply_exact"],
        needs_review=counts["needs_review"],
        failures=counts["failures"],
        unsure=counts["unsure"],
        stray_labels=len(set(labels) - set(rows)),
    )


def report_lines(report: GateReport, run_id: str) -> list[str]:
    """The printed report, caveat first and last."""

    def show(p: Proportion) -> str:
        if not p.n:
            return f"{p.k}/{p.n}  (none)"
        lo, hi = p.interval
        return f"{p.k}/{p.n} = {p.value:.3f}  [95% {lo:.3f}-{hi:.3f}]"

    def verdict(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    r = report
    action_mark = f"needs >= {float(ACTION_ACCURACY_PASS):.2f}"
    costly_mark = f"needs <= {float(COSTLY_RATE_MAX):.2f}"
    watch = (
        "   WATCH TRIGGER: schedules the Personal prompt round (follow-up)"
        if r.personal_archived else ""
    )
    incomplete = " (incomplete review)" if r.not_reviewed else ""
    lines = [
        f"review of run {run_id}",
        CAVEAT,
        "",
        f"rows {r.rows}   reviewed {r.reviewed}   not reviewed {r.not_reviewed}",
    ]
    if r.not_reviewed:
        lines.append("  the review is incomplete: these figures are partial")
    lines += [
        "",
        "gated (pass/fail on the point estimate)",
        f"  action accuracy   {show(r.action)}   {action_mark}  {verdict(r.action_passes)}",
        f"  costly errors     {show(r.costly)}   {costly_mark}  {verdict(r.costly_passes)}",
        "      (archived, of reviewed truth that keeps INBOX)",
        "",
        "reported, not gated",
        f"  six-way accuracy        {show(r.six_way)}",
        f"  To Action retention     {show(r.to_action_retention)}",
        f"  Personal -> archive     {r.personal_archived}{watch}",
        f"  reply rule              {show(r.reply)} action correct",
        f"                          {r.reply_label_exact} label exact (Personal)",
        f"  Needs Review (reviewed) {r.needs_review}",
        f"  failures (reviewed)     {r.failures}",
        f"  marked unsure           {r.unsure}",
    ]
    if r.stray_labels:
        lines.append(f"  review rows not in the run (ignored): {r.stray_labels}")
    lines += [
        "",
        f"gate step 2: {verdict(r.passes and not r.not_reviewed)}{incomplete}",
        CAVEAT,
    ]
    return lines
