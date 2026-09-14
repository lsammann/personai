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

import hashlib
import json
import math
import urllib.request
from collections.abc import Callable, Iterable, Mapping
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
from app.message_body import select_body

OLLAMA_URL = "http://localhost:11434/api/chat"

# How many alternatives to ask for. A category letter falling outside this gets
# probability 0 and the log records that it happened.
TOP_LOGPROBS = 20

# Which prompt to build when a caller does not say. A string id rather than the
# integer version this started as: Phase 2 step 6 sweeps named variants, and a
# results file saying `prompt_version: 3` cannot be read back against a table
# of hypotheses. Logged on every row, so a mixed log can be segmented by which
# prompt produced it.
DEFAULT_PROMPT_ID = "v1"

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
    return (
        _preamble()
        + _definitions(order)
        + "\n\n"
        + "Precedence when an email fits two categories:\n"
        + "Anything written by a real human directly to the reader is "
        + f"{Category.PERSONAL.value}. Otherwise {Category.TO_ACTION.value} beats "
        + f"everything. Then {Category.BOOKINGS.value} over "
        + f"{Category.RECEIPTS.value}. Then {Category.PROMOTIONS.value} over "
        + f"{Category.UPDATES.value}.\n\n"
        + _contract(order)
    )


def _preamble() -> str:
    return "You classify emails into exactly one category.\n\n"


def _definitions(
    order: tuple[Category, ...],
    overrides: Mapping[Category, str] | None = None,
) -> str:
    """The lettered category list. `overrides` lets a variant reword one line.

    Overriding here rather than editing `categories.DESCRIPTIONS` keeps the
    variant local to the prompt: `DESCRIPTIONS` is what the live system uses,
    and a sweep must not change it until a variant has actually won.
    """
    descriptions = {**DESCRIPTIONS, **(overrides or {})}
    letters = category_letters(order)
    return "\n".join(
        f"{letters[category]} = {category.value} - {descriptions[category]}"
        for category in order
    )


def _contract(order: tuple[Category, ...]) -> str:
    valid = ", ".join(LETTERS[: len(order)])
    return (
        f"Reply with exactly one character: {valid}. No explanation, no "
        "punctuation, no whitespace before it."
    )


# The precedence as a numbered total order rather than prose. Hypothesis: the
# rule v1 already states - "anything written by a real human is Personal" - was
# ignored on four of the six costly errors in run 20260913T065929-6ee8cd, at
# 0.98 confidence. Prose the model can skim past becomes a list it has to walk.
def build_ordered_prompt(order: tuple[Category, ...] = DEFAULT_ORDER) -> str:
    return (
        _preamble()
        + _definitions(order)
        + "\n\n"
        + "An email often fits more than one category. Work down this list and "
        "choose the FIRST that applies:\n"
        f"1. Written by a real person directly to you - {Category.PERSONAL.value}\n"
        f"2. Requires you to do something - {Category.TO_ACTION.value}\n"
        f"3. A reservation you hold for a future date - {Category.BOOKINGS.value}\n"
        f"4. A completed transaction - {Category.RECEIPTS.value}\n"
        f"5. Trying to sell you something - {Category.PROMOTIONS.value}\n"
        f"6. Otherwise - {Category.UPDATES.value}\n\n"
        + _contract(order)
    )


# Bookings, narrowed. Measured basis: all six costly errors in run
# 20260913T065929-6ee8cd were predicted Bookings, and the word "confirmation"
# appears in three of the four subject lines while the sixth arrives from
# bookings@anaesthesia-analgesia.com.au. The category is described as
# "confirmation of something scheduled or reserved", so the model is keying on
# one token. Cheap to try: there are 6 true Bookings in the whole set, and a
# Bookings/Receipts slip costs nothing because both archive.
BOOKINGS_NARROWED = (
    "a reservation you hold for a future date - a flight, hotel, restaurant, "
    "event or appointment. Not every email containing the word "
    '"confirmation"'
)


# v9 narrowed Bookings but listed "appointment" among the things it covers -
# and the message it still got wrong, at 0.984 confidence, was "FW: Appointment
# Confirmation MT WAVERLEY". The description invited the error it was written
# to prevent.
#
# v9b drops that word and moves the Personal exclusion INSIDE the description.
# Different hypothesis from v2-ordered, which made Personal globally dominant
# and over-applied it to 25 messages against a truth of 16: state the rule at
# the point where the category is over-firing, rather than everywhere.
BOOKINGS_NARROWED_B = (
    "a reservation you hold for a future date - a flight, hotel, restaurant "
    "or event ticket. Not an email that merely mentions a booking or a "
    'confirmation, and never something a person wrote to you directly'
)


def build_narrow_bookings_b_prompt(
    order: tuple[Category, ...] = DEFAULT_ORDER,
) -> str:
    return (
        _preamble()
        + _definitions(order, {Category.BOOKINGS: BOOKINGS_NARROWED_B})
        + "\n\n"
        + "Precedence when an email fits two categories:\n"
        + "Anything written by a real human directly to the reader is "
        + f"{Category.PERSONAL.value}. Otherwise {Category.TO_ACTION.value} beats "
        + f"everything. Then {Category.BOOKINGS.value} over "
        + f"{Category.RECEIPTS.value}. Then {Category.PROMOTIONS.value} over "
        + f"{Category.UPDATES.value}.\n\n"
        + _contract(order)
    )


