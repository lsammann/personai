"""Shared constants: the letter mapping, the label names, the argmax tiebreak."""

import pytest

from app import categories as cat
from app.categories import Category


def test_six_categories():
    assert len(Category) == 6


def test_every_category_has_a_description():
    """A missing description would silently drop a category from the prompt."""
    assert set(cat.DESCRIPTIONS) == set(Category)
    assert all(cat.DESCRIPTIONS[c].strip() for c in Category)


def all_label_names():
    return [cat.label_for(c) for c in Category] + [
        cat.NEEDS_REVIEW, cat.PROCESSED, cat.ERROR
    ]


def test_every_label_is_nested_under_agent():
    """One parent in the sidebar, and `Agent/` is what undo_run.py strips."""
    assert all(label.startswith("Agent/") for label in all_label_names())


def test_query_terms_are_always_quoted():
    """Quoting is what makes the label naming purely cosmetic - a quoted term
    is unambiguous whatever the name contains, so no query has to care."""
    for label in all_label_names():
        assert cat.label_term(label) == f'label:"{label}"'
        assert cat.label_term(label, negate=True) == f'-label:"{label}"'


def test_multi_word_labels_survive_the_query_builder():
    """The names with spaces are the whole reason label_term exists."""
    assert cat.label_term(cat.NEEDS_REVIEW) == 'label:"Agent/Needs Review"'
    assert cat.label_for(Category.TO_ACTION) == "Agent/To Action"


def test_operational_labels_are_not_categories():
    """The model must be structurally unable to emit these."""
    values = {c.value for c in Category}
    for label in (cat.NEEDS_REVIEW, cat.PROCESSED, cat.ERROR):
        assert label.removeprefix(cat.LABEL_PREFIX) not in values


def test_keeps_inbox_is_exactly_to_action_and_personal():
    assert cat.KEEPS_INBOX == {Category.TO_ACTION, Category.PERSONAL}


def test_letter_map_is_a_bijection():
    mapping = cat.letter_map()
    assert list(mapping) == list("ABCDEF")
    assert set(mapping.values()) == set(Category)


def test_category_letters_inverts_letter_map():
    assert cat.category_letters() == {
        c: letter for letter, c in cat.letter_map().items()
    }


def test_letter_map_accepts_a_permutation():
    """Phase 2 measures permutation stability by re-mapping the same categories."""
    permuted = tuple(reversed(cat.DEFAULT_ORDER))
    mapping = cat.letter_map(permuted)
    assert mapping["A"] == Category.PERSONAL
    assert set(mapping.values()) == set(Category)


@pytest.mark.parametrize(
    "order",
    [
        (Category.TO_ACTION,),                       # too few
        cat.DEFAULT_ORDER + (Category.PERSONAL,),    # too many
        (Category.TO_ACTION,) * 6,                   # duplicates
    ],
)
def test_letter_map_rejects_a_bad_order(order):
    with pytest.raises(ValueError):
        cat.letter_map(order)


def test_stop_labels_drive_the_query_terms():
    """The queue query is built from the constant, never retyped."""
    assert cat.STOP_LABELS == (cat.PROCESSED, cat.ERROR)
    assert cat.UNPROCESSED_TERMS == (
        '-label:"Agent/Processed" -label:"Agent/Error"'
    )


def test_argmax_picks_the_highest():
    dist = dict.fromkeys(Category, 0.1)
    dist[Category.BOOKINGS] = 0.5
    assert cat.argmax(dist) == Category.BOOKINGS


def test_argmax_ties_go_to_the_first_declared_category():
    """Declaration order is the documented tie-break. Receipts is declared
    before Promotions, so it wins an exact tie."""
    dist = dict.fromkeys(Category, 0.0)
    dist[Category.PROMOTIONS] = 0.5
    dist[Category.RECEIPTS] = 0.5
    assert cat.argmax(dist) == Category.RECEIPTS


def test_argmax_ignores_the_callers_dict_ordering():
    """The reason argmax iterates Category rather than the mapping: otherwise
    the tie-break would be whatever insertion order the caller happened to
    build, which is not a rule anyone could rely on."""
    promotions_first = {Category.PROMOTIONS: 0.5, Category.RECEIPTS: 0.5} | {
        c: 0.0 for c in Category if c not in (Category.PROMOTIONS, Category.RECEIPTS)
    }
    assert next(iter(promotions_first)) is Category.PROMOTIONS
    assert cat.argmax(promotions_first) == Category.RECEIPTS


def test_argmax_rejects_an_incomplete_distribution():
    with pytest.raises(ValueError):
        cat.argmax({})
    with pytest.raises(ValueError):
        cat.argmax({Category.TO_ACTION: 1.0})
