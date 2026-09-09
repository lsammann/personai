"""The label decision table - every row, both boundaries, and the invariants.

The table (T = confidence_threshold, F = to_action_floor, both compared with >=):

  1  p>=T, argmax To Action            -> To Action + Processed,   keeps INBOX
  2  p>=T, argmax Personal             -> Personal  + Processed,   keeps INBOX
  3  p>=T, argmax archive-four, pA<F   -> <argmax>  + Processed,   removes INBOX
  4  p>=T, argmax archive-four, pA>=F  -> <argmax>  + Processed,   keeps INBOX
  5  p<T                               -> Needs Review + Processed, keeps INBOX
  6  prefilter hit                     -> Promotions + Processed,  removes INBOX
  7  failure                           -> nothing at all
  8  dead letter (repeated failures)   -> Error only,              keeps INBOX
"""

import ast
from pathlib import Path

import pytest

from app import decision
from app.categories import ERROR, INBOX, NEEDS_REVIEW, PROCESSED, Category, label_for

T = 0.8
F = 0.15

ARCHIVED = [
    Category.RECEIPTS,
    Category.BOOKINGS,
    Category.UPDATES,
    Category.PROMOTIONS,
]


def dist(winner, confidence, *, p_to_action=0.0):
    """A valid six-key distribution with `winner` at `confidence`.

    Does not sum to 1 in every case, deliberately - `decide()` must not care.
    Renormalisation is the classifier's job and is tested there.
    """
    d = dict.fromkeys(Category, 0.0)
    d[Category.TO_ACTION] = p_to_action
    d[winner] = confidence
    return d


def decide(d):
    return decision.decide(d, confidence_threshold=T, to_action_floor=F)


# --- rows 1 and 2: confident, and the category keeps INBOX -----------------

@pytest.mark.parametrize("category", [Category.TO_ACTION, Category.PERSONAL])
def test_confident_and_keeps_inbox(category):
    p_to_action = 0.9 if category is Category.TO_ACTION else 0.0
    result = decide(dist(category, 0.9, p_to_action=p_to_action))
    assert result.labels_add == tuple(sorted([label_for(category), PROCESSED]))
    assert result.labels_remove == ()
    assert result.category is category
    assert result.confidence == 0.9
    assert not result.needs_review
    assert not result.to_action_override


# --- row 3: confident, archive-destined, no To Action signal ---------------

@pytest.mark.parametrize("category", ARCHIVED)
def test_confident_and_archived(category):
    result = decide(dist(category, 0.9, p_to_action=0.05))
    assert result.labels_add == tuple(sorted([label_for(category), PROCESSED]))
    assert result.labels_remove == (INBOX,)
    assert not result.to_action_override


# --- row 4: the asymmetric rule, the one row that protects a missed bill ---

@pytest.mark.parametrize("category", ARCHIVED)
def test_to_action_floor_keeps_inbox_despite_a_different_argmax(category):
    """Filed as the argmax, but left visible. Category and inbox state disagree
    here and only here."""
    result = decide(dist(category, 0.9, p_to_action=0.2))
    assert result.labels_add == tuple(sorted([label_for(category), PROCESSED]))
    assert result.labels_remove == ()
    assert result.category is category      # still filed as the argmax
    assert result.to_action_override


# --- row 5: low confidence ------------------------------------------------

@pytest.mark.parametrize("category", list(Category))
def test_low_confidence_goes_to_needs_review(category):
    result = decide(dist(category, 0.6, p_to_action=0.6 if category is Category.TO_ACTION else 0.0))
    assert result.labels_add == tuple(sorted([NEEDS_REVIEW, PROCESSED]))
    assert result.labels_remove == ()
    assert result.needs_review
    # The model's pick is still reported, so the log records what it thought.
    assert result.category is category


def test_low_confidence_with_to_action_signal_still_just_needs_review():
    """Needs Review keeps INBOX unconditionally, so the floor is a no-op here.
    Asserted to prove the two rules compose rather than fight."""
    result = decide(dist(Category.RECEIPTS, 0.5, p_to_action=0.4))
    assert result.labels_add == tuple(sorted([NEEDS_REVIEW, PROCESSED]))
    assert result.labels_remove == ()


# --- boundaries -----------------------------------------------------------

def test_confidence_exactly_at_threshold_counts_as_confident():
    result = decide(dist(Category.RECEIPTS, T, p_to_action=0.0))
    assert not result.needs_review
    assert result.labels_remove == (INBOX,)


def test_just_below_threshold_is_needs_review():
    result = decide(dist(Category.RECEIPTS, T - 0.001, p_to_action=0.0))
    assert result.needs_review


def test_to_action_floor_exactly_at_boundary_fires():
    result = decide(dist(Category.RECEIPTS, 0.85, p_to_action=F))
    assert result.to_action_override
    assert result.labels_remove == ()


