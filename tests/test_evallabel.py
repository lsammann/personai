"""Labelling tests. No Gmail, no terminal.

The properties here are the ones that would otherwise only be exercised by two
hours of keystrokes: that a resumed session picks up where it stopped and in
the same order, that a correction wins, that a correction cannot leak across
passes, and that nothing on screen or on disk carries content or a stratum.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app import evallabel
from app.categories import Category
from app.evallabel import Cached, LabelRecord
from app.evalset import Sampled
from app.gmail_client import Message
from app.message_body import Selection

NOW = dt.datetime(2026, 9, 12, tzinfo=dt.UTC)


def sampled(n: int, stratum: str = "Residual"):
    return [Sampled(f"m{i:04d}", stratum, "R", "dev") for i in range(n)]


def record(message_id: str, label: Category, *, unsure=False, pass_no=1):
    return LabelRecord(message_id, label, unsure, "2026-09-12T10:00:00+00:00", pass_no)


def cached(message_id="m0001", plain="a plain body", html=""):
    return Cached(message_id, "sender@example.com", "A subject",
                  "2026-03-11T09:00:00+00:00", plain, html)


def message(message_id="m0001", plain="plain", html="<p>html</p>"):
    return Message(
        id=message_id, thread_id="t1", sender="sender@example.com",
        subject="A subject", internal_date=dt.datetime(2026, 3, 11, tzinfo=dt.UTC),
        text_plain=plain, text_html=html, label_ids=("INBOX",),
    )


# --- presentation order --------------------------------------------------


def test_ordering_is_deterministic_from_the_seed():
    sample = sampled(50)
    assert evallabel.ordering(sample, 7) == evallabel.ordering(sample, 7)


def test_ordering_does_not_depend_on_input_order():
    """The file could be read in any order; the presented sequence may not move.

    Sorting before the shuffle is what guarantees this - `shuffle` permutes by
    position, so an input ordered differently would otherwise produce a
    different session for the same seed.
    """
    sample = sampled(50)
    assert evallabel.ordering(sample, 7) == evallabel.ordering(sample[::-1], 7)


def test_ordering_covers_the_sample_exactly_once():
    sample = sampled(30)
    order = evallabel.ordering(sample, 7)
    assert sorted(order) == sorted(row.message_id for row in sample)


def test_ordering_mixes_the_strata():
    """All of one stratum in a row would let 37 judgements drift together."""
    sample = sampled(20, "S_action") + [
        Sampled(f"r{i:04d}", "Residual", "R", "dev") for i in range(20)
    ]
    order = evallabel.ordering(sample, 7)
    first_ten = [m for m in order[:10] if m.startswith("m")]
    assert 1 <= len(first_ten) <= 9


def test_ordering_uses_its_own_seed_offset():
    """Reusing the split's derived seed would couple two independent draws."""
    assert evallabel.ORDER_SEED_OFFSET == 2


# --- resume, corrections, passes -----------------------------------------


def test_pending_skips_what_is_already_labelled_and_keeps_order():
    order = ["a", "b", "c", "d"]
    resolved = evallabel.resolve([record("b", Category.UPDATES)])
    assert evallabel.pending(order, resolved) == ["a", "c", "d"]


def test_pending_excludes_this_sittings_skips():
    order = ["a", "b", "c"]
    assert evallabel.pending(order, {}, skipped={"b"}) == ["a", "c"]


def test_a_correction_supersedes_by_file_position_not_timestamp():
    """`b` exists to be pressed immediately, so two rows can share a second."""
    rows = [
        LabelRecord("a", Category.PROMOTIONS, False, "2026-09-12T10:00:00+00:00"),
        LabelRecord("a", Category.PERSONAL, False, "2026-09-12T10:00:00+00:00"),
    ]
    assert evallabel.resolve(rows)["a"].label is Category.PERSONAL


def test_corrections_do_not_cross_passes():
    """A pass 2 row is a second judgement, not a correction of pass 1.

    Collapsing them would destroy the self-consistency measurement step 7
    exists to make - the disagreement rate would read as zero by construction.
    """
    rows = [
        record("a", Category.RECEIPTS, pass_no=1),
        record("a", Category.BOOKINGS, pass_no=2),
    ]
    assert evallabel.resolve(rows, 1)["a"].label is Category.RECEIPTS
    assert evallabel.resolve(rows, 2)["a"].label is Category.BOOKINGS


def test_pass_one_rows_do_not_satisfy_a_pass_two_query():
    rows = [record("a", Category.RECEIPTS, pass_no=1)]
    assert evallabel.resolve(rows, 2) == {}


