"""Classification: turning an email into a probability distribution.

Split in two, and the split is the point:

  interpret()  pure. A raw top-20 logprob list -> a normalised distribution
               over the six categories. Every malformed-input case is testable
               here without a socket.
  classify()   the Ollama HTTP call. Mocked in tests; its behaviour is measured
               by the eval set, never asserted in a unit test.

Confidence is the renormalised first-token distribution, never a number the
model reports about itself. Phase 0 measured self-reported confidence as
carrying no signal on any model tested - one was inversely calibrated, another
emitted 0.900 for every email. See DESIGN.md -> Classification logic.
"""

from __future__ import annotations

import json
import math
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.categories import (
    DEFAULT_ORDER,
    DESCRIPTIONS,
    LETTERS,
    Category,
    argmax,
    category_letters,
    letter_map,
)

OLLAMA_URL = "http://localhost:11434/api/chat"

# How many alternatives to ask for. A category letter falling outside this gets
# probability 0 and the log records that it happened.
TOP_LOGPROBS = 20

# Bump on every prompt or category-definition edit, including a change to
# DESCRIPTIONS in `categories`. Logged on every row, so a mixed log can be
# segmented by which prompt produced it.
PROMPT_VERSION = 1

# Measured identical to three decimal places at 0.5, 1.0 and 2.0 in Phase 0 -
# reported logprobs are pre-temperature on this stack. Pinned anyway to
# document the intent; nothing depends on it.
TEMPERATURE = 1.0

DEFAULT_TIMEOUT = 600


class ClassifierError(Exception):
    """Base for everything that means "no usable classification"."""


class NoCategoryLetter(ClassifierError):
    """The response carried no valid category letter.

    A failure, not a low-confidence result: it applies no labels and the
    message is retried.
    """


class OllamaError(ClassifierError):
    """Ollama was unreachable or answered with an error."""


@dataclass(frozen=True)
class Interpretation:
    """A model result, before any threshold is applied."""

    distribution: dict[Category, float]
    category: Category
    confidence: float
    # Letters that never appeared in the top-20, so scored exactly 0.0. Goes to
    # the log's `note` field: a truncated distribution is worth knowing about
    # when reading back a surprising decision.
    missing_letters: tuple[str, ...]

    # How much of the returned top-20 sat on category letters at all, BEFORE
    # renormalising. Renormalising answers "given the answer is one of A-F,
    # which is it?", which is the right question - but it means a response that
    # was 94% prose and 6% letters reports the same confidence as one that was
    # 99% letters. This is the only field that tells those apart, so it is
    # logged. Low values mean the model barely engaged with the output format
    # and the confidence beside it is worth less than it looks.
    #
    # Bounded by the top-20 window: it is the observable letter mass, not the
    # true one over the whole vocabulary.
    retained_mass: float


def interpret(
    top_logprobs: Iterable[Mapping[str, object]],
    mapping: Mapping[str, Category] | None = None,
) -> Interpretation:
    """Raw top-20 entries -> a distribution summing to 1 over all six categories.

    Probability mass is SUMMED across every token spelling the same letter:
    "A", " A" and "a" are one answer, not three. Taking only the highest-ranked
    variant (what the Phase 0 spike did) discards real mass and can push a
    genuinely confident answer below the threshold.

    Mass on anything that is not a category letter is dropped, not carried into
    the denominator - see `Interpretation.retained_mass` for why that is the
    right call and what it costs.
    """
    mapping = letter_map() if mapping is None else mapping

    mass: dict[Category, float] = {}
    for entry in top_logprobs:
        token = entry.get("token")
        if not isinstance(token, str):
            continue
        category = mapping.get(token.strip().upper())
        if category is None:
            continue
        mass[category] = mass.get(category, 0.0) + math.exp(float(entry["logprob"]))

    if not mass:
        raise NoCategoryLetter(
            "no category letter in the returned token distribution"
        )

    retained_mass = sum(mass.values())
    distribution = {
        category: mass.get(category, 0.0) / retained_mass for category in Category
    }

    category = argmax(distribution)
    missing = tuple(
        letter for letter, cat in sorted(mapping.items()) if cat not in mass
    )
    return Interpretation(
        distribution=distribution,
        category=category,
        confidence=distribution[category],
        missing_letters=missing,
        retained_mass=retained_mass,
    )


