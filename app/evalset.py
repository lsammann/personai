"""Building the eval sample: strata, drawing, splits, weights.

Pure by default. Everything that decides *which messages get labelled* is a
function of a frame listing and a seed, so the whole sampling design is
testable without Gmail - which matters more here than usual, because a
sampling bug does not raise. It silently produces a set whose accuracy number
describes a population that does not exist.

Not in `scripts/` despite being driven by one: `pyproject.toml` packages only
`app`, so `scripts/` is not importable and none of the tests below could be
written against it. `docs/PLAN.md` Phase 5 also has `app/metrics.py`
aggregating the eval results, so this is shared code, not harness scaffolding.

The frame is pinned to absolute dates rather than `newer_than:1y` - see
`FrameSpec`, which is where the reproducibility of the whole set actually
comes from.

The design in one paragraph. The mailbox is ~80% promotional, so a uniform
sample of 200 would contain single-digit counts of the categories that matter
(`docs/BACKLOG.md`: twenty consecutive inbox subjects held zero `To Action`,
`Personal`, `Receipts` or `Bookings`). So we draw `R` uniformly for a mailbox
estimate that is honest by construction, then over-sample three keyword-mined
strata so the rare-class metrics rest on more than a handful of examples, and
weight by `N_h/n_h` to recover a mailbox-level number from the combination.
`draw` records which of those a message came from, because a `To Action`
recall figure computed over messages selected for saying "overdue" is not a
recall figure.

See `docs/PHASE2_PLAN.md` -> Step 2 implementation.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.config import DATA_DIR

# Sent, drafts and chats are not mail that arrives, so they are not mail the
# agent would ever classify.
FRAME_EXCLUSIONS = "-in:sent -in:drafts -in:chats"
FRAME_WINDOW_YEARS = 1

DEFAULT_SEED = 7

# The split draw is seeded separately so that changing one cannot silently
# reshuffle the other. Derived from the main seed rather than a second CLI
# flag: one number has to reproduce the whole plan.
SPLIT_SEED_OFFSET = 1
DEV_FRACTION = 0.7

# The residual cell of the partition. Not a query - it is everything the
# mined strata did not claim, which is what makes the four cells exhaustive.
RESIDUAL = "Residual"

# Where a sampled message came from, as opposed to which cell it sits in.
DRAW_R = "R"
DRAW_MINED = "mined"
DRAW_EXTENSION = "extension"

# How many uniform draws over the whole frame. This is the representativeness
# anchor: accuracy on `draw == "R"` alone is a mailbox estimate needing no
# weights at all, which is the number to fall back on if the weighting is ever
# in doubt.
R_TARGET = 100

# Widening steps for a mined stratum that cannot be filled inside one year,
# and the point at which we stop widening and hand the shortfall back to `R`.
MAX_WINDOW_YEARS = 4

# Committed files must carry ids and provenance, never content. Enforced on
# write and asserted by a test - "never commit content" is an invariant, so it
# gets a guard rather than a docstring.
SAMPLE_KEYS = frozenset({"message_id", "stratum", "draw", "split", "seed"})

FRAME_PATH = DATA_DIR / "eval_frame.jsonl"
EVAL_DIR = Path("eval")
SAMPLE_PATH = EVAL_DIR / "sample.jsonl"
STRATA_PATH = EVAL_DIR / "strata.json"


def _shift_years(date: dt.date, years: int) -> dt.date:
    """`date` moved by whole years. 29 February lands on the 28th."""
    try:
        return date.replace(year=date.year + years)
    except ValueError:
        return date.replace(year=date.year + years, day=28)


@dataclass(frozen=True)
class FrameSpec:
    """The sampling frame, pinned to absolute dates.

    The reason this exists rather than a `newer_than:1y` string. That operator
    is evaluated relative to the moment the query runs, so the frame it
    describes moves every day: mail arrives at the front, a day ages off the
    back, and `--seed 7` draws from a different population each time it is
    run. The sample would look reproducible only for as long as the cached
    frame listing survived - and that listing lives in `data/`, which is
    gitignored, so `eval/sample.jsonl` would be committed and auditable while
    the population it came from existed on exactly one laptop.

    Pinning the window at sample time makes the frame query a constant. Two
    enumerations a month apart return the same population, the cache goes back
    to being a speed optimisation rather than the thing holding the guarantee
    up, and `N_h` stays comparable across re-runs.

    Dates are day-granular and interpreted in the account's timezone, which is
    Gmail's behaviour for `after:`/`before:`. That does not affect
    reproducibility - the same string always selects the same messages - only
    which side of midnight a boundary message falls, which no measurement here
    is sensitive to.

    Deletion is the one thing this cannot fix: a deleted message leaves the
    frame whatever the query says. That is handled downstream instead, by
    `data/eval_cache/` storing each message at label time.
    """

    start: dt.date
    end: dt.date
    exclusions: str = FRAME_EXCLUSIONS

    @classmethod
    def ending(
        cls, end: dt.date | None = None, years: int = FRAME_WINDOW_YEARS
    ) -> FrameSpec:
        """A window of `years` ending today, or on a given date.

        Today means the *local* date, not UTC: Gmail interprets `after:` and
        `before:` in the account's timezone, so a UTC date would disagree with
        the query it is about to build for anyone west of Greenwich after
        midnight.
        """
        end = end or dt.datetime.now(dt.UTC).astimezone().date()
        return cls(_shift_years(end, -years), end)

    @property
    def query(self) -> str:
        return (
            f"after:{self.start:%Y/%m/%d} before:{self.end:%Y/%m/%d} "
            f"{self.exclusions}"
        )

    def widened(self, years: int) -> str:
        """Mail *older* than the frame, reaching back `years` further.

        Disjoint from the frame by construction - it ends where the frame
        begins - which is what the widening step wants. The old
        `newer_than:{n}y` form returned a superset including the frame itself
        and relied on the caller filtering it back out.
        """
        return (
            f"after:{_shift_years(self.start, -years):%Y/%m/%d} "
            f"before:{self.start:%Y/%m/%d} {self.exclusions}"
        )


@dataclass(frozen=True)
class Stratum:
    """One cell of the partition, and how many extra to mine for it."""

    name: str
    # The Gmail clause ANDed with the frame query. Empty for Residual, which
    # is defined by subtraction and has no query of its own.
    query: str
    target: int


@dataclass(frozen=True)
class FrameRow:
    message_id: str
    thread_id: str


@dataclass(frozen=True)
class Sampled:
    message_id: str
    # The partition cell. Drives `w_h = N_h/n_h`.
    stratum: str
    # Provenance: R, mined, or extension. Drives which metrics may honestly
    # be computed over this row.
    draw: str
    split: str


@dataclass(frozen=True)
class SamplePlan:
    seed: int
    spec: FrameSpec
    frame_size: int
    strata: tuple[Stratum, ...]
    counts: Mapping[str, int]  # N_h, over the frame
    windows: Mapping[str, int]  # how far each stratum had to widen, in years
    sampled: tuple[Sampled, ...]


# The mined strata, in assignment order. Order is the disjointness mechanism:
# a message matching both S_action and S_txn - "Your invoice receipt" - lands
# in the first, deterministically, rather than in whichever query ran last.
#
# Every query is built from observable Gmail terms, never from my guess at the
# label. A stratum defined as "emails I think are bills" would leak the answer
# into the frame and make every number computed from it uninterpretable.
MINED_STRATA: tuple[Stratum, ...] = (
    Stratum(
        "S_action",
        'subject:(invoice OR "payment due" OR overdue OR renew OR expires OR '
        '"action required" OR statement OR verify)',
        35,
    ),
    # Resolved specially - see `resolve_membership`. The query is recorded for
    # the manifest; membership comes from thread ids, which a plain query
    # cannot express.
    Stratum("S_human", "is:starred OR from:me (by thread)", 35),
    Stratum(
        "S_txn",
        "subject:(receipt OR order OR booking OR reservation OR confirmation)",
        30,
    ),
)

ALL_STRATA: tuple[Stratum, ...] = MINED_STRATA + (Stratum(RESIDUAL, "", 0),)


# --- the partition -------------------------------------------------------


def assign_strata(
    frame_ids: Iterable[str],
    membership: Mapping[str, set[str]],
    order: Sequence[Stratum] = MINED_STRATA,
) -> dict[str, str]:
    """Every frame id to exactly one stratum. First match in `order` wins.

    Ordered assignment rather than four independent queries, because the
    weights need a partition: overlapping strata double-count `N_h` and the
    weighted accuracy stops being an estimate of anything.
    """
    assignment: dict[str, str] = {}
    for message_id in frame_ids:
        for stratum in order:
            if message_id in membership.get(stratum.name, frozenset()):
                assignment[message_id] = stratum.name
                break
        else:
            assignment[message_id] = RESIDUAL
    return assignment


def stratum_counts(assignment: Mapping[str, str]) -> dict[str, int]:
    """N_h. Sums to the frame size, by construction of `assign_strata`."""
    counts = Counter(assignment.values())
    return {stratum.name: counts.get(stratum.name, 0) for stratum in ALL_STRATA}


# --- drawing -------------------------------------------------------------


def build_plan(
    frame: Sequence[FrameRow],
    membership: Mapping[str, set[str]],
    *,
    seed: int = DEFAULT_SEED,
    r_target: int = R_TARGET,
    strata: Sequence[Stratum] = MINED_STRATA,
    extend: Callable[[Stratum, int], list[str]] | None = None,
    spec: FrameSpec,
) -> SamplePlan:
    """The whole sample, deterministically, from a frame and a seed.

    `extend` is injected rather than called directly so this stays a pure
    function of its arguments: the CLI passes a closure over a Gmail service,
    the tests pass a dict lookup, and the drawing logic is identical in both.
    It is asked for ids matching a stratum's query over a wider window, and
    anything already inside the frame is discarded here rather than there.

    Order matters and is the design:

    1. `R` uniformly over the whole frame. Representative by construction.
    2. Each mined stratum topped up from what `R` did not already take.
    3. Widening, for a stratum the frame cannot fill.
    4. Any shortfall left after widening goes back to `R`, inside the frame.

    Step 4's constraint is the important one. `R` is what makes the mailbox
    estimate meaningful, so it may never be topped up from outside the frame -
    a 2022 promotion carrying a weight derived from the last twelve months
    would quietly corrupt the one number the phase exists to produce.
    """
    rng = random.Random(seed)
    frame_ids = sorted(row.message_id for row in frame)
    in_frame = set(frame_ids)
    assignment = assign_strata(frame_ids, membership, strata)
    counts = stratum_counts(assignment)

    taken: set[str] = set()
    sampled: list[Sampled] = []
    windows: dict[str, int] = {stratum.name: 1 for stratum in ALL_STRATA}

    def take(pool: Sequence[str], n: int, stratum: str, draw: str) -> int:
        """Draw `n` from `pool`, recording them. Returns how many were taken.

        `sorted` on every pool, every time. Python randomises string hashing
        per process, so a set's iteration order changes between runs and
        `random.sample` picks by position - seeding alone does not give a
        reproducible sample, and the failure is invisible until two runs are
        diffed.
        """
        candidates = sorted(set(pool) - taken)
        picks = rng.sample(candidates, min(n, len(candidates)))
        for message_id in picks:
            sampled.append(Sampled(message_id, stratum, draw, ""))
        taken.update(picks)
        return len(picks)

    # 1. R, over the whole frame.
    take(frame_ids, r_target, "", DRAW_R)

    # 2 and 3. Mined top-ups, then widening for whatever the frame could not
    # supply.
    shortfall = 0
    for stratum in strata:
        pool = [m for m in frame_ids if assignment[m] == stratum.name]
        missing = stratum.target - take(pool, stratum.target, stratum.name, DRAW_MINED)

        years = 1
        while missing and years < MAX_WINDOW_YEARS and extend is not None:
            years += 1
            wider = [m for m in extend(stratum, years) if m not in in_frame]
            missing -= take(wider, missing, stratum.name, DRAW_EXTENSION)
            windows[stratum.name] = years
        shortfall += missing

    # 4. Whatever widening could not supply, back into R - inside the frame.
    if shortfall:
        take(frame_ids, shortfall, "", DRAW_R)

    # R's rows are stamped with their partition cell only now, because a cell
    # is a property of the message and not of the draw that found it.
    resolved = [
        Sampled(
            row.message_id,
            row.stratum or assignment[row.message_id],
            row.draw,
            row.split,
        )
        for row in sampled
    ]
    splits = assign_splits(resolved, seed)
    final = tuple(
        sorted(
            (
                Sampled(r.message_id, r.stratum, r.draw, splits[r.message_id])
                for r in resolved
            ),
            key=lambda r: r.message_id,
        )
    )

    return SamplePlan(
        seed=seed,
        spec=spec,
        frame_size=len(frame_ids),
        strata=tuple(strata) + (Stratum(RESIDUAL, "", 0),),
        counts=counts,
        windows=windows,
        sampled=final,
    )


def assign_splits(
    sampled: Iterable[Sampled],
    seed: int,
    dev_fraction: float = DEV_FRACTION,
) -> dict[str, str]:
    """Dev/holdout, stratified within each cell.

    Stratified rather than a single 140/60 cut across the whole sample: the
    mined cells are small, and an unstratified draw can put most of one rare
    class on one side, which would make the holdout's job - detecting
    inflation - impossible for the categories it matters for.

    Per-cell rounding means the dev total may land a message or two either
    side of `dev_fraction * n`. That is the intended trade; the alternative
    is an exact global count with lumpy per-cell coverage.
    """
    rng = random.Random(seed + SPLIT_SEED_OFFSET)
    by_stratum: dict[str, list[str]] = defaultdict(list)
    for row in sampled:
        by_stratum[row.stratum].append(row.message_id)

    splits: dict[str, str] = {}
    for name in sorted(by_stratum):
        ids = sorted(by_stratum[name])
        dev = set(rng.sample(ids, round(len(ids) * dev_fraction)))
        for message_id in ids:
            splits[message_id] = "dev" if message_id in dev else "holdout"
    return splits


def sampled_counts(plan: SamplePlan) -> dict[str, dict[str, int]]:
    """n_h and n_extension per cell.

    Extension rows are counted separately and deliberately excluded from
    `n_h`: they were drawn from outside the frame, so including them would put
    a denominator from one population under a numerator from another and make
    `w_h` describe neither.
    """
    result = {
        stratum.name: {"n_h": 0, "n_extension": 0} for stratum in plan.strata
    }
    for row in plan.sampled:
        key = "n_extension" if row.draw == DRAW_EXTENSION else "n_h"
        result[row.stratum][key] += 1
    return result


# --- Gmail-facing --------------------------------------------------------


def enumerate_frame(svc, spec: FrameSpec) -> list[FrameRow]:
    """The frame, as (id, thread_id). List calls only, no `get`."""
    from app import gmail_client

    return [
        FrameRow(mid, tid) for mid, tid in gmail_client.search_refs(svc, spec.query)
    ]


def resolve_membership(
    svc,
    frame: Sequence[FrameRow],
    strata: Sequence[Stratum] = MINED_STRATA,
    spec: FrameSpec | None = None,
) -> dict[str, set[str]]:
    """Which frame ids each mined stratum contains. Full enumeration, not a sample.

    `N_h` has to be exact - it is the numerator of every weight - so each
    stratum query is paginated to the end rather than estimated.

    `S_human` cannot be expressed as one query. Starred mail is a direct hit,
    but "threads I replied to" lives in `from:me`, which the frame excludes by
    construction. So we take the thread ids of sent mail and match them back
    against the frame's, which `search_refs` already carries. The alternative
    - a `get` per frame message to read its thread - is 6,300 requests to
    learn something `messages.list` returns for free.
    """
    from app import gmail_client

    spec = spec or FrameSpec.ending()
    frame_query = spec.query
    in_frame = {row.message_id for row in frame}
    membership: dict[str, set[str]] = {}

    for stratum in strata:
        if stratum.name == "S_human":
            starred = set(gmail_client.search_ids(svc, f"{frame_query} is:starred"))
            sent_threads = {
                thread_id
                for _, thread_id in gmail_client.search_refs(svc, "from:me")
                if thread_id
            }
            replied = {
                row.message_id for row in frame if row.thread_id in sent_threads
            }
            membership[stratum.name] = (starred & in_frame) | replied
        else:
            query = f"{frame_query} {stratum.query}"
            membership[stratum.name] = set(
                gmail_client.search_ids(svc, query)
            ) & in_frame

    return membership


def make_extender(svc, spec: FrameSpec) -> Callable[[Stratum, int], list[str]]:
    """A widening resolver for `build_plan`, closed over a Gmail service.

    Asks for the stratum's own query over the band immediately *older* than
    the frame. `FrameSpec.widened` ends where the frame begins, so the result
    is disjoint from the frame by construction rather than by the caller
    filtering it afterwards.

    `S_human` is not widened - its membership comes from matching thread ids
    against the frame, and a wider date range does not extend the frame.
    """
    from app import gmail_client

    def extend(stratum: Stratum, years: int) -> list[str]:
        if not stratum.query or stratum.name == "S_human":
            return []
        return gmail_client.search_ids(
            svc, f"{spec.widened(years)} {stratum.query}"
        )

    return extend


# --- persistence ---------------------------------------------------------


def save_frame(
    rows: Sequence[FrameRow], spec: FrameSpec, path: Path = FRAME_PATH
) -> None:
    """Gitignored - it is a listing of the mailbox, so it stays in `data/`.

    The first line records which window produced the listing. Without it a
    cache built for one window would be reused silently for another, and the
    sample would be drawn from a population that no longer matches the query
    recorded beside it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"frame_query": spec.query}) + "\n")
        for row in rows:
            handle.write(
                json.dumps({"message_id": row.message_id, "thread_id": row.thread_id})
                + "\n"
            )


