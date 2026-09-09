"""The label decision table, as pure functions.

Given an outcome, which labels? That is the only question this file answers,
and it answers it four ways - a model result, a prefilter hit, a failure, and a
dead letter. It is the most-tested file in the project and that only holds if
it stays isolated, so it imports nothing but `app.categories` and the standard
library. `test_decision.py` asserts that mechanically.

Deliberately NOT importing `app.config`: thresholds arrive as explicit keyword
floats so the purity claim needs no argument about whether a module holding
`Path` constants counts, and so every test states the thresholds it exercises.
`agent.py` does the one-line unpack from `Config`.

The three outcomes that matter are the three mailbox states, and they never
collapse into each other:

  Agent/Processed   classified (or prefiltered); never looked at again
  Agent/Error       the system gave up after repeated failures
  neither           unprocessed; picked up again next cycle
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.categories import (
    ERROR,
    INBOX,
    KEEPS_INBOX,
    NEEDS_REVIEW,
    PROCESSED,
    Category,
    argmax,
    label_for,
)


@dataclass(frozen=True)
class Decision:
    """What to do with one message, and enough context to log why.

    Carries more than the two label tuples because `logbook` needs the category
    and confidence for the row; having the caller re-derive the argmax would
    duplicate the logic this module exists to own.
    """

    labels_add: tuple[str, ...]
    labels_remove: tuple[str, ...]
    category: Category | None
    confidence: float | None
    needs_review: bool
    to_action_override: bool


def decide(
    distribution: Mapping[Category, float],
    *,
    confidence_threshold: float,
    to_action_floor: float,
) -> Decision:
    """A successful classification -> labels. Rows 1-5 of the decision table.

    Both comparisons are `>=`: confidence exactly at the threshold counts as
    confident, and p(To Action) exactly at the floor keeps INBOX.
    """
    _validate(distribution)

    category = argmax(distribution)
    confidence = distribution[category]
    p_to_action = distribution[Category.TO_ACTION]

    # Low confidence is a successful classification the model is unsure about,
    # not a failure. It still gets Processed - it is resolved by hand, not by
    # retrying, and reconciliation strips Needs Review once it is re-filed.
    if confidence < confidence_threshold:
        return Decision(
            labels_add=_sorted(NEEDS_REVIEW, PROCESSED),
            labels_remove=(),
            category=category,
            confidence=confidence,
            needs_review=True,
            to_action_override=False,
        )

    # The asymmetric rule: independently of the argmax, leftover probability on
    # To Action keeps the message visible. Nearly free now the whole
    # distribution is available, and it protects the only error class that
    # costs anything - a missed bill. The message is still FILED as the argmax;
    # only its visibility changes.
    override = category not in KEEPS_INBOX and p_to_action >= to_action_floor
    keeps_inbox = category in KEEPS_INBOX or override

    return Decision(
        labels_add=_sorted(label_for(category), PROCESSED),
        labels_remove=() if keeps_inbox else (INBOX,),
        category=category,
        confidence=confidence,
        needs_review=False,
        to_action_override=override,
    )


def decide_prefilter_hit() -> Decision:
    """Row 6. A sender-domain allowlist match, with no model call made.

    Confidence is None rather than 1.0: the row records a rule firing, and
    writing a fake probability would corrupt the calibration table, which is
    computed from model confidences.
    """
    return Decision(
        labels_add=_sorted(label_for(Category.PROMOTIONS), PROCESSED),
        labels_remove=(INBOX,),
        category=Category.PROMOTIONS,
        confidence=None,
        needs_review=False,
        to_action_override=False,
    )


def decide_failure() -> Decision:
    """Row 7. Ollama unreachable, no valid category letter, a Gmail error.

    Applies NOTHING - not even Agent/Processed. The message stays invisible to
    the system and is naturally re-attempted next run. Conflating this with low
    confidence would let a transient outage permanently pollute the review
    queue with mail that was never actually classified.
    """
    return Decision(
        labels_add=(),
        labels_remove=(),
        category=None,
        confidence=None,
        needs_review=False,
        to_action_override=False,
    )


def decide_dead_letter() -> Decision:
    """Row 8. Repeated failures on the same message; stop retrying it.

    The poison-message problem from queue systems: because a failure applies no
    labels, a message that reliably breaks the classifier stays in the queue and
    is retried on every cycle forever, and a backfill can never finish. After
    `max_failures_before_error` attempts it is dead-lettered instead.

    `Agent/Error` IS the dead-letter queue here - Gmail labels are the state, so
    `label:"Agent/Error"` is the queue you go and read. It is applied alone: the
    stop signal in its own right, so requeuing a message after the underlying
    bug is fixed means removing one label rather than two, and Agent/Processed
    keeps meaning exactly "successfully classified" in the metrics.

    INBOX is retained: the system never established what this message is, so
    nothing has earned the right to archive it. Agent/Error should be empty -
    anything in it is a bug to investigate, not mail to re-file.
    """
    return Decision(
        labels_add=(ERROR,),
        labels_remove=(),
        category=None,
        confidence=None,
        needs_review=False,
        to_action_override=False,
    )


def _sorted(*labels: str) -> tuple[str, ...]:
    """Label order carries no meaning to Gmail; sorting makes tests exact."""
    return tuple(sorted(labels))


def _validate(distribution: Mapping[Category, float]) -> None:
    """A partial distribution is a bug upstream, not a low-confidence result.

    An absent or empty distribution means the classifier failed, which is
    `decide_failure()`. Reaching here with one would silently label mail off a
    distribution that never summed to 1.
    """
    if set(distribution) != set(Category):
        missing = sorted(set(Category) - set(distribution))
        extra = sorted(set(distribution) - set(Category))
        raise ValueError(
            f"distribution must cover all {len(Category)} categories; "
            f"missing={missing} unexpected={extra}"
        )