def test_just_below_the_floor_does_not_fire():
    result = decide(dist(Category.RECEIPTS, 0.85, p_to_action=F - 0.001))
    assert not result.to_action_override
    assert result.labels_remove == (INBOX,)


def test_to_action_argmax_below_threshold_is_needs_review_not_to_action():
    """The one that would be tempting to special-case. It must not be."""
    result = decide(dist(Category.TO_ACTION, 0.6, p_to_action=0.6))
    assert result.labels_add == tuple(sorted([NEEDS_REVIEW, PROCESSED]))
    assert not result.to_action_override


def test_a_tie_can_never_clear_the_confidence_threshold():
    """Two categories tied at v are both the maximum, so the six probabilities
    sum to at least 2v; they sum to 1, therefore v <= 0.5. A tie is
    arithmetically guaranteed to fall through to Needs Review at any threshold
    above 0.5, which is why the tie-break decides only what gets LOGGED and
    never what gets labelled."""
    d = dict.fromkeys(Category, 0.0)
    d[Category.UPDATES] = 0.5
    d[Category.BOOKINGS] = 0.5

    result = decide(d)
    assert result.labels_add == tuple(sorted([NEEDS_REVIEW, PROCESSED]))
    # The category is still reported, by declaration order, for the log.
    assert result.category is Category.BOOKINGS


# --- rows 6, 7, 8 ---------------------------------------------------------

def test_prefilter_hit():
    result = decision.decide_prefilter_hit()
    assert result.labels_add == tuple(
        sorted([label_for(Category.PROMOTIONS), PROCESSED])
    )
    assert result.labels_remove == (INBOX,)
    assert result.category is Category.PROMOTIONS
    # No model ran, so there is no confidence to report. A fake 1.0 here would
    # corrupt the calibration table.
    assert result.confidence is None


def test_failure_applies_nothing_at_all():
    """The single most important assertion in this file.

    A failure marked Processed is a message the system will never look at
    again, silently, forever."""
    result = decision.decide_failure()
    assert result.labels_add == ()
    assert result.labels_remove == ()
    assert PROCESSED not in result.labels_add
    assert result.category is None


def test_dead_letter_applies_error_only():
    result = decision.decide_dead_letter()
    assert result.labels_add == (ERROR,)
    assert PROCESSED not in result.labels_add
    assert result.labels_remove == ()      # never established what it is


# --- invariants across every decision the module can produce ---------------

def all_decisions():
    yield decide(dist(Category.TO_ACTION, 0.9, p_to_action=0.9))
    yield decide(dist(Category.PERSONAL, 0.9))
    yield decide(dist(Category.RECEIPTS, 0.9, p_to_action=0.05))
    yield decide(dist(Category.RECEIPTS, 0.9, p_to_action=0.2))
    yield decide(dist(Category.UPDATES, 0.5))
    yield decision.decide_prefilter_hit()
    yield decision.decide_failure()
    yield decision.decide_dead_letter()


def test_processed_and_error_are_mutually_exclusive():
    """The three mailbox states: Processed, Error, or neither. Never both."""
    for result in all_decisions():
        assert not (PROCESSED in result.labels_add and ERROR in result.labels_add)


def test_every_non_failure_decision_reaches_a_terminal_state():
    for result in all_decisions():
        if result.labels_add == ():
            continue        # row 7, the retry path
        assert len({PROCESSED, ERROR} & set(result.labels_add)) == 1


def test_inbox_is_only_ever_removed_never_added():
    for result in all_decisions():
        assert INBOX not in result.labels_add


@pytest.mark.parametrize(
    "keeper", [Category.TO_ACTION, Category.PERSONAL]
)
def test_inbox_never_removed_for_the_visible_categories(keeper):
    p_to_action = 0.9 if keeper is Category.TO_ACTION else 0.0
    assert decide(dist(keeper, 0.99, p_to_action=p_to_action)).labels_remove == ()


def test_needs_review_and_error_never_remove_inbox():
    assert decide(dist(Category.PROMOTIONS, 0.3)).labels_remove == ()
    assert decision.decide_dead_letter().labels_remove == ()


# --- malformed input ------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    [
        {},                                              # a failure, not a result
        {Category.TO_ACTION: 1.0},                       # partial
        dict.fromkeys(list(Category)[:5], 0.2),          # one missing
    ],
)
def test_incomplete_distribution_raises(bad):
    with pytest.raises(ValueError):
        decide(bad)


# --- the purity claim, made executable ------------------------------------

def test_decision_module_imports_nothing_impure():
    """CLAUDE.md says this file stays pure and that it only holds if it stays
    isolated. Reviews forget; this does not."""
    allowed = {"app.categories", "collections.abc", "dataclasses", "typing",
               "__future__"}
    source = Path(decision.__file__).read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert imported <= allowed, f"impure imports: {sorted(imported - allowed)}"