def test_unfinished_lists_what_a_pass_has_not_reached():
    rows = [record("a", Category.UPDATES)]
    assert evallabel.unfinished(rows, ["a", "b", "c"]) == ["b", "c"]


def test_a_complete_pass_one_is_what_unblocks_pass_two():
    ids = ["a", "b"]
    rows = [record("a", Category.UPDATES), record("b", Category.PERSONAL)]
    assert evallabel.unfinished(rows, ids) == []
    # ...and pass 2 itself is empty until the recheck writes rows.
    assert evallabel.unfinished(rows, ids, pass_no=2) == ids


def test_counts_by_stratum_reports_labelled_against_sampled():
    """`n_h` at score time is the labelled count, not the sampled one."""
    sample = sampled(2, "S_action") + sampled(0)
    sample += [Sampled("x0001", "Residual", "R", "dev")]
    resolved = evallabel.resolve([record("m0000", Category.TO_ACTION)])
    counts = evallabel.counts_by_stratum(sample, resolved)
    assert counts["S_action"] == {"sampled": 2, "labelled": 1}
    assert counts["Residual"] == {"sampled": 1, "labelled": 0}


# --- the committed file --------------------------------------------------


def test_written_rows_carry_only_allowlisted_keys(tmp_path):
    """The content-leak guard. An invariant gets a test, not a docstring."""
    path = tmp_path / "labeled.jsonl"
    evallabel.append_record(record("a", Category.TO_ACTION), path)
    row = json.loads(path.read_text().splitlines()[0])
    assert set(row) == evallabel.LABEL_KEYS
    for banned in ("sender", "subject", "body", "snippet", "text_plain"):
        assert banned not in row


def test_pass_is_written_as_pass_not_pass_no(tmp_path):
    path = tmp_path / "labeled.jsonl"
    evallabel.append_record(record("a", Category.TO_ACTION, pass_no=2), path)
    assert json.loads(path.read_text())["pass"] == 2


def test_an_unknown_category_cannot_be_written(tmp_path):
    bad = LabelRecord("a", "Newsletters", False, "2026-09-12T10:00:00+00:00")
    with pytest.raises(ValueError):
        evallabel.append_record(bad, tmp_path / "labeled.jsonl")


def test_records_round_trip(tmp_path):
    path = tmp_path / "labeled.jsonl"
    rows = [record("a", Category.TO_ACTION, unsure=True), record("b", Category.PERSONAL)]
    for row in rows:
        evallabel.append_record(row, path)
    loaded, skipped = evallabel.load_records(path)
    assert loaded == rows
    assert skipped == 0


