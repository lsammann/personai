"""Scoring a prediction run. Pure: no network, no model, no clock.

The predict/score split is the structural idea of the harness, and this is the
half that matters for iteration. Inference over 200 messages costs an hour of
CPU; scoring costs milliseconds. So everything that could reasonably be
reconsidered later is a SCORING parameter, not a prediction one:

  needs new inference   model, prompt_id, body_chars, letter order,
                        EXTRACTION_VERSION
  free forever after    confidence_threshold, to_action_floor, weighting,
                        bucket widths, dev-vs-holdout, and any metric invented
                        after the run - which re-scores every historical run
                        retroactively

That only holds if this module stays a function of its arguments, so a test
parses its imports and fails if anything outside a small allowlist appears,
the same way `app/decision.py` is guarded. It imports neither `json` nor
`pathlib`: reading a results file is `evalrun`'s job, and this module takes
parsed `Prediction` objects. The claim is then about the module, not about
which functions in it happen to behave.

`decide()` is imported rather than reimplemented. `to_action_retention` asks
whether a message would have kept INBOX, and answering that with a second copy
of the threshold logic would measure the copy. It is already pure.

See `docs/PHASE2_PLAN.md` -> Step 5 implementation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from app.categories import KEEPS_INBOX, Category
from app.decision import decide
from app.evallabel import LabelRecord
from app.evalset import Sampled
from app.prefilter import is_reply, matches

# Calibration buckets are half-open, [0.0,0.1) ... [0.9,1.0], so 0.8 lands in
# [0.8,0.9). Arbitrary but pinned: an off-by-one at the boundary moves the
# confidence threshold that gets read off the table, and that number ends up in
# DESIGN.md.
BUCKETS = 10

# The threshold sweep. Bounded below at 0.5 because a tie between two
# categories cannot exceed 0.5 (see `Category`), so anything lower cannot
# separate a confident answer from a coin flip.
THRESHOLD_SWEEP = (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)

# Wilson interval z for 95%.
Z = 1.96

# Rows carrying this are a rule firing, not a model answer.
PREFILTER = "prefilter"
# The reply rule. Checked BEFORE the allowlist, matching the live order:
# a "Re:" from an allowlisted promotional domain is a reply to something
# the reader sent, and they want to see it.
REPLY = "reply"


@dataclass(frozen=True)
class Prediction:
    """One model answer, as read back from a results file."""

    message_id: str
    order_name: str
    category: str | None
    confidence: float | None
    distribution: dict[str, float]
    retained_mass: float | None
    latency_ms: int
    body_source: str
    body_len: int
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None or self.category is None

    def as_distribution(self) -> dict[Category, float]:
        return {category: self.distribution.get(category.value, 0.0)
                for category in Category}


@dataclass(frozen=True)
class Scored:
    """One prediction joined to its ground truth and its sampling provenance.

    The join is where every honest caveat in the report comes from: `stratum`
    carries the weight, `draw` says whether the example was keyword-mined, and
    `unsure` says whether the target itself is shaky.
    """

    message_id: str
    predicted: str | None
    truth: str
    confidence: float | None
    retained_mass: float | None
    distribution: dict[Category, float]
    stratum: str
    draw: str
    split: str
    unsure: bool
    body_source: str
    latency_ms: int
    source: str            # "model" or "prefilter"
    error: str | None

    @property
    def correct(self) -> bool:
        return self.predicted == self.truth


@dataclass
class Report:
    """Everything `score` computed. Rendering is the CLI's job, not this one."""

    n: int
    failures: int
    accuracy: float
    accuracy_interval: tuple[float, float]
    weighted_accuracy: float
    weighted_interval: tuple[float, float]
    accuracy_excluding_unsure: float
    n_unsure: int
    confusion: dict[tuple[str, str], int]
    action_matrix: dict[tuple[str, str], int]
    to_action_recall: dict[str, tuple[int, int]]
    to_action_retention: dict[str, tuple[int, int]]
    to_action_misses: list[Scored]
    calibration: list[dict[str, float]]
    calibration_gap: float
    threshold_sweep: list[dict[str, float]]
    floor_sweep: list[dict[str, float]]
    retained_mass: dict[str, float]
    accuracy_by_retained_decile: list[dict[str, float]]
    latency: dict[str, float]
    by_source: dict[str, tuple[int, int]]
    prefilter: dict[str, int] = field(default_factory=dict)
    reply: dict[str, int] = field(default_factory=dict)
    stability: dict[str, float] | None = None


