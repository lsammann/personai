"""The six categories, their letter mapping, and the `Agent/` label names.

Shared constants, so `decision` and `classifier` do not have to depend on each
other. Nothing here does I/O.

Two vocabularies live in this file and they are deliberately not the same type:

  Category      - the six things the MODEL may output, as an enum
  the label      - `Agent/Needs Review`, `Agent/Processed`, `Agent/Error`,
  constants        plain strings the APP applies

Keeping the operational labels out of the enum makes "the model emitted
Needs Review" unrepresentable rather than merely unlikely.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class Category(StrEnum):
    """StrEnum so a Category serialises to its own name in the JSONL log.

    The values are the label suffixes verbatim - `label_for()` just prefixes
    them - so there is no second list of names to keep in step.

    **Declaration order is the tie-break.** An exact tie in the distribution
    resolves to whichever category is declared first here. That decides only
    the category RECORDED, never a label: if two categories tie at `v` they are
    both the maximum, so the six probabilities sum to at least `2v` and hence
    `v <= 0.5` - a tie can never reach a threshold of 0.8 and always falls
    through to Needs Review. It would become label-deciding only if the
    confidence threshold were configured below 0.5.

    Not to be confused with the precedence rule in the system prompt, which
    tells the MODEL how to choose when an email genuinely fits two categories.
    That one does real work; this is just a deterministic coin-flip.
    """

    TO_ACTION = "To Action"
    RECEIPTS = "Receipts"
    BOOKINGS = "Bookings"
    UPDATES = "Updates"
    PROMOTIONS = "Promotions"
    PERSONAL = "Personal"


# One line each. These are the category definitions the model is given, so
# editing any of them is a prompt change and must bump PROMPT_VERSION in
# `classifier`.
DESCRIPTIONS: Mapping[Category, str] = {
    Category.TO_ACTION: (
        "requires a decision, payment, reply, or click from the reader, "
        "especially anything with a deadline"
    ),
    Category.RECEIPTS: "a transaction already completed; record-keeping only",
    Category.BOOKINGS: (
        "confirmation of something scheduled or reserved; reference only"
    ),
    Category.UPDATES: "low-priority informational content, no action ever needed",
    Category.PROMOTIONS: "marketing content trying to sell something",
    Category.PERSONAL: (
        "written by a real person directly to the reader, not automated or "
        "bulk mail"
    ),
}

# The two categories that stay visible in the inbox. The other four produce
# identical behaviour - label it, remove INBOX - which is why a Receipts vs
# Bookings confusion costs nothing and a To Action false negative costs a
# missed bill. See DESIGN.md, "The action space is nearly binary".
KEEPS_INBOX = frozenset({Category.TO_ACTION, Category.PERSONAL})

# Single-token labels, not category names: category words tokenize into several
# tokens and their first tokens collide ("Receipts"/"Reminders" both begin
# "Re"), which makes the first-token distribution unreadable.
LETTERS = "ABCDEF"

# The fixed production mapping. DESIGN.md: fixed, not randomised - a mapping
# that moved between runs would make logged distributions incomparable.
DEFAULT_ORDER: tuple[Category, ...] = (
    Category.TO_ACTION,
    Category.RECEIPTS,
    Category.BOOKINGS,
    Category.UPDATES,
    Category.PROMOTIONS,
    Category.PERSONAL,
)

LABEL_PREFIX = "Agent/"

NEEDS_REVIEW = f"{LABEL_PREFIX}Needs Review"
PROCESSED = f"{LABEL_PREFIX}Processed"
ERROR = f"{LABEL_PREFIX}Error"
INBOX = "INBOX"

# Three mutually exclusive mailbox states: PROCESSED, ERROR, or neither
# (= unprocessed). These are the two labels that take a message out of the
# work queue.
#
# Splitting dedup across two labels is only dangerous if a query can forget
# one, so every consumer - the poller, the backfill and undo_run.py - builds
# its query from this constant rather than retyping the strings.
STOP_LABELS: tuple[str, ...] = (PROCESSED, ERROR)

def label_term(label: str, *, negate: bool = False) -> str:
    """A Gmail `label:` search term, always quoted.

    Label names contain spaces, and quoting is what keeps that a purely
    cosmetic choice: a quoted term is unambiguous whatever the name is, so no
    query in the project has to care how a label is spelled. Every query is
    built through here rather than by formatting a label into a string at the
    call site.
    """
    return f'{"-" if negate else ""}label:"{label}"'


# Gmail search terms excluding everything already in a terminal state.
UNPROCESSED_TERMS = " ".join(
    label_term(label, negate=True) for label in STOP_LABELS
)


def label_for(category: Category) -> str:
    """`Category.TO_ACTION` -> `Agent/To Action`."""
    return f"{LABEL_PREFIX}{category.value}"


def letter_map(order: tuple[Category, ...] = DEFAULT_ORDER) -> dict[str, Category]:
    """Letter -> category, e.g. `{"A": TO_ACTION, ...}`.

    Parameterised rather than hardcoded because Phase 2's permutation-stability
    metric classifies the same email under a different ordering to check the
    distribution measures content and not alphabet position. Re-deriving that
    mapping inside `eval/` would let the two drift.
    """
    _validate_order(order)
    return {LETTERS[i]: category for i, category in enumerate(order)}


def category_letters(
    order: tuple[Category, ...] = DEFAULT_ORDER,
) -> dict[Category, str]:
    """The inverse of `letter_map`, for building the prompt."""
    return {category: letter for letter, category in letter_map(order).items()}


def argmax(distribution: Mapping[Category, float]) -> Category:
    """The most probable category. Exact ties go to whichever is declared first.

    Iterates `Category` rather than the mapping, so the tie-break is the
    declaration order above rather than whatever insertion order the caller's
    dict happened to have.

    Shared by `classifier` (which reports the model's pick) and `decision`
    (which applies thresholds to it) so the two cannot diverge on what the
    winning category was.
    """
    if set(distribution) != set(Category):
        raise ValueError("argmax needs a probability for every category")
    return max(Category, key=lambda category: distribution[category])


def _validate_order(order: tuple[Category, ...]) -> None:
    if len(order) != len(Category) or set(order) != set(Category):
        raise ValueError(
            f"a letter mapping must cover all {len(Category)} categories "
            f"exactly once, got {list(order)}"
        )
