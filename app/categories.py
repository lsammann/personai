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

from collections.abc import Collection, Mapping
from dataclasses import dataclass
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
# editing any of them is a prompt change: it needs a new `prompt_id` in
# `classifier.PROMPTS`. `classifier.prompt_hash()` is the backstop that
# catches an edit made without one - two incomparable eval runs filed under
# the same label would invalidate every conclusion drawn from them.
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

# The categories that stay visible in the inbox. The other three produce
# identical behaviour - label it, remove INBOX - which is why a Receipts vs
# Promotions confusion costs nothing and a To Action false negative costs a
# missed bill. See DESIGN.md, "The action space is nearly binary".
#
# BOOKINGS was added in Phase 2 on measurement, not preference. Every costly
# error in the eval - a message that should have stayed visible and was
# archived - was a message the model had called Bookings, across every prompt
# variant tried. Narrowing the category's description took its over-prediction
# from 21 of 200 down to 10, and did not move those messages at all. Keeping
# Bookings visible takes them to zero by construction rather than by
# persuasion, and it costs five extra messages in the inbox per 140.
#
# It is also defensible on its own terms: an upcoming flight or appointment is
# something the reader wants in front of them, which is what INBOX means here.
# The cost is that the archive half is now three categories rather than four,
# so "Receipts vs Bookings is a free error" - an argument several Phase 2
# labelling decisions leaned on - no longer holds.
KEEPS_INBOX = frozenset(
    {Category.TO_ACTION, Category.PERSONAL, Category.BOOKINGS}
)

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

# Sent, drafts and chats are not mail that arrives, so they are never mail
# the agent classifies. The eval frame and the live queue both use this, and
# DESIGN.md makes their consistency a hard requirement, so it is defined
# once, here. A test fails if it is retyped elsewhere (step 8 decision Q1).
RECEIVED_ONLY = "-in:sent -in:drafts -in:chats"


def label_for(category: Category) -> str:
    """`Category.TO_ACTION` -> `Agent/To Action`."""
    return f"{LABEL_PREFIX}{category.value}"


# Every label the agent owns: the six categories plus the three operational
# labels. Built from the names above, never retyped, so a renamed category or
# a new stop label reaches setup_labels without a second edit.
AGENT_LABELS: frozenset[str] = frozenset(
    {label_for(category) for category in Category} | {NEEDS_REVIEW, *STOP_LABELS}
)

# Gmail nesting is naming only: `Agent/Bookings` displays under `Agent` if and
# only if a label named exactly `Agent` exists. Step 2 measured the nine
# showing flat without it. Created by setup, never applied to a message - so
# it is deliberately NOT in AGENT_LABELS, and never in a lookup or the undo
# sweep.
LABEL_PARENT = LABEL_PREFIX.rstrip("/")

# What `scripts/setup_labels.py` ensures exists: the nine plus the parent.
SETUP_LABELS: frozenset[str] = AGENT_LABELS | {LABEL_PARENT}

# Every label a decision can name: the nine, plus INBOX, which a decision
# removes. Each writer resolves exactly these ids once per run. Defined here,
# not in each writer, so the agent and undo cannot need different maps
# (step 8 decision Q1).
REQUIRED_LABELS: frozenset[str] = AGENT_LABELS | {INBOX}


@dataclass(frozen=True)
class LabelSetupPlan:
    """What `scripts/setup_labels.py` would do, given the labels that exist."""

    present: tuple[str, ...]
    to_create: tuple[str, ...]
    # (wanted, existing case variant)
    clashes: tuple[tuple[str, str], ...]


def plan_label_setup(existing: Collection[str]) -> LabelSetupPlan:
    """Sort `SETUP_LABELS` into present, to create, and clashing.

    A clash is an existing label equal to a wanted one ignoring case but not
    exactly - `agent/to action` against `Agent/To Action`. Gmail refuses a
    second label differing only in case, so creating it would fail anyway;
    reporting the clash up front means setup stops before creating anything,
    rather than with a 409 halfway through. Whether to rename the existing
    one is the human's call, never the script's.

    Labels outside `SETUP_LABELS` - INBOX, the reader's own - are ignored.
    """
    existing_set = set(existing)
    by_folded: dict[str, list[str]] = {}
    for name in existing_set:
        by_folded.setdefault(name.casefold(), []).append(name)

    present, to_create, clashes = [], [], []
    for wanted in sorted(SETUP_LABELS):
        if wanted in existing_set:
            present.append(wanted)
            continue
        variants = sorted(by_folded.get(wanted.casefold(), []))
        if variants:
            clashes += [(wanted, variant) for variant in variants]
        else:
            to_create.append(wanted)
    return LabelSetupPlan(tuple(present), tuple(to_create), tuple(clashes))


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