# --- pure statistics ------------------------------------------------------


def wilson(successes: int, n: int, z: float = Z) -> tuple[float, float]:
    """A 95% interval on a proportion, Wilson rather than normal-approximation.

    At n=140 and an accuracy near 0.9 the normal approximation runs off the end
    of the scale - it would happily report an upper bound above 1.0 - and its
    coverage is poor exactly where this project expects to land. Wilson is
    bounded by construction and behaves at small n, which the per-stratum and
    per-source breakdowns very much are.
    """
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denominator = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denominator
    spread = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def mcnemar(pairs: Sequence[tuple[bool, bool]]) -> dict[str, float]:
    """Paired comparison of two runs over the same messages.

    Two accuracy numbers cannot tell "+2 points because four things got fixed"
    from "+2 points because twelve got fixed and eight broke". Only the
    discordant pairs carry information, which is what this counts.

    The exact binomial p-value, not the chi-square approximation: the
    discordant count here is routinely under 25, where the approximation is
    known to be anti-conservative.
    """
    fixed = sum(1 for a, b in pairs if not a and b)
    broken = sum(1 for a, b in pairs if a and not b)
    discordant = fixed + broken
    if discordant == 0:
        return {"n_fixed": 0, "n_broken": 0, "p_value": 1.0}
    smaller = min(fixed, broken)
    tail = sum(math.comb(discordant, k) for k in range(smaller + 1))
    p = min(1.0, 2 * tail / 2**discordant)
    return {"n_fixed": fixed, "n_broken": broken, "p_value": p}


def tv_distance(a: Mapping[Category, float], b: Mapping[Category, float]) -> float:
    """Total variation distance between two distributions: half the L1 norm.

    A finer signal than counting argmax changes, which is why the permutation
    check reports both. At n=200 an argmax-flip count moves in steps of 0.5%
    and hides a distribution that shifted a lot without crossing over.
    """
    return 0.5 * sum(abs(a.get(c, 0.0) - b.get(c, 0.0)) for c in Category)


def bucket(confidence: float, buckets: int = BUCKETS) -> int:
    """Which calibration bucket a confidence falls in. Half-open, top closed."""
    return min(int(confidence * buckets), buckets - 1)


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(q / 100 * len(ordered)), len(ordered) - 1)
    return ordered[index]


# --- weights --------------------------------------------------------------


def weights(
    strata: Mapping[str, object], scored: Sequence[Scored]
) -> dict[str, float]:
    """`w_h = N_h / n_h`, with `n_h` counted from what was actually LABELLED.

    `strata.json` records what was *sampled*. If any message ends up unlabelled
    or unpredicted, the real `n_h` is smaller, and weighting by the sampled
    figure overstates that stratum's share of the mailbox estimate in exact
    proportion to the gap. Nothing about the resulting number would look wrong.
    """
    counts: dict[str, int] = {}
    for row in scored:
        counts[row.stratum] = counts.get(row.stratum, 0) + 1
    result = {}
    for entry in strata["strata"]:  # type: ignore[index]
        name = entry["name"]
        n_h = counts.get(name, 0)
        if n_h:
            result[name] = entry["N_h"] / n_h
    return result


def weighted_accuracy(
    scored: Sequence[Scored], weight: Mapping[str, float]
) -> float:
    """Accuracy as an estimate of the MAILBOX, not of the eval set.

    The eval set over-samples rare classes by design, so its unweighted
    accuracy describes a population that does not exist. This puts each
    stratum back in proportion.
    """
    total = sum(weight.get(row.stratum, 0.0) for row in scored)
    if not total:
        return 0.0
    hit = sum(weight.get(row.stratum, 0.0) for row in scored if row.correct)
    return hit / total


# --- the join -------------------------------------------------------------