def load_frame(spec: FrameSpec, path: Path = FRAME_PATH) -> list[FrameRow]:
    """The cached listing, but only if it was built for this window.

    Tolerant read: a truncated trailing line is skipped, not fatal - the same
    strict-write / tolerant-read split as `app/logbook.py`. A half-written
    last line means re-enumerating, not a crash. A header for a different
    window means the same thing.
    """
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError:
        return []
    if header.get("frame_query") != spec.query:
        return []

    rows: list[FrameRow] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append(FrameRow(record["message_id"], record.get("thread_id", "")))
    return rows


def sample_rows(plan: SamplePlan) -> list[dict[str, object]]:
    """The committed rows. Ids and provenance only - never content."""
    return [
        {
            "message_id": row.message_id,
            "stratum": row.stratum,
            "draw": row.draw,
            "split": row.split,
            "seed": plan.seed,
        }
        for row in plan.sampled
    ]


def save_sample(plan: SamplePlan, path: Path = SAMPLE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sample_rows(plan)
    for row in rows:
        extra = set(row) - SAMPLE_KEYS
        if extra:
            raise ValueError(f"refusing to write non-allowlisted keys: {sorted(extra)}")
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def strata_document(plan: SamplePlan) -> dict[str, object]:
    """The manifest `run_eval` derives `w_h = N_h/n_h` from at score time.

    Weights are not stored. A re-measured `N_h` would otherwise leave a stale
    weight on disk beside a fresh count, and nothing about the resulting
    number would look wrong.
    """
    counts = sampled_counts(plan)
    return {
        "seed": plan.seed,
        # Absolute, so a re-run months from now enumerates the same
        # population - and so a reader can see which one it was.
        "frame_query": plan.spec.query,
        "window_start": plan.spec.start.isoformat(),
        "window_end": plan.spec.end.isoformat(),
        "frame_size": plan.frame_size,
        "strata": [
            {
                "name": stratum.name,
                "query": stratum.query,
                "window_years": plan.windows.get(stratum.name, 1),
                "N_h": plan.counts.get(stratum.name, 0),
                "n_h": counts[stratum.name]["n_h"],
                "n_extension": counts[stratum.name]["n_extension"],
            }
            for stratum in plan.strata
        ],
    }


def save_strata(plan: SamplePlan, path: Path = STRATA_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(strata_document(plan), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
