"""The classification log: one JSON object per attempt, appended to a file.

Guiding principle from DESIGN.md: record the decision's inputs AND its
parameters. A row should carry enough to recompute why a message was labelled
the way it was, and to segment any metric by anything that was tuned. Anything
tunable that is not logged becomes a variable that cannot be controlled for
later - if the model changes mid-backfill and the rows do not say which
produced them, the Metrics view silently averages across two models.

This is not a database and does not undermine the no-DB decision: an
append-only file with no migrations and no queries beyond "read it all and
aggregate". It exists because three things are impossible without it -
threshold tuning (Gmail labels retain none of the distribution), dry-run
inspection (a dry run writes no labels by definition), and the correction
corpus that Phase 2's tuning and any future few-shot work both feed on.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.categories import Category
from app.config import LOG_PATH

# Where a row's ANSWER came from. Orthogonal to `run_kind` below.
Source = Literal["model", "prefilter", "correction"]

# Which INVOCATION produced the row. `manual` is the CLI entry point - trying
# the thing out on a handful of emails - which otherwise lands in the same file
# as real traffic and quietly skews metrics like "% hitting Needs Review".
# `eval` keeps scoring runs out of the live numbers entirely.
RunKind = Literal["poll", "backfill", "eval", "manual"]


class Action(BaseModel):
    """The label change, always populated - including on a dry run.

    DESIGN.md originally defined `action` as "labels added/removed, OR
    dry_run", which destroys exactly what a dry run exists to show: what it
    WOULD have done. The intent and whether it was applied are two facts, so
    they are two fields - see `Record.dry_run`.
    """

    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


class Record(BaseModel):
    timestamp: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.UTC)
    )

    # Run identity. Stamped on every row of one invocation, so a hand-run over
    # twenty emails is separable from the overnight backfill afterwards. Added
    # now rather than later because it is uniquely lossy - a field added in a
    # later phase is absent from every row written before it.
    run_id: str
    run_kind: RunKind

    message_id: str
    sender: str = ""
    subject: str = ""

    source: Source

    category: Category | None = None
    confidence: float | None = None
    # All six probabilities, not just the winner. The runner-up drives the
    # asymmetric To Action rule, and a saturated distribution - the failure
    # mode that killed qwen2.5:3b in Phase 0 - is only visible here.
    distribution: dict[Category, float] = Field(default_factory=dict)
    # Share of the returned top-20 that sat on category letters at all, before
    # renormalising. `confidence` is conditional on the answer being a category
    # letter, so a barely-engaged response reports the same confidence as a
    # certain one - this is the only field that separates them. None for
    # prefilter hits and corrections, which never called the model.
    retained_mass: float | None = None

    # The three tuning knobs Phase 2 iterates on. Without them in the row, a
    # mixed log cannot be segmented and the eval numbers become uninterpretable
    # the first time something changes mid-run.
    model: str | None = None
    prompt_version: int | None = None
    body_chars: int | None = None

    # The thresholds in force, so a past decision can be recomputed against the
    # values that actually produced it.
    confidence_threshold: float | None = None
    to_action_floor: float | None = None

    action: Action = Field(default_factory=Action)
    dry_run: bool = False

    # Failure reason, None on success. A row with an error and no labels is the
    # retry path; a row with an error is never also a classification.
    error: str | None = None
    # e.g. a category letter falling outside the returned top-20.
    note: str | None = None


def new_run_id() -> str:
    """A sortable, human-readable run identifier.

    Timestamp-prefixed so runs order chronologically when read back by eye, with
    a short random suffix so two runs started in the same second cannot collide.
    One function so the poller, the backfill, the eval harness and the CLI
    cannot invent four formats between them.
    """
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def append(record: Record, path: Path | None = None) -> None:
    """Append one row. Writes are strict - a malformed row never gets written.

    `path=None` resolves LOG_PATH at call time rather than binding it as a
    default argument, which would freeze the module-level value at import and
    make the test suite's redirect into tmp_path silently ineffective.
    """
    path = LOG_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record.model_dump_json() + "\n")


def read_all(path: Path | None = None) -> tuple[list[Record], int]:
    """Every readable row, plus a count of the ones that could not be parsed.

    Reads are tolerant where writes are strict. The log outlives prompt-version
    bumps and schema edits, and it is the only thing the Metrics view reads -
    so one stale or half-written line must degrade the numbers slightly rather
    than take the page down. The skipped count is returned rather than swallowed
    so the caller can surface it instead of quietly under-reporting.
    """
    path = LOG_PATH if path is None else path
    if not path.exists():
        return [], 0

    records: list[Record] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(Record.model_validate_json(line))
        except (ValueError, json.JSONDecodeError):
            skipped += 1
    return records, skipped