def join(
    predictions: Sequence[Prediction],
    labels: Mapping[str, LabelRecord],
    sample: Sequence[Sampled],
    *,
    split: str | None = "dev",
    allowlist: Iterable[str] = (),
    senders: Mapping[str, str] | None = None,
    subjects: Mapping[str, str] | None = None,
    order_name: str = "default",
) -> list[Scored]:
    """Predictions joined to truth and provenance, filtered to one split.

    `split=None` scores everything, which is what a smoke run over ten messages
    wants; the lock-box protocol is enforced by the caller passing "dev" until
    it deliberately does not.
    """
    plan = {row.message_id: row for row in sample}
    senders = senders or {}
    subjects = subjects or {}
    allowlist = frozenset(allowlist)

    scored: list[Scored] = []
    for prediction in predictions:
        if prediction.order_name != order_name:
            continue
        label = labels.get(prediction.message_id)
        row = plan.get(prediction.message_id)
        if label is None or row is None:
            continue
        if split is not None and row.split != split:
            continue
        sender = senders.get(prediction.message_id, "")
        # Reply first, allowlist second - the live order.
        replied = is_reply(subjects.get(prediction.message_id))
        is_rule = bool(allowlist) and matches(sender, allowlist)
        scored.append(
            Scored(
                message_id=prediction.message_id,
                predicted=prediction.category,
                truth=label.label.value,
                confidence=prediction.confidence,
                retained_mass=prediction.retained_mass,
                distribution=prediction.as_distribution(),
                stratum=row.stratum,
                draw=row.draw,
                split=row.split,
                unsure=label.unsure,
                body_source=prediction.body_source,
                latency_ms=prediction.latency_ms,
                source=(
                    REPLY if replied else PREFILTER if is_rule else "model"
                ),
                error=prediction.error,
            )
        )
    return scored


# --- the report -----------------------------------------------------------


def keeps_inbox(row: Scored, threshold: float, floor: float) -> bool:
    """Would the live system have left this message visible?

    Through `decide()` rather than a reimplementation of the rule, so this
    measures the shipped behaviour - including the asymmetric floor and
    Needs Review, both of which save a message the argmax would have archived.
    """
    if row.source == REPLY:
        # `decide_reply_hit()` removes nothing, so a rule-routed message is
        # visible whatever the model said about it. Scoring the model's
        # would-be decision here would measure a call the live system never
        # makes.
        return True
    if row.predicted is None:
        return True          # a failure applies no labels, so INBOX survives
    return "INBOX" not in decide(
        row.distribution,
        confidence_threshold=threshold,
        to_action_floor=floor,
    ).labels_remove


def score(
    predictions: Sequence[Prediction],
    labels: Mapping[str, LabelRecord],
    sample: Sequence[Sampled],
    strata: Mapping[str, object],
    *,
    confidence_threshold: float,
    to_action_floor: float,
    split: str | None = "dev",
    allowlist: Iterable[str] = (),
    senders: Mapping[str, str] | None = None,
    subjects: Mapping[str, str] | None = None,
) -> Report:
    """A results file plus ground truth -> every number the phase gate needs."""
    scored = join(
        predictions, labels, sample,
        split=split, allowlist=allowlist, senders=senders, subjects=subjects,
    )
    answered = [row for row in scored if row.predicted is not None]
    # Rule hits are a rule firing. They have no confidence in the live system,
    # so they cannot appear in a calibration table without corrupting it - and
    # the eval has a model confidence for them only because `predict` classifies
    # every message regardless of which rules would have intercepted it.
    model_rows = [
        row for row in answered if row.source not in (PREFILTER, REPLY)
    ]
    failures = len(scored) - len(answered)

    hits = sum(1 for row in answered if row.correct)
    accuracy = hits / len(answered) if answered else 0.0
    weight = weights(strata, answered)

    confident = [row for row in answered if not row.unsure]
    confident_hits = sum(1 for row in confident if row.correct)

    return Report(
        n=len(scored),
        failures=failures,
        accuracy=accuracy,
        accuracy_interval=wilson(hits, len(answered)),
        weighted_accuracy=weighted_accuracy(answered, weight),
        weighted_interval=wilson(hits, len(answered)),
        accuracy_excluding_unsure=(
            confident_hits / len(confident) if confident else 0.0
        ),
        n_unsure=sum(1 for row in answered if row.unsure),
        confusion=_confusion(answered),
        action_matrix=_action_matrix(
            answered, confidence_threshold, to_action_floor
        ),
        to_action_recall=_to_action_recall(answered),
        to_action_retention=_to_action_retention(
            answered, confidence_threshold, to_action_floor
        ),
        to_action_misses=[
            row for row in answered
            if row.truth == Category.TO_ACTION.value
            and not keeps_inbox(row, confidence_threshold, to_action_floor)
        ],
        calibration=_calibration(model_rows),
        calibration_gap=_calibration_gap(model_rows),
        threshold_sweep=_threshold_sweep(model_rows, to_action_floor),
        floor_sweep=_floor_sweep(model_rows, confidence_threshold),
        retained_mass=_retained_mass(model_rows),
        accuracy_by_retained_decile=_by_retained_decile(model_rows),
        latency=_latency(answered),
        by_source=_by_body_source(answered),
        prefilter=_prefilter_bucket(scored, confidence_threshold, to_action_floor),
        reply=_reply_bucket(scored, confidence_threshold, to_action_floor),
    )


