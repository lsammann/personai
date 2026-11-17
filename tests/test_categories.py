"""Shared constants: the letter mapping, the label names, the argmax tiebreak."""

import ast
from pathlib import Path

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
    assert cat.KEEPS_INBOX == {
        Category.TO_ACTION, Category.PERSONAL, Category.BOOKINGS
    }


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


# --- the agent's labels, and setting them up ---------------------------------


def test_agent_labels_are_the_six_categories_plus_three_operational():
    assert len(cat.AGENT_LABELS) == 9
    assert all(label.startswith(cat.LABEL_PREFIX) for label in cat.AGENT_LABELS)
    assert cat.AGENT_LABELS == (
        {cat.label_for(c) for c in Category} | {cat.NEEDS_REVIEW, *cat.STOP_LABELS}
    )
    # The parent is created by setup but never applied to a message.
    assert cat.LABEL_PARENT == "Agent"
    assert cat.LABEL_PARENT not in cat.AGENT_LABELS
    assert cat.SETUP_LABELS == cat.AGENT_LABELS | {cat.LABEL_PARENT}


def test_label_setup_with_nothing_present_creates_all_ten():
    plan = cat.plan_label_setup(["INBOX", "UNREAD"])
    assert set(plan.to_create) == cat.SETUP_LABELS
    assert plan.present == () and plan.clashes == ()


def test_label_setup_creates_only_what_is_missing():
    plan = cat.plan_label_setup(["INBOX", cat.PROCESSED, cat.ERROR])
    assert set(plan.present) == {cat.PROCESSED, cat.ERROR}
    assert set(plan.to_create) == cat.SETUP_LABELS - {cat.PROCESSED, cat.ERROR}


def test_label_setup_with_all_present_creates_nothing():
    """What idempotence means: a second run is a no-op."""
    plan = cat.plan_label_setup([*cat.SETUP_LABELS, "INBOX"])
    assert plan.to_create == () and plan.clashes == ()
    assert set(plan.present) == cat.SETUP_LABELS


def test_a_case_variant_is_a_clash_not_present_and_not_to_create():
    """Gmail refuses a label differing only in case, so creating it would
    fail; and treating it as present would apply the reader's label."""
    plan = cat.plan_label_setup(["agent/to action"])
    assert plan.clashes == (("Agent/To Action", "agent/to action"),)
    assert "Agent/To Action" not in plan.to_create
    assert "Agent/To Action" not in plan.present


def test_label_setup_ignores_labels_it_does_not_own():
    plan = cat.plan_label_setup(["Agents", "Agent/Something Else", "Receipts"])
    assert set(plan.to_create) == cat.SETUP_LABELS
    assert plan.clashes == ()


# --- shared query and label facts, defined once (step 8 decision Q1) --------

ROOT = Path(__file__).resolve().parent.parent

# Records of past measurements, left as they were measured. By exact path, so
# a new script that retypes the exclusions is caught.
RETYPED_EXCLUSIONS_EXEMPT = frozenset({
    "scripts/spike_gmail.py",
    "scripts/measure_bodies.py",
})


def test_the_exclusions_are_written_only_in_categories():
    """A second copy could drift, and the eval frame and the live queue
    would then disagree with no error (DESIGN.md: input consistency)."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for directory in ("app", "scripts")
        for path in sorted((ROOT / directory).rglob("*.py"))
        if "-in:sent" in path.read_text()
        and path.relative_to(ROOT).as_posix()
        not in RETYPED_EXCLUSIONS_EXEMPT | {"app/categories.py"}
    ]
    assert not offenders, f"import categories.RECEIVED_ONLY instead: {offenders}"


def test_every_exclusions_exemption_still_exists():
    missing = [path for path in RETYPED_EXCLUSIONS_EXEMPT if not (ROOT / path).exists()]
    assert not missing, f"remove these from RETYPED_EXCLUSIONS_EXEMPT: {missing}"


def test_the_eval_frame_uses_the_queue_exclusions():
    from app import evalset

    assert evalset.FRAME_EXCLUSIONS is cat.RECEIVED_ONLY


def test_received_only_excludes_what_never_arrives():
    assert cat.RECEIVED_ONLY.split() == ["-in:sent", "-in:drafts", "-in:chats"]


def test_required_labels_are_the_nine_and_inbox():
    assert cat.REQUIRED_LABELS == cat.AGENT_LABELS | {cat.INBOX}


def test_required_labels_are_defined_only_in_categories():
    """The agent and undo read the same set, so they cannot need different
    label maps."""
    offenders = []
    for directory in ("app", "scripts"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                targets = (
                    node.targets if isinstance(node, ast.Assign)
                    else [node.target] if isinstance(node, ast.AnnAssign)
                    else []
                )
                if any(isinstance(t, ast.Name) and t.id == "REQUIRED_LABELS" for t in targets):
                    offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == ["app/categories.py"]
