"""Undo's pure core: the window, row selection, the plan, orphans, verify.

Gmail is not involved: `app/undo.py` does no I/O, and every message here is
just a set of label ids. Names map to fake ids (`Label_<n>`); INBOX is its
own id, as in Gmail.
"""

import datetime as dt

import pytest

from app import undo
from app.categories import (
    AGENT_LABELS,
    ERROR,
    INBOX,
    NEEDS_REVIEW,
    PROCESSED,
    Category,
    label_for,
)
from app.logbook import Action, Record

NOW = dt.datetime(2026, 11, 3, 15, 0, tzinfo=dt.UTC)
IDS = {name: f"Label_{n}" for n, name in enumerate(sorted(AGENT_LABELS), start=2)}
IDS[INBOX] = INBOX
RECEIPTS, TO_ACTION = label_for(Category.RECEIPTS), label_for(Category.TO_ACTION)


def ids(*names):
    return {IDS[name] for name in names}


def row(*, at="2026-11-03T14:00Z", add=(), remove=(), prior=(INBOX, "UNREAD"),
        after=None, message_id="m1", dry_run=False, error=None):
    return Record(
        timestamp=dt.datetime.fromisoformat(at), run_id="r", run_kind="manual",
        message_id=message_id, source="model", action=Action(add=add, remove=remove),
        prior_labels=prior, labels_after=after, dry_run=dry_run, error=error,
    )


def archived_receipt(**overrides):
    """M1's row: filed as Receipts, taken out of the inbox."""
    fields = {"add": (PROCESSED, RECEIPTS), "remove": (INBOX,),
              "after": ("UNREAD", IDS[PROCESSED], IDS[RECEIPTS])}
    return row(**(fields | overrides))


WINDOW = undo.Window(
    dt.datetime(2026, 11, 3, 13, 0, tzinfo=dt.UTC),
    dt.datetime(2026, 11, 3, 15, 0, tzinfo=dt.UTC),
)


# --- parse_window -----------------------------------------------------------

def test_a_window_with_z_and_offsets_parses_to_the_same_instants():
    """+01:00 is converted, not ignored: 14:00+01:00 is 13:00Z."""
    window = undo.parse_window("2026-11-03T14:00+01:00", "2026-11-03T14:30Z", now=NOW)
    assert window.since == dt.datetime(2026, 11, 3, 13, 0, tzinfo=dt.UTC)
    assert window.until == dt.datetime(2026, 11, 3, 14, 30, tzinfo=dt.UTC)


def test_offsets_that_make_an_empty_window_are_refused():
    """15:00+01:00 is 14:00Z, the same instant as --since."""
    with pytest.raises(ValueError, match="before"):
        undo.parse_window("2026-11-03T14:00Z", "2026-11-03T15:00+01:00", now=NOW)


@pytest.mark.parametrize("since, until, message", [
    ("2026-11-03T14:00", "2026-11-03T15:00Z", "no timezone"),
    ("2026-11-03T14:00Z", "2026-11-03T15:00", "no timezone"),
    ("2026-11-03T15:00Z", "2026-11-03T14:00Z", "before"),
    ("2026-11-03T14:00Z", "2026-11-03T14:00Z", "before"),
    ("2026-11-03T14:00Z", "2026-11-03T15:01Z", "future"),
    ("2026-11-02T14:59Z", "2026-11-03T15:00Z", "longer than"),
    ("yesterday", "2026-11-03T15:00Z", "ISO 8601"),
])
def test_a_bad_window_is_refused_saying_why(since, until, message):
    with pytest.raises(ValueError, match=message):
        undo.parse_window(since, until, now=NOW)


def test_exactly_the_maximum_window_is_allowed():
    undo.parse_window("2026-11-02T15:00Z", "2026-11-03T15:00Z", now=NOW)


def test_since_is_inclusive_and_until_exclusive():
    assert WINDOW.contains(WINDOW.since)
    assert not WINDOW.contains(WINDOW.until)


# --- select -------------------------------------------------------------------

def test_only_applied_rows_in_the_window_are_selected():
    rows = [
        archived_receipt(message_id="kept"),
        archived_receipt(message_id="dry", dry_run=True, after=None),
        row(message_id="failed", error="ollama down"),
        row(message_id="nothing-changed"),
        archived_receipt(message_id="too-early", at="2026-11-03T12:59Z"),
        archived_receipt(message_id="at-until", at="2026-11-03T15:00Z"),
    ]
    assert set(undo.select(rows, WINDOW)) == {"kept"}


def test_rows_for_one_message_are_grouped_oldest_first():
    late = archived_receipt(at="2026-11-03T14:30Z")
    early = archived_receipt(at="2026-11-03T13:30Z")
    assert undo.select([late, early], WINDOW)["m1"] == [early, late]