def test_a_half_written_trailing_line_costs_one_label_not_the_session(tmp_path):
    """What a Ctrl-C mid-append leaves behind."""
    path = tmp_path / "labeled.jsonl"
    evallabel.append_record(record("a", Category.TO_ACTION), path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"message_id": "b", "lab')
    loaded, skipped = evallabel.load_records(path)
    assert [r.message_id for r in loaded] == ["a"]
    assert skipped == 1


def test_a_row_with_an_unknown_category_is_counted_not_silently_dropped(tmp_path):
    path = tmp_path / "labeled.jsonl"
    path.write_text(json.dumps({
        "message_id": "a", "label": "Newsletters", "unsure": False,
        "labelled_at": "2026-09-12T10:00:00+00:00", "pass": 1,
    }) + "\n")
    loaded, skipped = evallabel.load_records(path)
    assert loaded == [] and skipped == 1


def test_load_records_on_a_missing_file_is_an_empty_session(tmp_path):
    assert evallabel.load_records(tmp_path / "nope.jsonl") == ([], 0)


# --- the cache -----------------------------------------------------------


def test_cache_round_trip(tmp_path):
    evallabel.cache_put(message("m1", plain="hello", html="<p>hi</p>"), tmp_path)
    got = evallabel.cache_get("m1", tmp_path)
    assert got.text_plain == "hello"
    assert got.text_html == "<p>hi</p>"
    assert got.sender == "sender@example.com"


def test_the_cache_stores_raw_parts_so_an_extraction_bump_is_rescorable(tmp_path):
    """A1: `select_body` runs fresh at eval time, against this same cache."""
    evallabel.cache_put(message("m1", plain="", html="<p>only html</p>"), tmp_path)
    stored = json.loads((tmp_path / "m1.json").read_text())
    assert stored["text_html"] == "<p>only html</p>"
    assert "body" not in stored


def test_the_cache_holds_nothing_the_model_will_not_see_except_the_date(tmp_path):
    evallabel.cache_put(message("m1"), tmp_path)
    stored = json.loads((tmp_path / "m1.json").read_text())
    assert set(stored) == {
        "message_id", "sender", "subject", "internal_date",
        "text_plain", "text_html",
    }


def test_an_unreadable_cache_entry_reads_as_absent(tmp_path):
    """Treated as absent so the caller refetches. The cache is derived data."""
    (tmp_path / "m1.json").write_text('{"message_id": "m1", "text_pl')
    assert evallabel.cache_get("m1", tmp_path) is None


def test_no_temp_file_survives_a_write(tmp_path):
    evallabel.cache_put(message("m1"), tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["m1.json"]


def test_cache_missing_reports_in_the_order_asked(tmp_path):
    evallabel.cache_put(message("b"), tmp_path)
    assert evallabel.cache_missing(["a", "b", "c"], tmp_path) == ["a", "c"]


def test_a_cache_path_cannot_escape_the_directory(tmp_path):
    with pytest.raises(ValueError):
        evallabel.cache_path("../../etc/passwd", tmp_path)


# --- the screen ----------------------------------------------------------


def test_render_truncates_and_says_so():
    # "z" appears in neither the sender nor the subject, so counting it counts
    # body characters and nothing else.
    body = "z" * 4000
    out = evallabel.render(cached(plain=body), 1500, now=NOW)
    assert "1,500 of 4,000 chars" in out
    assert out.count("z") == 1500


def test_more_shows_the_rest():
    body = "z" * 4000
    out = evallabel.render(cached(plain=body), 1500, show_full=True, now=NOW)
    assert out.count("z") == 4000
    assert "for more" not in out


def test_render_shows_what_the_model_will_read_not_the_raw_part():
    """Same `select_body` the classifier calls - tags gone, stub rule applied."""
    out = evallabel.render(cached(plain="", html="<p>Amount due</p><p>87.42</p>"),
                           1500, now=NOW)
    assert "Amount due 87.42" in out
    assert "<p>" not in out


def test_more_shows_the_other_text_part_too():
    """The labeller must not be blind when the body policy picks the wrong part.

    A stub plain part that dodges the stub rule - too long because of tracking
    URLs, and a ratio deflated by zero-width padding in the HTML - leaves the
    real content only in the part `select_body` did not choose. Ground truth is
    what the email IS, so `m` has to reach it.
    """
    message = cached(
        plain="This email is only available in HTML. View it in your browser.",
        html="<p>Your invoice for 87.42 is due on Friday</p>",
    )
    partial = evallabel.render(message, 1500, now=NOW)
    assert "invoice" not in partial

    full = evallabel.render(message, 1500, show_full=True, now=NOW)
    assert "Your invoice for 87.42 is due on Friday" in full
    assert "also in the html part" in full


def test_more_shows_the_plain_part_when_html_was_chosen():
    message = cached(plain="", html="<p>the html body</p>")
    full = evallabel.render(message, 1500, show_full=True, now=NOW)
    assert "also in the" not in full  # nothing on the other side to show

    message = cached(plain="short stub", html="<p>" + "html body " * 50 + "</p>")
    full = evallabel.render(message, 1500, show_full=True, now=NOW)
    assert "also in the plain part" in full
    assert "short stub" in full


def test_render_never_shows_a_stratum():
    """`S_action` on screen is a direct hint at `To Action`."""
    out = evallabel.render(cached(), 1500, now=NOW, position=1, total=200)
    for stratum in ("S_action", "S_human", "S_txn", "Residual", "mined"):
        assert stratum not in out


def test_cached_cannot_carry_a_stratum():
    """Structural, not a matter of remembering to leave it out of `render`."""
    assert not hasattr(cached(), "stratum")


def test_render_shows_the_arrival_date_and_age():
    out = evallabel.render(cached(), 1500, now=NOW, position=47, total=200)
    assert "2026-03-11" in out
    assert "6 months ago" in out
    assert "[47/200]" in out


def test_render_flags_a_partial_html_parse(monkeypatch):
    """A short body from a failed parse must not read as a short email.

    Forced rather than provoked with bad markup: `HTMLParser` recovers from
    almost everything real mail contains, so `parsed_ok=False` is the rare case
    where `feed` actually raised. The point under test is that `render` shows
    it, not what triggers it.
    """
    monkeypatch.setattr(
        evallabel, "select_body",
        lambda plain, html: Selection(text="hi", source="html", parsed_ok=False),
    )
    out = evallabel.render(cached(plain="", html="<p>hi"), 1500, now=NOW)
    assert "html parse failed" in out


def test_render_wraps_long_lines_but_keeps_the_authors_own():
    """Plain text's line breaks carry structure; HTML-derived text has none."""
    out = evallabel.render(
        cached(plain="Amount due\n87.42\n\n" + "word " * 100), 1500, now=NOW
    )
    assert "Amount due\n87.42" in out
    assert max(len(line) for line in out.splitlines()) <= 78


def test_render_handles_an_empty_body():
    assert "(no body text)" in evallabel.render(cached(plain=""), 1500, now=NOW)


@pytest.mark.parametrize(
    "days, expected",
    [(0, "today"), (1, "yesterday"), (10, "10 days ago"),
     (185, "6 months ago"), (800, "2 years ago")],
)
def test_age(days, expected):
    assert evallabel.age(NOW - dt.timedelta(days=days), NOW) == expected


# --- the keys ------------------------------------------------------------


def test_digit_keys_cover_every_category_in_enum_order():
    """Derived from `Category`, so there is no second ordering to drift."""
    assert list(evallabel.KEYS.values()) == list(Category)
    assert evallabel.KEYS["1"] is Category.TO_ACTION


# --- the blind recheck ---------------------------------------------------


def test_the_draw_is_deterministic_from_the_seed():
    ids = [row.message_id for row in sampled(200)]
    assert evallabel.recheck_draw(ids, 30, 99) == evallabel.recheck_draw(ids, 30, 99)
    assert evallabel.recheck_draw(ids, 30, 99) != evallabel.recheck_draw(ids, 30, 7)


def test_the_draw_does_not_depend_on_input_order():
    """Sorted before the shuffle: string hashing is randomised per process."""
    ids = [row.message_id for row in sampled(200)]
    assert evallabel.recheck_draw(ids, 30, 99) == evallabel.recheck_draw(
        list(reversed(ids)), 30, 99
    )


def test_the_draw_is_a_subset_of_the_sample_with_no_repeats():
    ids = [row.message_id for row in sampled(200)]
    drawn = evallabel.recheck_draw(ids, 30, 99)
    assert len(drawn) == len(set(drawn)) == 30
    assert set(drawn) <= set(ids)


def test_a_larger_draw_extends_the_smaller_one():
    """The prefix property: extending an ambiguous ceiling keeps the rows already
    recorded, so it costs ten more messages rather than a fresh thirty."""
    ids = [row.message_id for row in sampled(200)]
    assert evallabel.recheck_draw(ids, 40, 99)[:30] == evallabel.recheck_draw(
        ids, 30, 99
    )


def test_a_draw_larger_than_the_sample_is_the_whole_sample():
    ids = [row.message_id for row in sampled(10)]
    assert sorted(evallabel.recheck_draw(ids, 30, 99)) == sorted(ids)


def test_agreement_counts_exact_matches():
    first = {"a": record("a", Category.RECEIPTS), "b": record("b", Category.UPDATES)}
    second = {"a": record("a", Category.RECEIPTS, pass_no=2),
              "b": record("b", Category.PROMOTIONS, pass_no=2)}
    result = evallabel.agreement(first, second)
    assert (result.n, result.n_agree) == (2, 1)
    assert [d.message_id for d in result.disagreements] == ["b"]
    assert (result.disagreements[0].first, result.disagreements[0].second) == (
        Category.UPDATES, Category.PROMOTIONS,
    )


def test_agreement_is_measured_only_over_the_rows_pass_two_reached():
    """A partial recheck is scored over what it visited, not over all 200."""
    first = {f"m{i}": record(f"m{i}", Category.UPDATES) for i in range(200)}
    second = {"m0": record("m0", Category.UPDATES, pass_no=2)}
    assert evallabel.agreement(first, second).n == 1


def test_a_free_error_agrees_on_the_action_and_a_costly_one_does_not():
    """`To Action` and `Personal` both keep INBOX; `Bookings` and `Receipts`
    sit either side of it, which is the disagreement with consequences."""
    first = {"a": record("a", Category.TO_ACTION), "b": record("b", Category.BOOKINGS)}
    second = {"a": record("a", Category.PERSONAL, pass_no=2),
              "b": record("b", Category.RECEIPTS, pass_no=2)}
    result = evallabel.agreement(first, second)
    assert (result.n_agree, result.n_action_agree) == (0, 1)
    crossings = {d.message_id: d.crosses_inbox for d in result.disagreements}
    assert crossings == {"a": False, "b": True}


def test_unsure_in_either_pass_is_attributed():
    """The flagged rows are the ones expected to flip; whether they did is the
    difference between a noisy ceiling and a known-noisy handful of rows."""
    first = {"a": record("a", Category.RECEIPTS, unsure=True),
             "b": record("b", Category.UPDATES),
             "c": record("c", Category.UPDATES)}
    second = {"a": record("a", Category.BOOKINGS, pass_no=2),
              "b": record("b", Category.UPDATES, unsure=True, pass_no=2),
              "c": record("c", Category.UPDATES, pass_no=2)}
    result = evallabel.agreement(first, second)
    assert (result.n_unsure, result.n_unsure_agree) == (2, 1)
    assert result.disagreements[0].unsure is True


def test_a_pass_two_row_with_no_pass_one_row_raises():
    """Damage, not a message to skip. Dropping it would shrink the denominator
    and flatter the ceiling by exactly the rows lost."""
    with pytest.raises(ValueError, match="ghost"):
        evallabel.agreement({}, {"ghost": record("ghost", Category.UPDATES, pass_no=2)})


def test_agreement_over_identical_passes_is_total():
    first = {"a": record("a", Category.PERSONAL)}
    second = {"a": record("a", Category.PERSONAL, pass_no=2)}
    result = evallabel.agreement(first, second)
    assert (result.n_agree, result.n_action_agree) == (1, 1)
    assert result.disagreements == ()


def test_the_draw_round_trips(tmp_path):
    path = tmp_path / "recheck.json"
    draw = evallabel.RecheckDraw(2, 99, "2026-09-14T10:00:00+00:00", ("a", "b"))
    evallabel.save_draw(draw, path)
    assert evallabel.load_draw(path) == draw


def test_the_draw_file_carries_ids_and_provenance_only(tmp_path):
    path = tmp_path / "recheck.json"
    evallabel.save_draw(
        evallabel.RecheckDraw(1, 99, "2026-09-14T10:00:00+00:00", ("a",)), path
    )
    assert set(json.loads(path.read_text())) == evallabel.RECHECK_KEYS


def test_a_draw_that_was_never_made_is_none(tmp_path):
    assert evallabel.load_draw(tmp_path / "nothing.json") is None


def test_an_unreadable_draw_raises_rather_than_reading_as_absent(tmp_path):
    """Unlike the cache: treating damage as "never drawn" would replace the
    measurement plan with a fresh one mid-recheck."""
    path = tmp_path / "recheck.json"
    path.write_text('{"seed": 99}')
    with pytest.raises(ValueError):
        evallabel.load_draw(path)


def test_the_recheck_seed_is_not_the_sample_seed():
    """One number reproduces the plan, and no draw reshuffles another."""
    assert evallabel.DEFAULT_RECHECK_SEED != 7
    assert evallabel.RECHECK_PASS == 2


def test_a_pass_one_correction_after_the_recheck_is_detected():
    """The ceiling is measured once, before corrections. Fixing a genuine
    pass-1 error afterwards makes pass 1 agree with pass 2 by construction, so
    re-running the report would print a higher number that means nothing."""
    first = {"a": LabelRecord("a", Category.PERSONAL, False, "2026-09-16T10:00:00+00:00"),
             "b": LabelRecord("b", Category.UPDATES, False, "2026-09-12T10:00:00+00:00")}
    second = {"a": LabelRecord("a", Category.PERSONAL, False, "2026-09-14T10:00:00+00:00", 2),
              "b": LabelRecord("b", Category.UPDATES, False, "2026-09-14T10:00:00+00:00", 2)}
    assert evallabel.corrected_after_recheck(first, second) == ["a"]


def test_an_untouched_pass_one_is_not_stale():
    first = {"a": LabelRecord("a", Category.PERSONAL, False, "2026-09-12T10:00:00+00:00")}
    second = {"a": LabelRecord("a", Category.BOOKINGS, False, "2026-09-14T10:00:00+00:00", 2)}
    assert evallabel.corrected_after_recheck(first, second) == []


def test_staleness_ignores_messages_the_recheck_never_visited():
    """Relabelling the other 170 says nothing about the draw's ceiling."""
    first = {"z": LabelRecord("z", Category.PERSONAL, False, "2026-09-20T10:00:00+00:00")}
    assert evallabel.corrected_after_recheck(first, {}) == []