def _confusion(rows: Sequence[Scored]) -> dict[tuple[str, str], int]:
    """truth -> predicted counts. Cells are read, not just the diagonal."""
    matrix: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row.truth, row.predicted or "")
        matrix[key] = matrix.get(key, 0) + 1
    return matrix


def _action_matrix(
    rows: Sequence[Scored], threshold: float, floor: float
) -> dict[tuple[str, str], int]:
    """The 2x2 with actual consequences: kept in the inbox, or archived.

    DESIGN.md's binary-action-space argument made concrete. A Receipts/Bookings
    confusion costs nothing because both archive; only crossing this boundary
    changes what happens to the message. Computed through `decide()`, so the
    asymmetric floor and Needs Review both count as keeping.
    """
    matrix: dict[tuple[str, str], int] = {}
    for row in rows:
        truth = "keeps" if row.truth in {c.value for c in KEEPS_INBOX} else "archives"
        predicted = "keeps" if keeps_inbox(row, threshold, floor) else "archives"
        matrix[(truth, predicted)] = matrix.get((truth, predicted), 0) + 1
    return matrix


def _by_draw(rows: Sequence[Scored], predicate) -> dict[str, tuple[int, int]]:
    """(hits, n) overall and per `draw`.

    Broken out because a `To Action` recall figure computed over messages
    selected for saying "overdue" in the subject is not a recall figure. The
    honest number is `draw == "R"`, the uniform draw over the frame.
    """
    result: dict[str, list[int]] = {"all": [0, 0]}
    for row in rows:
        for key in ("all", row.draw):
            cell = result.setdefault(key, [0, 0])
            cell[1] += 1
            if predicate(row):
                cell[0] += 1
    return {key: (hit, n) for key, (hit, n) in result.items()}


def _to_action_recall(rows: Sequence[Scored]) -> dict[str, tuple[int, int]]:
    """Of the messages that ARE To Action, how many did the argmax catch?"""
    targets = [row for row in rows if row.truth == Category.TO_ACTION.value]
    return _by_draw(targets, lambda row: row.predicted == Category.TO_ACTION.value)


def _to_action_retention(
    rows: Sequence[Scored], threshold: float, floor: float
) -> dict[str, tuple[int, int]]:
    """...and how many stayed in the inbox, which is the operational question.

    The two differ, and the difference is the point: a bill filed as Receipts
    at p(To Action)=0.22 is not a missed bill, because the asymmetric floor
    keeps it visible. DESIGN.md's metric as written scores it as one.
    """
    targets = [row for row in rows if row.truth == Category.TO_ACTION.value]
    return _by_draw(targets, lambda row: keeps_inbox(row, threshold, floor))


def _calibration(rows: Sequence[Scored]) -> list[dict[str, float]]:
    buckets: dict[int, list[int]] = {}
    for row in rows:
        if row.confidence is None:
            continue
        cell = buckets.setdefault(bucket(row.confidence), [0, 0])
        cell[1] += 1
        if row.correct:
            cell[0] += 1
    return [
        {
            "low": index / BUCKETS,
            "high": (index + 1) / BUCKETS,
            "n": n,
            "accuracy": hit / n if n else 0.0,
        }
        for index, (hit, n) in sorted(buckets.items())
    ]


