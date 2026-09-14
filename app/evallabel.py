"""Hand-labelling the eval set: records, corrections, the cache, rendering.

No terminal, no network. The keypress loop lives in `scripts/label_eval.py`;
everything here is a function of its arguments, which is what makes the parts
that would otherwise only be exercised by 200 keystrokes at 11pm testable -
resume, correction, and the truncation marker.

Two things in here are guarantees rather than conveniences.

**The committed file carries no content.** `eval/labeled.jsonl` holds an id, a
category, a flag and two stamps. `LABEL_KEYS` is checked on every write, the
same way `evalset.SAMPLE_KEYS` is, because "never commit content" is an
invariant and an invariant gets a guard.

**The cache is exactly what the model will read.** `Cached` carries the four
fields `classifier.classify()` takes and nothing else that reaches the model,
so the eval measures the pipeline that will actually run rather than a
lookalike. It stores the raw text parts rather than a finished body - per A1,
`select_body` runs fresh at eval time, so bumping `EXTRACTION_VERSION` can be
re-scored against the same cache instead of forcing a refetch of 200 messages.

Paths are parameters with no defaults. `scripts/label_eval.py` supplies them,
which means nothing here can write into the real `data/` because a test forgot
to redirect it.

See `docs/PHASE2_PLAN.md` -> Step 3 implementation for the labelling pass
and Step 7 for the blind recheck, which is pass 2 over the same file.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.categories import KEEPS_INBOX, Category
from app.evalset import EVAL_DIR, Sampled
from app.gmail_client import Message
from app.message_body import select_body, strip_html

LABELED_PATH = EVAL_DIR / "labeled.jsonl"

# Committed: ids, a category and provenance. No sender, subject, body, snippet
# or domain. Asserted on write and by a test.
LABEL_KEYS = frozenset({"message_id", "label", "unsure", "labelled_at", "pass"})

# The presentation order is drawn from its own offset of the sample seed, as
# the dev/holdout split is (`evalset.SPLIT_SEED_OFFSET`). One number still
# reproduces the whole plan, and changing one draw cannot silently reshuffle
# the other.
ORDER_SEED_OFFSET = 2

# Step 7's blind recheck. Its own seed, distinct from the sample's, for the
# reason `ORDER_SEED_OFFSET` exists: one number reproduces the whole plan and no
# draw can silently reshuffle another. 30 rather than the 20 §2 sketched - at
# n=20 the Wilson interval on the ceiling is about +/-0.15, and a uniform draw
# that size would likely contain no `Bookings` at all.
RECHECK_PATH = EVAL_DIR / "recheck.json"
RECHECK_PASS = 2
DEFAULT_RECHECK_N = 30
DEFAULT_RECHECK_SEED = 99

# Ids and provenance, no content - the same rule as `LABEL_KEYS`, asserted on
# write for the same reason.
RECHECK_KEYS = frozenset({"n", "seed", "created_at", "message_ids"})

# Digit -> category, derived from the enum rather than listed again here. A
# second ordering would be free to drift from `Category`, and the drift would
# show up as mislabelled ground truth, which is the one error this project
# cannot measure its way out of.
KEYS: Mapping[str, Category] = {
    str(i + 1): category for i, category in enumerate(Category)
}

# Gmail ids are lowercase hex, but a cache filename is composed from whatever
# is passed in, so the character set is checked rather than assumed.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Terminal width for the body. Wrapped per line rather than as one block:
# `select_body` leaves plain text's own line breaks intact because they carry
# structure - an amount and a due date on their own lines - and reflowing the
# whole body would throw that away for the 77% of mail that has it.
WRAP = 78


@dataclass(frozen=True)
class LabelRecord:
    """One decision. `label` is a `Category`, so an unknown one cannot be written.

    `pass_no` is `pass` in the file - `pass` is a keyword. Pass 1 is the
    labelling sweep; pass 2 is step 7's blind recheck, which is a separate
    measurement of self-consistency rather than a set of corrections to pass 1.
    That is why `resolve` never looks across passes.
    """

    message_id: str
    label: Category
    unsure: bool
    labelled_at: str
    pass_no: int = 1


@dataclass(frozen=True)
class Cached:
    """One fetched message, on disk, gitignored.

    Why cache at all: refetching every eval run would make the measurement
    depend on the mailbox's state that day, and a deleted message would change
    the denominator silently between two runs being compared.

    `internal_date` is the one field here the model never sees. It exists for
    the arrived-at rule in the labelling UI - label what it should have been
    when it arrived - and must not drift into `predict`.
    """

    message_id: str
    sender: str
    subject: str
    internal_date: str
    text_plain: str
    text_html: str


# --- order and progress --------------------------------------------------


def ordering(sample: Sequence[Sampled], seed: int) -> list[str]:
    """The order messages are presented in. Randomised across strata.

    Randomised because the strata are not interchangeable: labelling all 37
    `S_action` messages in a row means judging "is this actionable?" 37 times
    with the previous 36 as context, and the answers would drift together.
    Deterministic from the seed so that resuming a session keeps the order it
    started with.

    `sorted` before the shuffle, for the reason `evalset.build_plan` sorts
    before every draw: Python randomises string hashing per process, so any
    order inherited from a set or a file read is not stable across runs, and
    `shuffle` permutes by position.
    """
    ids = sorted(row.message_id for row in sample)
    random.Random(seed + ORDER_SEED_OFFSET).shuffle(ids)
    return ids


def resolve(
    records: Sequence[LabelRecord], pass_no: int = 1
) -> dict[str, LabelRecord]:
    """The current label for each message: last row in FILE order wins.

    File order, not timestamp - a correction made within the same second as
    the original would tie on a timestamp, and `b` (back) exists precisely to
    be used immediately.

    Within one pass only. A pass 2 row is a second independent judgement, not a
    correction of pass 1, and collapsing the two would destroy the
    self-consistency measurement step 7 exists to make.
    """
    return {
        record.message_id: record
        for record in records
        if record.pass_no == pass_no
    }


def pending(
    order: Sequence[str],
    resolved: Mapping[str, LabelRecord],
    skipped: Iterable[str] = (),
) -> list[str]:
    """What is left to label, in presentation order.

    `skipped` is the current sitting's `s` presses. They are deliberately not
    persisted: a skip is "not now", so the next sitting offers the message
    again, and if it is still unlabelled at the end `verify` reports it as the
    taxonomy question it probably is.
    """
    done = set(resolved) | set(skipped)
    return [message_id for message_id in order if message_id not in done]


def unfinished(
    records: Sequence[LabelRecord],
    sample_ids: Iterable[str],
    pass_no: int = 1,
) -> list[str]:
    """Sample ids with no row in `pass_no`. Empty means that pass is complete.

    The pass-2 guard. A blind recheck run before pass 1 finishes would draw its
    20 messages from a partly-labelled set, so its disagreement rate would
    measure the order things happened to be labelled in as much as the
    labeller's consistency.
    """
    resolved = resolve(records, pass_no)
    return [
        message_id for message_id in sample_ids if message_id not in resolved
    ]


def counts_by_stratum(
    sample: Sequence[Sampled], resolved: Mapping[str, LabelRecord]
) -> dict[str, dict[str, int]]:
    """Sampled vs labelled per stratum - the `n_h` step 5 actually weights by.

    `strata.json` records what was *sampled*. If any message ends up
    unlabelled, the real `n_h` is smaller, and weighting by the sampled figure
    would overstate that stratum's share of the mailbox estimate in proportion
    to the gap. `run_eval` derives `n_h` from the labelled set; this is what
    lets `verify` show the difference before it matters.
    """
    counts: dict[str, dict[str, int]] = {}
    for row in sample:
        cell = counts.setdefault(row.stratum, {"sampled": 0, "labelled": 0})
        cell["sampled"] += 1
        if row.message_id in resolved:
            cell["labelled"] += 1
    return counts


# --- the blind recheck ---------------------------------------------------


@dataclass(frozen=True)
class RecheckDraw:
    """Which messages pass 2 re-presents, pinned to disk.

    Pinned rather than recomputed from the CLI defaults every time, for A4's
    reason one level down: a draw derived from flags is a draw that moves
    silently the day someone types a different `--n`, and `verify --pass 2`
    would then check completeness against a set nobody labelled.
    """

    n: int
    seed: int
    created_at: str
    message_ids: tuple[str, ...]


@dataclass(frozen=True)
class Disagreement:
    """One message the two passes labelled differently.

    `crosses_inbox` is the expensive kind: the two judgements disagree about
    whether the message stays in the inbox, so the disagreement bounds action
    accuracy and not just the 6-way number. A `To Action` / `Personal` flip
    does not cross; `Bookings` / `Receipts` does.
    """

    message_id: str
    first: Category
    second: Category
    unsure: bool
    crosses_inbox: bool


@dataclass(frozen=True)
class Agreement:
    """The self-consistency ceiling, as counts. Intervals are the caller's job.

    Two ceilings rather than one, because the harness reports two headline
    numbers: `n_agree / n` bounds `accuracy`, and `n_action_agree / n` bounds
    the collapsed action accuracy, which is the metric with consequences.
    """

    n: int
    n_agree: int
    n_action_agree: int
    n_unsure: int
    n_unsure_agree: int
    disagreements: tuple[Disagreement, ...]


def recheck_draw(sample_ids: Iterable[str], n: int, seed: int) -> list[str]:
    """`n` of the sample, uniformly at random. Shuffle first, then take.

    Uniform over the whole sample, not stratified by the pass-1 label and not
    restricted to the dev split. Stratifying would over-represent the rare
    categories and bias the headline in a direction nothing here could
    correct for; restricting to dev would measure a procedure that was applied
    identically to both splits and then not apply to the holdout, which is the
    instrument step 8 needs it for.

    Shuffle-then-take rather than `sample`, so the draw has the **prefix
    property**: `n=40` is the `n=30` draw plus ten more. Extending an
    ambiguous result then costs ten messages instead of a fresh draw, and the
    rows already recorded stay valid.

    `sorted` first, as everywhere else here: string hashing is randomised per
    process, so any order inherited from a set or a file read is not stable.
    """
    ids = sorted(sample_ids)
    random.Random(seed).shuffle(ids)
    return ids[:n]


def agreement(
    first: Mapping[str, LabelRecord], second: Mapping[str, LabelRecord]
) -> Agreement:
    """Compare two independent passes over the same messages.

    Raw agreement, deliberately: it is the quantity that directly bounds
    accuracy, which is what the ceiling is for. Cohen's kappa was rejected -
    it corrects for chance using the estimated marginals, and at n=30 over six
    categories those marginals are noisier than the statistic they adjust.

    A pass-2 id with no pass-1 row raises rather than being skipped. That
    condition means `labeled.jsonl` is damaged, and quietly shrinking the
    denominator would flatter the ceiling by exactly the number of rows lost.
    """
    orphans = sorted(set(second) - set(first))
    if orphans:
        raise ValueError(
            f"pass-2 rows with no pass-1 row: {' '.join(orphans)}"
        )

    disagreements: list[Disagreement] = []
    n_agree = n_action_agree = n_unsure = n_unsure_agree = 0
    for message_id, later in second.items():
        earlier = first[message_id]
        same = earlier.label == later.label
        unsure = earlier.unsure or later.unsure
        crosses = (earlier.label in KEEPS_INBOX) != (later.label in KEEPS_INBOX)

        n_agree += same
        n_action_agree += not crosses
        n_unsure += unsure
        n_unsure_agree += unsure and same
        if not same:
            disagreements.append(
                Disagreement(
                    message_id=message_id,
                    first=earlier.label,
                    second=later.label,
                    unsure=unsure,
                    crosses_inbox=crosses,
                )
            )

    return Agreement(
        n=len(second),
        n_agree=n_agree,
        n_action_agree=n_action_agree,
        n_unsure=n_unsure,
        n_unsure_agree=n_unsure_agree,
        disagreements=tuple(disagreements),
    )


def save_draw(draw: RecheckDraw, path: Path) -> None:
    """Pin the draw. Ids and provenance only, checked on write."""
    row = {
        "n": draw.n,
        "seed": draw.seed,
        "created_at": draw.created_at,
        "message_ids": list(draw.message_ids),
    }
    extra = set(row) - RECHECK_KEYS
    if extra:
        raise ValueError(f"refusing to write non-allowlisted keys: {sorted(extra)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_draw(path: Path) -> RecheckDraw | None:
    """The pinned draw, or None if it was never made.

    Unreadable **raises** here, where an unreadable cache entry reads as
    absent. The cache is derived data and refetching costs a second; the draw
    is the measurement plan, and treating damage as "never drawn" would
    silently replace it with a fresh one and reset the recheck mid-sitting.
    """
    if not path.exists():
        return None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        return RecheckDraw(
            n=int(row["n"]),
            seed=int(row["seed"]),
            created_at=str(row["created_at"]),
            message_ids=tuple(row["message_ids"]),
        )
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"{path} is unreadable: {error}") from error


# --- rendering -----------------------------------------------------------


def age(when: dt.datetime, now: dt.datetime) -> str:
    """A rough human age. Precision past "months" buys nothing here."""
    days = max((now - when).days, 0)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 60:
        return f"{days} days ago"
    if days < 730:
        return f"{days // 30} months ago"
    return f"{days // 365} years ago"


def render(
    message: Cached,
    body_chars: int,
    show_full: bool = False,
    *,
    now: dt.datetime | None = None,
    position: int | None = None,
    total: int | None = None,
) -> str:
    """The message as the labeller sees it.

    The body is `select_body()`'s output - what the model will actually read -
    so a labelling decision is made against the same text, minus the length
    limit.

    **The truncation marker is informational only.** Ground truth is what the
    email *is*, not what the model can see. If the target moved with
    `body_chars`, every truncation change would silently redefine what is being
    measured and `body_chars` would stop being tunable at all. `m` shows the
    rest for exactly that reason.

    `show_full` shows everything the message contains, not just the rest of
    the selected part - the other text part follows it under a divider. That
    is the same rule stated the other way round: if the body policy picks the
    wrong part, the *model* is blind and that is a measurement to be fixed,
    but the *labeller* must not be, or ground truth silently becomes "what the
    extractor happened to select".

    `Cached` carries no stratum, so this cannot leak one onto the screen even
    by accident - `S_action` in front of the labeller is a direct hint at
    `To Action`, which would put the keyword mining into the ground truth it is
    supposed to be measured against.
    """
    now = now or dt.datetime.now(dt.UTC)
    selection = select_body(message.text_plain, message.text_html)
    body = selection.text
    shown = body if show_full else body[:body_chars]
    wrapped = wrap(shown)

    when = _parse_date(message.internal_date)
    head = f"[{position}/{total}]" if position and total else ""
    stamp = f"{when:%Y-%m-%d}  ({age(when, now)})" if when else message.internal_date

    lines = [
        f"{head:<20}{stamp:>58}".rstrip(),
        f"From:     {message.sender}",
        f"Subject:  {message.subject}",
        "",
        wrapped if shown else "(no body text)",
    ]
    if len(shown) < len(body):
        lines.append(f"\n[{len(shown):,} of {len(body):,} chars - m for more]")
    elif show_full and body:
        lines.append(f"\n[{len(body):,} chars - all of it]")

    if show_full:
        name, other = alternate(message, selection.source)
        if other:
            lines.append(f"\n--- also in the {name} part, {len(other):,} chars ---")
            lines.append(wrap(other))
    if not selection.parsed_ok:
        lines.append("[html parse failed partway - body may be incomplete]")
    return "\n".join(lines)


def alternate(message: Cached, source: str) -> tuple[str, str]:
    """The text part `select_body` did NOT choose, as words.

    Shown on `m` with no materiality test: `m` means "show me everything", and
    a threshold here would be one more place for the real content to hide. On
    the 77% of mail where both parts say the same thing it is visible
    duplication, which is the harmless direction to err in.
    """
    if source in ("plain", "plain_markup"):
        return "html", strip_html(message.text_html)[0].strip()
    if source in ("html", "stub_fallback"):
        return "plain", message.text_plain.strip()
    return "", ""


def wrap(text: str, width: int = WRAP) -> str:
    """Wrap long lines, keep existing ones. Truncation already happened."""
    return "\n".join(
        textwrap.fill(line, width) if line.strip() else line
        for line in text.splitlines()
    )


def _parse_date(raw: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


# --- persistence ---------------------------------------------------------


def to_row(record: LabelRecord) -> dict[str, object]:
    """The committed shape. `pass_no` is `pass` on disk."""
    return {
        "message_id": record.message_id,
        "label": Category(record.label).value,
        "unsure": record.unsure,
        "labelled_at": record.labelled_at,
        "pass": record.pass_no,
    }


def append_record(record: LabelRecord, path: Path) -> None:
    """Append one decision, flushed. Strict: a bad row is never written.

    Opened and closed per record rather than held open for the session, which
    is the cheapest possible guarantee that a crash forty minutes in costs zero
    labels. Two hundred opens is nothing against two hours of human attention.
    """
    row = to_row(record)
    extra = set(row) - LABEL_KEYS
    if extra:
        raise ValueError(f"refusing to write non-allowlisted keys: {sorted(extra)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def load_records(path: Path) -> tuple[list[LabelRecord], int]:
    """Every readable row, plus how many could not be read.

    Tolerant where writes are strict, the same split as `logbook.read_all`: a
    half-written trailing line from a `Ctrl-C` mid-append must cost one label,
    not the session. The count is returned rather than swallowed so `verify`
    can report damage instead of quietly labelling one message twice.
    """
    if not path.exists():
        return [], 0
    records: list[LabelRecord] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            records.append(
                LabelRecord(
                    message_id=row["message_id"],
                    label=Category(row["label"]),
                    unsure=bool(row["unsure"]),
                    labelled_at=row["labelled_at"],
                    pass_no=int(row.get("pass", 1)),
                )
            )
        except (ValueError, KeyError, TypeError):
            skipped += 1
    return records, skipped


# --- the message cache ---------------------------------------------------


def cache_path(message_id: str, directory: Path) -> Path:
    if not _SAFE_ID.match(message_id):
        raise ValueError(f"refusing to build a cache path from {message_id!r}")
    return directory / f"{message_id}.json"


def cache_put(message: Message, directory: Path) -> None:
    """Store one message. Written whole or not at all.

    Temp file then `os.replace`, which is atomic on POSIX: a `Ctrl-C` during
    the write leaves either the old entry or none, never a truncated one. A
    truncated entry is the bad case because it reads back as a real message
    with a short body - indistinguishable from a genuinely short email, and it
    would reach the model that way on every future run.
    """
    directory.mkdir(parents=True, exist_ok=True)
    final = cache_path(message.id, directory)
    payload = {
        "message_id": message.id,
        "sender": message.sender,
        "subject": message.subject,
        "internal_date": message.internal_date.isoformat(),
        "text_plain": message.text_plain,
        "text_html": message.text_html,
    }
    temp = final.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temp, final)


def cache_get(message_id: str, directory: Path) -> Cached | None:
    """One cached message, or None if it is absent or unreadable.

    Unreadable is treated as absent so the caller simply refetches. There is no
    state to lose here - the cache is derived data.
    """
    path = cache_path(message_id, directory)
    if not path.exists():
        return None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        return Cached(
            message_id=row["message_id"],
            sender=row.get("sender", ""),
            subject=row.get("subject", ""),
            internal_date=row.get("internal_date", ""),
            text_plain=row.get("text_plain", ""),
            text_html=row.get("text_html", ""),
        )
    except (ValueError, KeyError, TypeError):
        return None


def cache_missing(ids: Iterable[str], directory: Path) -> list[str]:
    """Which of these are not on disk yet, in the order given."""
    return [
        message_id
        for message_id in ids
        if not cache_path(message_id, directory).exists()
    ]