# "low-priority" in the Updates description is a judgement about the reader's
# interest welded to a structural claim, and only the second is the taxonomy's
# business. Raised during labelling: a serious, high-interest, no-action email
# reads as excluded by its own category. Updates is also the most
# under-predicted category by a distance - 5 predicted against a truth of 21.
UPDATES_REWORDED = "informational content; no action is ever needed"


def build_updates_prompt(order: tuple[Category, ...] = DEFAULT_ORDER) -> str:
    return (
        _preamble()
        + _definitions(order, {Category.UPDATES: UPDATES_REWORDED})
        + "\n\n"
        + "Precedence when an email fits two categories:\n"
        + "Anything written by a real human directly to the reader is "
        + f"{Category.PERSONAL.value}. Otherwise {Category.TO_ACTION.value} beats "
        + f"everything. Then {Category.BOOKINGS.value} over "
        + f"{Category.RECEIPTS.value}. Then {Category.PROMOTIONS.value} over "
        + f"{Category.UPDATES.value}.\n\n"
        + _contract(order)
    )


# v10, written after step 7 flipped the cost model. v9/v9b narrowed Bookings
# because, while Bookings ARCHIVED, every costly error was a Bookings
# prediction. Step 6 then moved Bookings into KEEPS_INBOX, which reverses the
# asymmetry: on run 20260913T113909-f1f3a4 all five false Bookings predictions
# land on messages that keep the inbox anyway, so they cause zero clutter
# errors, while the single costly error in the run is a true Bookings the model
# called Receipts. Over-prediction is now free and under-prediction is not.
#
# The measured target is one message: United's "eTicket Itinerary and Receipt",
# Receipts at 0.970 with p(Bookings) = 0.027 - against Qantas's "Confirmation
# and E-Ticket Flight Itinerary", Bookings at 0.991. Same genre, opposite
# answers, so the model is keying on the literal token "receipt" (in the
# subject, and in Receipts@united.com) rather than on the itinerary.
#
# The fix belongs in the DESCRIPTION, not the precedence block: the prompt
# already says "Bookings over Receipts", and it did not help because at 0.027
# the model never had Bookings in play to apply precedence to. One added
# sentence, so the result is attributable.
BOOKINGS_ITINERARY = (
    "a reservation you hold for a future date - a flight, hotel, restaurant "
    "or event ticket. A ticket or itinerary for a trip that has not happened "
    "yet belongs here even when the same email is also the receipt for it. "
    "Not an email that merely mentions a booking or a confirmation, and never "
    "something a person wrote to you directly"
)


def build_itinerary_prompt(order: tuple[Category, ...] = DEFAULT_ORDER) -> str:
    """v10-itinerary on top of v9b, the current best - so the comparison is to it."""
    return (
        _preamble()
        + _definitions(order, {Category.BOOKINGS: BOOKINGS_ITINERARY})
        + "\n\n"
        + "Precedence when an email fits two categories:\n"
        + "Anything written by a real human directly to the reader is "
        + f"{Category.PERSONAL.value}. Otherwise {Category.TO_ACTION.value} beats "
        + f"everything. Then {Category.BOOKINGS.value} over "
        + f"{Category.RECEIPTS.value}. Then {Category.PROMOTIONS.value} over "
        + f"{Category.UPDATES.value}.\n\n"
        + _contract(order)
    )


# Nothing in v1 tells the model what it is looking at. Two of the hardest
# errors in the eval are the model reading the From ADDRESS as a category: an
# unpaid invoice from bookings@anaesthesia-analgesia.com.au called Bookings,
# and a flight e-ticket from Receipts@united.com called Receipts, both above
# 0.94 confidence. This names the fields and says what the address is.
#
# Two sentences rather than one, which stretches the one-variable-per-variant
# rule - but they are the same intervention: orient the model to its input.
INPUT_SHAPE = (
    "Each email is given to you as a From: line with the sender's address, a "
    "Subject: line, then a blank line and the body text. The address in the "
    "From: line is where the mail came from, not what kind of mail it is.\n\n"
)


def build_format_prompt(order: tuple[Category, ...] = DEFAULT_ORDER) -> str:
    """v4-format on top of v9b, the current best - so the comparison is to it."""
    return (
        _preamble()
        + INPUT_SHAPE
        + _definitions(order, {Category.BOOKINGS: BOOKINGS_NARROWED_B})
        + "\n\n"
        + "Precedence when an email fits two categories:\n"
        + "Anything written by a real human directly to the reader is "
        + f"{Category.PERSONAL.value}. Otherwise {Category.TO_ACTION.value} beats "
        + f"everything. Then {Category.BOOKINGS.value} over "
        + f"{Category.RECEIPTS.value}. Then {Category.PROMOTIONS.value} over "
        + f"{Category.UPDATES.value}.\n\n"
        + _contract(order)
    )