def _calibration_gap(rows: Sequence[Scored]) -> float:
    """Mean confidence when right, minus mean confidence when wrong.

    The screen that killed self-reported confidence in Phase 0, where it came
    out at -0.050, 0.000 and +0.017 on the three candidate models. If this is
    not meaningfully positive the threshold is decorative and nothing should be
    allowed to remove INBOX.
    """
    right = [row.confidence for row in rows if row.correct and row.confidence]
    wrong = [row.confidence for row in rows if not row.correct and row.confidence]
    if not right or not wrong:
        return 0.0
    return sum(right) / len(right) - sum(wrong) / len(wrong)


def _threshold_sweep(
    rows: Sequence[Scored], floor: float
) -> list[dict[str, float]]:
    """What each confidence threshold would cost and buy.

    Three columns, because the choice is a trade: how much lands in
    Needs Review, how accurate the rest is once it is applied automatically,
    and - the one that decides it - how many To Action messages get archived
    anyway.
    """
    sweep = []
    for threshold in THRESHOLD_SWEEP:
        auto = [row for row in rows
                if row.confidence is not None and row.confidence >= threshold]
        review = len(rows) - len(auto)
        hits = sum(1 for row in auto if row.correct)
        missed = sum(
            1 for row in rows
            if row.truth == Category.TO_ACTION.value
            and not keeps_inbox(row, threshold, floor)
        )
        sweep.append({
            "threshold": threshold,
            "needs_review_rate": review / len(rows) if rows else 0.0,
            "auto_accuracy": hits / len(auto) if auto else 0.0,
            "to_action_missed": missed,
        })
    return sweep


def _floor_sweep(
    rows: Sequence[Scored], threshold: float
) -> list[dict[str, float]]:
    """The To Action floor at a fixed threshold, respecting `T + F < 1`.

    Values at or above `1 - T` are skipped rather than reported as zero: the
    asymmetric rule is unreachable there, which `config` refuses to start on,
    so printing them would invite choosing one.
    """
    sweep = []
    targets = [row for row in rows if row.truth == Category.TO_ACTION.value]
    for floor in (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4):
        if threshold + floor >= 1.0:
            continue
        kept = sum(1 for row in targets if keeps_inbox(row, threshold, floor))
        archived = sum(
            1 for row in rows
            if row.truth != Category.TO_ACTION.value
            and not keeps_inbox(row, threshold, floor)
        )
        sweep.append({
            "floor": floor,
            "to_action_kept": kept,
            "to_action_total": len(targets),
            "other_archived": archived,
        })
    return sweep


def _retained_mass(rows: Sequence[Scored]) -> dict[str, float]:
    values = [row.retained_mass for row in rows if row.retained_mass is not None]
    return {f"p{q}": percentile(values, q) for q in (1, 5, 10, 50)}