@pytest.mark.parametrize("action", [
    {"add": (PROCESSED,), "remove": (NEEDS_REVIEW,)},  # removes an Agent/ label
    {"add": (INBOX,)},                                 # adds INBOX
    {"add": ("STARRED",)},                             # touches a non-agent label
])
def test_a_row_undo_cannot_invert_stops_selection(action):
    """Phase 4's reconciliation will produce rows like the first. Undo must
    refuse loudly rather than guess at their inverse."""
    with pytest.raises(undo.UnexpectedRow):
        undo.select([row(**action)], WINDOW)


def test_an_odd_row_outside_the_window_does_not_matter():
    odd = row(add=(INBOX,), at="2026-11-03T10:00Z")
    assert undo.select([odd, archived_receipt()], WINDOW).keys() == {"m1"}


# --- plan_message: the rehearsal's five cases, as pure inputs -----------------

def plan(rows, current):
    return undo.plan_message(rows, current, IDS)


def test_m1_archived_receipt_comes_back_to_the_inbox():
    result = plan([archived_receipt()], {"UNREAD", "STARRED", *ids(PROCESSED, RECEIPTS)})
    assert set(result.remove_ids) == ids(PROCESSED, RECEIPTS)
    assert result.add_ids == (INBOX,)
    assert result.stray == ()


def test_m2_a_refile_is_stripped_and_reported_as_stray():
    """Reader removed Needs Review and added To Action before undo."""
    needs_review = row(add=(NEEDS_REVIEW, PROCESSED))
    result = plan([needs_review], {INBOX, *ids(PROCESSED, TO_ACTION)})
    assert set(result.remove_ids) == ids(PROCESSED, TO_ACTION)
    assert result.stray == (IDS[TO_ACTION],)
    assert result.add_ids == ()


def test_m3_error_alone_is_removed():
    result = plan([row(add=(ERROR,))], {INBOX, IDS[ERROR]})
    assert result.remove_ids == (IDS[ERROR],)
    assert result.add_ids == ()


def test_m4_a_kept_message_has_inbox_left_alone():
    result = plan([row(add=(PROCESSED, TO_ACTION))], {INBOX, *ids(PROCESSED, TO_ACTION)})
    assert set(result.remove_ids) == ids(PROCESSED, TO_ACTION)
    assert result.add_ids == ()


def test_m5_an_already_archived_message_is_not_put_back_in_the_inbox():
    """A3's case. The row has no INBOX remove, because the message was
    archived before the agent touched it, so undo has nothing to add back."""
    archived = row(add=(PROCESSED, RECEIPTS), prior=("UNREAD",))
    result = plan([archived], {"UNREAD", *ids(PROCESSED, RECEIPTS)})
    assert result.add_ids == ()
    assert set(result.remove_ids) == ids(PROCESSED, RECEIPTS)


def test_an_already_reverted_message_needs_no_call():
    result = plan([archived_receipt()], {INBOX, "UNREAD"})
    assert result.already_reverted


def test_a_partial_manual_undo_leaves_only_the_rest():
    """The reader already moved it back and removed Receipts."""
    result = plan([archived_receipt()], {INBOX, IDS[PROCESSED]})
    assert result.remove_ids == (IDS[PROCESSED],)
    assert result.add_ids == ()


def test_two_rows_for_one_message_are_unioned_into_one_change():
    """Labelled, undone, labelled again inside one window."""
    first = archived_receipt(at="2026-11-03T13:30Z")
    second = row(at="2026-11-03T14:30Z", add=(PROCESSED, label_for(Category.UPDATES)),
                 remove=(INBOX,), prior=("UNREAD",))
    current = {"UNREAD", *ids(PROCESSED, label_for(Category.UPDATES))}
    result = plan([first, second], current)
    assert set(result.remove_ids) == ids(PROCESSED, label_for(Category.UPDATES))
    assert result.add_ids == (INBOX,)   # from the FIRST row's remove
    assert result.stray == ()


def test_an_agent_label_the_message_had_before_is_not_stray():
    """Only labels that appeared since the agent first touched it are strays."""
    had_review = row(add=(PROCESSED,), prior=(INBOX, IDS[NEEDS_REVIEW]))
    result = plan([had_review], {INBOX, *ids(PROCESSED, NEEDS_REVIEW)})
    assert result.remove_ids == (IDS[PROCESSED],)
    assert result.stray == ()