def build_narrow_bookings_prompt(
    order: tuple[Category, ...] = DEFAULT_ORDER,
) -> str:
    return (
        _preamble()
        + _definitions(order, {Category.BOOKINGS: BOOKINGS_NARROWED})
        + "\n\n"
        + "Precedence when an email fits two categories:\n"
        + "Anything written by a real human directly to the reader is "
        + f"{Category.PERSONAL.value}. Otherwise {Category.TO_ACTION.value} beats "
        + f"everything. Then {Category.BOOKINGS.value} over "
        + f"{Category.RECEIPTS.value}. Then {Category.PROMOTIONS.value} over "
        + f"{Category.UPDATES.value}.\n\n"
        + _contract(order)
    )


# Prompt templates by id. DESIGN.md names `classifier` as their home, and the
# Phase 2 sweep needs them addressable by name: a results file saying
# `prompt_id: "v3-letters"` can be read back against the hypothesis table in
# docs/PHASE2_PLAN.md, where `prompt_version: 3` cannot.
#
# `v1` is `build_system_prompt` itself rather than a copy of its text, so the
# baseline is literally the committed prompt and cannot drift from it.
# Variants are added immediately before the run that tests them - writing all
# eight now would leave untested prompt strings in the tree for days, attached
# to hypotheses that the baseline numbers may well revise.
PROMPTS: dict[str, Callable[[tuple[Category, ...]], str]] = {
    "v1": build_system_prompt,
    "v2-ordered": build_ordered_prompt,
    "v9-bookings": build_narrow_bookings_prompt,
    "v9b-bookings": build_narrow_bookings_b_prompt,
    "v8-updates": build_updates_prompt,
    "v4-format": build_format_prompt,
    "v10-itinerary": build_itinerary_prompt,
}


def system_prompt(
    prompt_id: str, order: tuple[Category, ...] = DEFAULT_ORDER
) -> str:
    """Build a prompt by id. An unknown id raises rather than defaulting.

    Loudly, because the failure it prevents is silent: a typo in a sweep that
    fell back to `v1` would file a run under the wrong label, and nothing in
    the results file or the report would look wrong. Every conclusion drawn
    from the comparison afterwards would be invalid.
    """
    try:
        template = PROMPTS[prompt_id]
    except KeyError:
        raise KeyError(
            f"unknown prompt_id {prompt_id!r}; known: {sorted(PROMPTS)}"
        ) from None
    return template(order)


def prompt_hash(
    prompt_id: str, order: tuple[Category, ...] = DEFAULT_ORDER
) -> str:
    """A short digest of the built prompt text, for the eval run manifest.

    The id says which template; this says what that template actually produced.
    They come apart the moment `categories.DESCRIPTIONS` is edited without
    bumping the id - the category definitions ARE the prompt - and the result
    is two incomparable runs filed under one label, which invalidates every
    conclusion after it. Recorded beside `model`, `body_chars` and
    `extraction_version` for exactly the same reason each of those is.
    """
    text = system_prompt(prompt_id, order)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def build_user_message(
    sender: str, subject: str, text_plain: str, text_html: str, body_chars: int
) -> str:
    """Sender, subject, and a selected, truncated body.

    **This is the only place model input is constructed.** DESIGN.md makes
    input consistency a hard requirement - the poller and the backfill must
    build the model's input from the same fields, fetched the same way - and
    taking the raw text parts rather than a finished body is what makes that
    structural instead of a line in a document. A caller cannot reach the model
    having picked its own body, because picking happens in here.

    Truncation is the dominant performance lever, not a detail: the call emits
    a single token, so latency is essentially prompt length divided by the
    prompt-eval rate. Measured over the 200-message eval cache under
    EXTRACTION_VERSION v4 (docs/BACKLOG.md -> Body length under v4), the
    selected body has a median of 1,947 characters for plain-text mail and
    1,182 for stripped HTML. Those were 5,637 and 1,598 before URL rewriting,
    so the gap that had one `body_chars` doing two rather different jobs has
    largely closed - but which value is right is still what the Phase 2 sweep
    settles.
    """
    body = select_body(text_plain, text_html).text
    return f"From: {sender}\nSubject: {subject}\n\n{body[:body_chars]}"


def classify(
    sender: str,
    subject: str,
    text_plain: str,
    text_html: str,
    *,
    model: str,
    body_chars: int,
    prompt_id: str = DEFAULT_PROMPT_ID,
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

    `text_html` is deliberately not defaulted. Both text parts are required so
    that the single-path guarantee in `build_user_message` is structural rather
    than conventional: with a default, a caller holding a `Message` could pass
    only `text_plain` and silently classify the 23% of HTML-only mail on an
    empty body - the exact failure `message_body` exists to prevent, reachable
    by omission. An explicit `""` is the caller stating there is no HTML part.

    `prompt_id` and `order` are the two knobs the Phase 2 sweep turns here.
    Both are recorded in the eval run manifest, because a prediction is only
    comparable to another made under the same pair.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt(prompt_id, order)},
            {
                "role": "user",
                "content": build_user_message(
                    sender, subject, text_plain, text_html, body_chars
                ),
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