def _by_retained_decile(rows: Sequence[Scored]) -> list[dict[str, float]]:
    """Accuracy conditioned on how much of the top-20 sat on category letters.

    DESIGN.md explicitly defers whether a `retained_mass` floor should force
    Needs Review to Phase 2. This is the instrument: if the bottom decile is
    not materially worse than the rest, the answer is *no floor*, recorded as
    such rather than left open.
    """
    values = sorted(
        (row.retained_mass, row.correct)
        for row in rows if row.retained_mass is not None
    )
    if not values:
        return []
    size = max(1, len(values) // 10)
    deciles = []
    for index in range(0, len(values), size):
        chunk = values[index:index + size]
        deciles.append({
            "from": chunk[0][0],
            "to": chunk[-1][0],
            "n": len(chunk),
            "accuracy": sum(1 for _, ok in chunk if ok) / len(chunk),
        })
    return deciles


def _latency(rows: Sequence[Scored]) -> dict[str, float]:
    values = [float(row.latency_ms) for row in rows]
    if not values:
        return {"mean": 0.0, "p50": 0.0, "p90": 0.0}
    return {
        "mean": sum(values) / len(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
    }


def _by_body_source(rows: Sequence[Scored]) -> dict[str, tuple[int, int]]:
    """Accuracy per extraction cohort, returned WITH its n.

    Two of the four cohorts are tiny in this set - `plain_markup` has 3 members
    and `stub_fallback` 7 - so their accuracy is anecdote. Returning the count
    alongside is what stops it being printed as a measurement.
    """
    result: dict[str, list[int]] = {}
    for row in rows:
        cell = result.setdefault(row.body_source, [0, 0])
        cell[1] += 1
        if row.correct:
            cell[0] += 1
    return {key: (hit, n) for key, (hit, n) in sorted(result.items())}


def _prefilter_bucket(
    rows: Sequence[Scored], threshold: float, floor: float
) -> dict[str, int]:
    """How the sender-domain rule does, scored on the ACTION not the label.

    The rule can only ever emit `Agent/Promotions`, so a prefiltered message
    whose true label is `Updates` is an error 6-way and entirely correct in
    practice - both archive. Scoring it 6-way would report the prefilter as
    badly wrong while it is doing exactly its job.
    """
    hits = [row for row in rows if row.source == PREFILTER]
    if not hits:
        return {}
    keeps = {c.value for c in KEEPS_INBOX}
    return {
        "n": len(hits),
        # The rule always archives, so it is right whenever the truth archives.
        "rule_action_correct": sum(1 for row in hits if row.truth not in keeps),
        "model_action_correct": sum(
            1 for row in hits
            if (row.truth in keeps) == keeps_inbox(row, threshold, floor)
        ),
        "label_exact": sum(
            1 for row in hits if row.truth == Category.PROMOTIONS.value
        ),
    }


def _reply_bucket(
    rows: Sequence[Scored], threshold: float, floor: float
) -> dict[str, int]:
    """How the reply rule does, scored on the ACTION not the label.

    Same treatment as the prefilter, for the same reason: the rule can only
    emit `Agent/Personal`, so a reply whose true label is `To Action` is an
    error 6-way and entirely correct in practice - both keep the inbox.

    `model_action_correct` is the comparison that justifies the rule: what the
    model would have done with these messages if the rule had not fired. Where
    the rule scores higher, the rule is earning its place.

    `label_exact` is deliberately reported too. The reader's definition here is
    "am I in this thread", which is wider than `Personal`'s documented "written
    by a real person" - so a gap between `rule_action_correct` and
    `label_exact` is the size of that widening, visible rather than silent.
    """
    hits = [row for row in rows if row.source == REPLY]
    if not hits:
        return {}
    keeps = {c.value for c in KEEPS_INBOX}
    return {
        "n": len(hits),
        # The rule always keeps INBOX, so it is right whenever the truth keeps.
        "rule_action_correct": sum(1 for row in hits if row.truth in keeps),
        "model_action_correct": sum(
            1 for row in hits
            if (row.truth in keeps) == (
                row.predicted is None
                or "INBOX" not in decide(
                    row.distribution,
                    confidence_threshold=threshold,
                    to_action_floor=floor,
                ).labels_remove
            )
        ),
        "label_exact": sum(
            1 for row in hits if row.truth == Category.PERSONAL.value
        ),
    }


def stability(
    predictions: Sequence[Prediction], baseline: str = "default"
) -> dict[str, float]:
    """Does the distribution measure content, or alphabet position?

    Phase 0 killed both 3B candidates on this: classifying identical emails
    under three letter mappings, the predicted category held for 5 of 11, where
    llama3.1:8b held 9 of 11. Reports the unchanged-argmax fraction and the
    mean TV distance, because at this sample size a flip count alone moves in
    steps of 0.5% and hides a distribution that shifted without crossing over.
    """
    by_message: dict[str, dict[str, Prediction]] = {}
    for prediction in predictions:
        by_message.setdefault(prediction.message_id, {})[
            prediction.order_name
        ] = prediction

    same, total, distances = 0, 0, []
    for orders in by_message.values():
        anchor = orders.get(baseline)
        if anchor is None or anchor.failed:
            continue
        for name, other in orders.items():
            if name == baseline or other.failed:
                continue
            total += 1
            same += anchor.category == other.category
            distances.append(
                tv_distance(anchor.as_distribution(), other.as_distribution())
            )
    if not total:
        return {"n": 0, "unchanged": 0.0, "mean_tv": 0.0}
    return {
        "n": total,
        "unchanged": same / total,
        "mean_tv": sum(distances) / len(distances),
    }