def test_a_row_adding_a_label_already_in_prior_labels_holds_the_message():
    """The cross-check that keeps a second source of truth. A pre-existing
    Error was removed by someone, then a later run re-added it: the log
    contradicts itself, so undo leaves the message for a person."""
    first = row(at="2026-11-03T13:30Z", add=(PROCESSED,), prior=(INBOX, IDS[ERROR]))
    second = row(at="2026-11-03T14:30Z", add=(ERROR,), prior=(INBOX, IDS[PROCESSED]))
    result = plan([first, second], {INBOX, *ids(PROCESSED, ERROR)})
    assert result.held and ERROR in result.held
    assert result.add_ids == () and result.remove_ids == ()
    assert not result.already_reverted


def test_a_wrongly_recorded_prior_label_holds_the_message():
    """A bug that put the agent's own label into prior_labels would otherwise
    fool both undo and verify, since both trust prior_labels."""
    bad = archived_receipt(prior=(INBOX, IDS[RECEIPTS]))
    assert plan([bad], {"UNREAD", *ids(PROCESSED, RECEIPTS)}).held


def test_labels_undo_does_not_own_are_never_in_the_plan():
    result = plan([archived_receipt()], {"STARRED", "UNREAD", "Label_99", *ids(PROCESSED, RECEIPTS)})
    assert set(result.remove_ids) | set(result.add_ids) <= set(IDS.values())


# --- orphans --------------------------------------------------------------------

def test_a_swept_message_with_no_row_is_an_orphan():
    assert undo.orphans({"m1", "m6"}, [archived_receipt()]) == {"m6"}


def test_a_dry_run_row_does_not_explain_a_label():
    dry = archived_receipt(message_id="m6", dry_run=True, after=None)
    assert undo.orphans({"m6"}, [dry]) == {"m6"}


def test_an_applied_row_outside_the_window_still_explains_a_label():
    """Out of scope, not orphaned: an earlier run labelled it."""
    earlier = archived_receipt(at="2026-10-01T09:00Z")
    assert undo.orphans({"m1"}, [earlier]) == set()


def test_the_sweep_query_covers_both_stop_labels():
    assert undo.SWEEP_QUERY == '{label:"Agent/Processed" label:"Agent/Error"}'


# --- verify -----------------------------------------------------------------------

def check(rows, before, after):
    return undo.verify(rows, before, after, IDS)


def test_a_clean_undo_verifies():
    before = {"UNREAD", *ids(PROCESSED, RECEIPTS)}
    assert check([archived_receipt()], before, {INBOX, "UNREAD"}) == []


def test_an_agent_label_left_behind_is_a_mismatch():
    before = {"UNREAD", *ids(PROCESSED, RECEIPTS)}
    problems = check([archived_receipt()], before, {INBOX, "UNREAD", IDS[PROCESSED]})
    assert problems and PROCESSED in problems[0]


def test_a_message_left_archived_is_a_mismatch():
    before = {"UNREAD", *ids(PROCESSED, RECEIPTS)}
    assert check([archived_receipt()], before, {"UNREAD"})


def test_the_readers_star_survives_and_is_not_a_mismatch():
    before = {"UNREAD", "STARRED", *ids(PROCESSED, RECEIPTS)}
    assert check([archived_receipt()], before, {INBOX, "UNREAD", "STARRED"}) == []


def test_a_label_undo_does_not_own_changing_is_a_mismatch():
    """Should be impossible - undo never sends these ids - so it is checked."""
    before = {"UNREAD", "STARRED", *ids(PROCESSED, RECEIPTS)}
    problems = check([archived_receipt()], before, {INBOX, "UNREAD"})
    assert problems and "STARRED" in problems[0]


def test_a_reader_archiving_a_kept_message_is_respected():
    """The agent kept it in the inbox; the reader archived it since. Undo
    leaves INBOX alone, and verify accepts the reader's choice."""
    kept = row(add=(PROCESSED, TO_ACTION),
               after=(INBOX, "UNREAD", *ids(PROCESSED, TO_ACTION)))
    before = {"UNREAD", *ids(PROCESSED, TO_ACTION)}
    assert check([kept], before, {"UNREAD"}) == []


def test_without_labels_after_inbox_falls_back_to_prior():
    """Same reader archive, but the row has no labels_after - so the reader's
    move cannot be told apart from undo's failure, and it is flagged."""
    kept = row(add=(PROCESSED, TO_ACTION), after=None)
    before = {"UNREAD", *ids(PROCESSED, TO_ACTION)}
    problems = check([kept], before, {"UNREAD"})
    assert problems and "inbox" in problems[0]


def test_m5_verifies_staying_archived():
    archived = row(add=(PROCESSED, RECEIPTS), prior=("UNREAD",),
                   after=("UNREAD", *ids(PROCESSED, RECEIPTS)))
    before = {"UNREAD", *ids(PROCESSED, RECEIPTS)}
    assert check([archived], before, {"UNREAD"}) == []
    assert check([archived], before, {"UNREAD", INBOX})   # wrongly un-archived