def build_system_prompt(order: tuple[Category, ...] = DEFAULT_ORDER) -> str:
    """The category definitions, the precedence rule, and the output contract.

    The precedence rule is stated explicitly because several emails legitimately
    belong to two categories - a flight receipt is both a Receipt and a Booking.
    Without a tiebreak the model dithers and floods Needs Review with items
    where either answer was fine.
    """
    letters = category_letters(order)
    definitions = "\n".join(
        f"{letters[category]} = {category.value} - {DESCRIPTIONS[category]}"
        for category in order
    )
    valid = ", ".join(LETTERS[: len(order)])
    return (
        "You classify emails into exactly one category.\n\n"
        f"{definitions}\n\n"
        "Precedence when an email fits two categories:\n"
        "Anything written by a real human directly to the reader is "
        f"{Category.PERSONAL.value}. Otherwise {Category.TO_ACTION.value} beats "
        f"everything. Then {Category.BOOKINGS.value} over "
        f"{Category.RECEIPTS.value}. Then {Category.PROMOTIONS.value} over "
        f"{Category.UPDATES.value}.\n\n"
        f"Reply with exactly one character: {valid}. No explanation, no "
        "punctuation, no whitespace before it."
    )


def build_user_message(sender: str, subject: str, body: str, body_chars: int) -> str:
    """Sender, subject, and a truncated body.

    Truncation is the dominant performance lever, not a detail: the call emits
    a single token, so latency is essentially prompt length divided by the
    prompt-eval rate. Real bodies have a median of ~7,400 characters - see
    docs/BACKLOG.md - so this cut is doing most of the work.
    """
    return f"From: {sender}\nSubject: {subject}\n\n{body[:body_chars]}"


def classify(
    sender: str,
    subject: str,
    body: str,
    *,
    model: str,
    body_chars: int,
    order: tuple[Category, ...] = DEFAULT_ORDER,
    url: str = OLLAMA_URL,
    timeout: int = DEFAULT_TIMEOUT,
) -> Interpretation:
    """One forward pass, one token, then `interpret()` on the distribution.

    No chain-of-thought is requested. Emitting reasoning first would condition
    the category on that reasoning, so the token distribution would measure
    agreement-with-its-own-argument rather than confidence in the answer. Phase
    0 measured the accuracy cost of dropping it at roughly zero.

    Raises rather than retrying: retry-with-delay is per-message orchestration
    and belongs to the caller, which also decides when repeated failures tip a
    message into the dead-letter path.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": build_system_prompt(order)},
            {
                "role": "user",
                "content": build_user_message(sender, subject, body, body_chars),
            },
        ],
        "stream": False,
        "logprobs": True,
        "top_logprobs": TOP_LOGPROBS,
        # num_predict: 1 - we want the first token's distribution and nothing
        # after it.
        "options": {"temperature": TEMPERATURE, "num_predict": 1},
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.load(response)
    # OSError alone is the right net: urllib's URLError and HTTPError are both
    # OSError subclasses, and so is TimeoutError. Naming them individually would
    # read as three distinct cases when it is one.
    except OSError as exc:
        raise OllamaError(f"Ollama call failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OllamaError(f"Ollama returned invalid JSON: {exc}") from exc

    entries = parsed.get("logprobs") or []
    if not entries:
        said = parsed.get("message", {}).get("content", "")
        raise NoCategoryLetter(f"no logprobs in response (model said {said!r})")

    return interpret(entries[0].get("top_logprobs") or [], mapping=letter_map(order))
