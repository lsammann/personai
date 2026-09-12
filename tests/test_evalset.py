"""Sampling design tests. No Gmail, no network.

A sampling bug does not raise - it quietly produces a set whose accuracy
number describes a population that does not exist - so the properties that
matter are asserted rather than eyeballed: the partition is exhaustive and
disjoint, the draw is reproducible, and `R` never reaches outside the frame.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app import evalset
from app.evalset import FrameRow, FrameSpec, Stratum

# A fixed window. Every test pins one rather than letting the frame default to
# "ending today", so the suite does not change behaviour overnight - the same
# property the production path now has.
SPEC = FrameSpec(dt.date(2025, 9, 10), dt.date(2026, 9, 10))


def frame(n: int, *, start: int = 0, threads: dict[int, str] | None = None):
    """`n` frame rows with predictable ids. Thread ids default to one each."""
    threads = threads or {}
    return [
        FrameRow(f"m{i:04d}", threads.get(i, f"t{i:04d}"))
        for i in range(start, start + n)
    ]


def ids(rows):
    return [row.message_id for row in rows]


# --- the partition -------------------------------------------------------


def test_every_frame_id_lands_in_exactly_one_stratum():
    rows = frame(20)
    membership = {"S_action": {"m0001", "m0002"}, "S_txn": {"m0003"}}
    assignment = evalset.assign_strata(ids(rows), membership)

    assert set(assignment) == set(ids(rows))
    assert assignment["m0001"] == "S_action"
    assert assignment["m0003"] == "S_txn"
    assert assignment["m0009"] == evalset.RESIDUAL


def test_first_match_wins_so_overlapping_queries_cannot_double_count():
    """"Your invoice receipt" matches S_action and S_txn. It is one message."""
    membership = {"S_action": {"m0000"}, "S_txn": {"m0000"}}
    assignment = evalset.assign_strata(["m0000"], membership)
    assert assignment["m0000"] == "S_action"


def test_stratum_counts_sum_to_the_frame_size():
    rows = frame(50)
    membership = {"S_action": set(ids(rows)[:10]), "S_human": set(ids(rows)[10:15])}
    counts = evalset.stratum_counts(evalset.assign_strata(ids(rows), membership))
    assert sum(counts.values()) == 50
    assert counts["S_action"] == 10
    assert counts["S_human"] == 5
    assert counts[evalset.RESIDUAL] == 35


# --- drawing -------------------------------------------------------------


def test_r_is_drawn_from_the_whole_frame_not_the_residual():
    """R is the representativeness anchor - it must be able to hit any cell."""
    rows = frame(400)
    membership = {"S_action": set(ids(rows)[:200])}
    # target 0: the cell exists for assignment, but nothing is mined into it,
    # so every row below came from the uniform draw.
    plan = evalset.build_plan(
        rows, membership, spec=SPEC, r_target=100,
        strata=(Stratum("S_action", "q", 0),)
    )

    drawn = {row.stratum for row in plan.sampled if row.draw == evalset.DRAW_R}
    assert drawn == {"S_action", evalset.RESIDUAL}


def test_no_message_is_sampled_twice():
    rows = frame(500)
    membership = {"S_action": set(ids(rows)[:100]), "S_txn": set(ids(rows)[100:200])}
    plan = evalset.build_plan(rows, membership, spec=SPEC)

    sampled = [row.message_id for row in plan.sampled]
    assert len(sampled) == len(set(sampled))


def test_mined_top_ups_come_from_their_own_cell():
    rows = frame(500)
    membership = {"S_action": set(ids(rows)[:120])}
    plan = evalset.build_plan(
        rows, membership, spec=SPEC, strata=(Stratum("S_action", "q", 35),)
    )

    mined = [r for r in plan.sampled if r.draw == evalset.DRAW_MINED]
    assert len(mined) == 35
    assert all(row.stratum == "S_action" for row in mined)


def test_the_full_budget_is_two_hundred_when_the_frame_can_supply_it():
    rows = frame(3000)
    membership = {
        "S_action": set(ids(rows)[:300]),
        "S_human": set(ids(rows)[300:600]),
        "S_txn": set(ids(rows)[600:900]),
    }
    plan = evalset.build_plan(rows, membership, spec=SPEC)
    assert len(plan.sampled) == 200


# --- determinism ---------------------------------------------------------


def test_the_same_seed_produces_a_byte_identical_sample(tmp_path):
    rows = frame(500)
    membership = {"S_action": set(ids(rows)[:100])}

    first, second = (
        evalset.build_plan(rows, membership, spec=SPEC, seed=7) for _ in range(2)
    )
    path_a, path_b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    evalset.save_sample(first, path_a)
    evalset.save_sample(second, path_b)

    assert path_a.read_bytes() == path_b.read_bytes()


def test_a_different_seed_produces_a_different_sample():
    rows = frame(500)
    plan_a = evalset.build_plan(rows, {}, spec=SPEC, seed=7)
    plan_b = evalset.build_plan(rows, {}, spec=SPEC, seed=8)
    assert [r.message_id for r in plan_a.sampled] != [
        r.message_id for r in plan_b.sampled
    ]


def test_draws_do_not_depend_on_the_iteration_order_of_the_input():
    """The sort-before-every-draw rule, asserted rather than trusted.

    Python randomises string hashing per process, so a set's iteration order
    differs between runs and `random.sample` picks by position. Passing the
    same frame in two orders stands in for that here: if any pool reached
    `random.sample` unsorted, these two plans would diverge.
    """
    rows = frame(300)
    membership = {"S_action": set(ids(rows)[:80])}

    forward = evalset.build_plan(rows, membership, spec=SPEC, seed=7)
    backward = evalset.build_plan(
        list(reversed(rows)), membership, spec=SPEC, seed=7
    )

    assert [r.message_id for r in forward.sampled] == [
        r.message_id for r in backward.sampled
    ]


# --- widening ------------------------------------------------------------


def test_a_stratum_the_frame_cannot_fill_widens_and_the_rows_are_flagged():
    rows = frame(200)
    membership = {"S_action": set(ids(rows)[:5])}  # only 5, target is 35

    def extend(stratum, years):
        return [f"old{years}_{i:03d}" for i in range(100)]

    plan = evalset.build_plan(
        rows,
        membership,
        spec=SPEC,
        strata=(Stratum("S_action", "q", 35),),
        extend=extend,
    )

    mined = [r for r in plan.sampled if r.draw == evalset.DRAW_MINED]
    extension = [r for r in plan.sampled if r.draw == evalset.DRAW_EXTENSION]
    assert len(mined) == 5
    assert len(extension) == 30
    assert all(row.stratum == "S_action" for row in extension)
    assert plan.windows["S_action"] == 2


def test_widening_never_re_draws_something_already_in_the_frame():
    rows = frame(200)
    membership = {"S_action": set(ids(rows)[:5])}

    # The wider window legitimately returns the frame's own messages too -
    # a resolver could still hand back frame ids - they must not come back.
    def extend(stratum, years):
        return ids(rows) + [f"old_{i:03d}" for i in range(50)]

    plan = evalset.build_plan(
        rows, membership, spec=SPEC,
        strata=(Stratum("S_action", "q", 35),), extend=extend
    )
    extension = [r.message_id for r in plan.sampled if r.draw == evalset.DRAW_EXTENSION]
    assert all(message_id.startswith("old_") for message_id in extension)


def test_extension_rows_are_excluded_from_n_h():
    """`w_h = N_h/n_h` is defined over the frame. Outside rows are not in it."""
    rows = frame(200)
    membership = {"S_action": set(ids(rows)[:5])}
    plan = evalset.build_plan(
        rows,
        membership,
        spec=SPEC,
        strata=(Stratum("S_action", "q", 35),),
        extend=lambda stratum, years: [f"old_{i:03d}" for i in range(100)],
    )
    counts = evalset.sampled_counts(plan)
    assert counts["S_action"]["n_extension"] == 30
    assert counts["S_action"]["n_h"] <= 5 + evalset.R_TARGET


def test_a_shortfall_that_survives_widening_tops_up_r_from_inside_the_frame():
    """R may never reach outside the frame, or the mailbox estimate is void."""
    rows = frame(300)
    membership = {"S_action": set(ids(rows)[:2])}
    in_frame = set(ids(rows))

    plan = evalset.build_plan(
        rows,
        membership,
        spec=SPEC,
        r_target=100,
        strata=(Stratum("S_action", "q", 35),),
        extend=lambda stratum, years: [],  # widening finds nothing
    )

    assert len(plan.sampled) == 135
    r_rows = [r for r in plan.sampled if r.draw == evalset.DRAW_R]
    assert len(r_rows) == 133
    assert all(row.message_id in in_frame for row in r_rows)


def test_widening_stops_at_the_cap():
    rows = frame(200)
    membership = {"S_action": set(ids(rows)[:1])}
    asked: list[int] = []

    def extend(stratum, years):
        asked.append(years)
        return []

    evalset.build_plan(
        rows, membership, spec=SPEC,
        strata=(Stratum("S_action", "q", 35),), extend=extend
    )
    assert asked == [2, 3, 4]


# --- splits --------------------------------------------------------------


def test_the_split_is_stratified_within_every_cell():
    rows = frame(1000)
    membership = {
        "S_action": set(ids(rows)[:200]),
        "S_txn": set(ids(rows)[200:400]),
    }
    plan = evalset.build_plan(rows, membership, spec=SPEC)

    per_cell: dict[str, dict[str, int]] = {}
    for row in plan.sampled:
        cell = per_cell.setdefault(row.stratum, {"dev": 0, "holdout": 0})
        cell[row.split] += 1

    for cell, counts in per_cell.items():
        total = counts["dev"] + counts["holdout"]
        assert counts["dev"] == round(total * evalset.DEV_FRACTION), cell


def test_the_split_is_reproducible_from_the_seed():
    rows = frame(400)
    a = evalset.build_plan(rows, {}, spec=SPEC, seed=7)
    b = evalset.build_plan(rows, {}, spec=SPEC, seed=7)
    assert {r.message_id: r.split for r in a.sampled} == {
        r.message_id: r.split for r in b.sampled
    }


def test_roughly_seventy_thirty_overall():
    rows = frame(3000)
    membership = {
        "S_action": set(ids(rows)[:300]),
        "S_human": set(ids(rows)[300:600]),
        "S_txn": set(ids(rows)[600:900]),
    }
    plan = evalset.build_plan(rows, membership, spec=SPEC)
    dev = sum(1 for r in plan.sampled if r.split == "dev")
    # Per-cell rounding moves this a message or two off 140.
    assert 136 <= dev <= 144


# --- the committed files -------------------------------------------------


def test_sample_rows_carry_no_content(tmp_path):
    rows = frame(300)
    plan = evalset.build_plan(rows, {}, spec=SPEC)
    path = tmp_path / "sample.jsonl"
    evalset.save_sample(plan, path)

    for line in path.read_text().splitlines():
        assert set(json.loads(line)) <= evalset.SAMPLE_KEYS


def test_save_sample_refuses_a_row_with_an_unexpected_key(tmp_path, monkeypatch):
    """The allowlist is a guard, not a description of current behaviour."""
    plan = evalset.build_plan(frame(50), {}, spec=SPEC)
    monkeypatch.setattr(
        evalset,
        "sample_rows",
        lambda _plan: [{"message_id": "m1", "subject": "Your bill is ready"}],
    )
    with pytest.raises(ValueError, match="non-allowlisted"):
        evalset.save_sample(plan, tmp_path / "sample.jsonl")


def test_strata_document_records_what_the_weights_need(tmp_path):
    rows = frame(500)
    membership = {"S_action": set(ids(rows)[:120])}
    plan = evalset.build_plan(rows, membership, spec=SPEC)
    document = evalset.strata_document(plan)

    assert document["frame_size"] == 500
    assert document["seed"] == evalset.DEFAULT_SEED
    by_name = {s["name"]: s for s in document["strata"]}
    assert by_name["S_action"]["N_h"] == 120
    assert sum(s["N_h"] for s in document["strata"]) == 500
    # Weights are derived, never stored.
    assert all("w_h" not in s for s in document["strata"])


def test_strata_json_is_stable_across_runs(tmp_path):
    plan = evalset.build_plan(frame(300), {"S_txn": {"m0001"}}, spec=SPEC, seed=7)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    evalset.save_strata(plan, a)
    evalset.save_strata(plan, b)
    assert a.read_bytes() == b.read_bytes()


# --- the frame cache -----------------------------------------------------


def test_frame_round_trips(tmp_path):
    path = tmp_path / "frame.jsonl"
    rows = frame(5)
    evalset.save_frame(rows, SPEC, path)
    assert evalset.load_frame(SPEC, path) == rows


def test_a_truncated_trailing_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "frame.jsonl"
    evalset.save_frame(frame(3), SPEC, path)
    with path.open("a") as handle:
        handle.write('{"message_id": "m9999", "thread_i')
    assert len(evalset.load_frame(SPEC, path)) == 3


def test_a_missing_frame_is_empty_not_an_error(tmp_path):
    assert evalset.load_frame(SPEC, tmp_path / "nope.jsonl") == []


# --- S_human membership --------------------------------------------------


class FakeGmail:
    """Just enough of `gmail_client` for `resolve_membership`."""

    def __init__(self, by_query: dict[str, list[tuple[str, str]]]):
        self.by_query = by_query
        self.queries: list[str] = []

    def search_refs(self, svc, query, limit=None):
        self.queries.append(query)
        return self.by_query.get(query, [])

    def search_ids(self, svc, query, limit=None):
        return [message_id for message_id, _ in self.search_refs(svc, query, limit)]


def test_s_human_pulls_in_the_received_message_of_a_thread_i_replied_to(monkeypatch):
    rows = [FrameRow("m1", "tA"), FrameRow("m2", "tB"), FrameRow("m3", "tC")]
    fake = FakeGmail(
        {
            f"{SPEC.query} is:starred": [("m3", "tC")],
            # A sent message in thread tA - its received sibling is m1.
            "from:me": [("sent1", "tA")],
        }
    )
    monkeypatch.setattr("app.gmail_client.search_refs", fake.search_refs)
    monkeypatch.setattr("app.gmail_client.search_ids", fake.search_ids)

    membership = evalset.resolve_membership(
        None, rows, strata=(Stratum("S_human", "", 35),), spec=SPEC
    )
    assert membership["S_human"] == {"m1", "m3"}


def test_a_sent_thread_with_nothing_in_the_frame_contributes_nothing(monkeypatch):
    rows = [FrameRow("m1", "tA")]
    fake = FakeGmail(
        {
            f"{SPEC.query} is:starred": [],
            "from:me": [("sent1", "tZZZ")],
        }
    )
    monkeypatch.setattr("app.gmail_client.search_refs", fake.search_refs)
    monkeypatch.setattr("app.gmail_client.search_ids", fake.search_ids)

    membership = evalset.resolve_membership(
        None, rows, strata=(Stratum("S_human", "", 35),), spec=SPEC
    )
    assert membership["S_human"] == set()


def test_membership_is_clipped_to_the_frame(monkeypatch):
    """A stratum query can return mail the frame excludes. N_h must not."""
    rows = [FrameRow("m1", "tA")]
    fake = FakeGmail(
        {f"{SPEC.query} subject:(invoice)": [("m1", "tA"), ("gone", "tB")]}
    )
    monkeypatch.setattr("app.gmail_client.search_refs", fake.search_refs)
    monkeypatch.setattr("app.gmail_client.search_ids", fake.search_ids)

    membership = evalset.resolve_membership(
        None, rows, strata=(Stratum("S_action", "subject:(invoice)", 35),), spec=SPEC
    )
    assert membership["S_action"] == {"m1"}


# --- widening query construction -----------------------------------------


def test_the_extender_widens_the_window_and_keeps_the_stratum_query(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        "app.gmail_client.search_ids",
        lambda svc, query, limit=None: seen.append(query) or [],
    )
    extend = evalset.make_extender(None, SPEC)
    extend(Stratum("S_action", "subject:(invoice)", 35), 3)

    # Three years back from the frame's start, ending where the frame begins:
    # disjoint from the frame by construction, not by filtering afterwards.
    assert seen == [
        (
            "after:2022/09/10 before:2025/09/10 "
            "-in:sent -in:drafts -in:chats subject:(invoice)"
        )
    ]


def test_s_human_is_never_widened(monkeypatch):
    """Its membership comes from thread matching, which a wider window
    does not extend - the frame is still the frame."""
    monkeypatch.setattr(
        "app.gmail_client.search_ids",
        lambda *a, **k: pytest.fail("S_human must not be widened"),
    )
    extend = evalset.make_extender(None, SPEC)
    assert extend(Stratum("S_human", "is:starred OR from:me (by thread)", 35), 2) == []


# --- the pinned window ---------------------------------------------------


def test_the_frame_query_is_absolute_not_relative():
    """`newer_than:1y` moves every day; the sample must not.

    This is the property the whole eval set's reproducibility rests on. A
    relative window means `--seed 7` draws from a different population
    tomorrow, and the only thing that hid it was the gitignored frame cache.
    """
    assert SPEC.query == (
        "after:2025/09/10 before:2026/09/10 -in:sent -in:drafts -in:chats"
    )
    assert "newer_than" not in SPEC.query


def test_the_same_window_gives_the_same_sample_on_a_different_day():
    """Two runs a year apart, same pinned window, same 200 messages."""
    rows = frame(500)
    membership = {"S_action": set(ids(rows)[:100])}
    spec_today = FrameSpec.ending(dt.date(2026, 9, 10))
    spec_next_year = FrameSpec.ending(dt.date(2026, 9, 10))

    a = evalset.build_plan(rows, membership, spec=spec_today, seed=7)
    b = evalset.build_plan(rows, membership, spec=spec_next_year, seed=7)
    assert [r.message_id for r in a.sampled] == [r.message_id for r in b.sampled]


def test_ending_defaults_to_a_one_year_window():
    spec = FrameSpec.ending(dt.date(2026, 9, 10))
    assert (spec.start, spec.end) == (dt.date(2025, 9, 10), dt.date(2026, 9, 10))


def test_a_leap_day_window_does_not_raise():
    spec = FrameSpec.ending(dt.date(2028, 2, 29))
    assert spec.start == dt.date(2027, 2, 28)


def test_the_window_is_recorded_in_the_manifest():
    """A reader must be able to see which population the numbers describe."""
    plan = evalset.build_plan(frame(300), {}, spec=SPEC)
    document = evalset.strata_document(plan)
    assert document["window_start"] == "2025-09-10"
    assert document["window_end"] == "2026-09-10"
    assert "newer_than" not in document["frame_query"]


def test_a_frame_cached_for_another_window_is_not_reused(tmp_path):
    """Changing the window must re-enumerate, not silently reuse the old set."""
    path = tmp_path / "frame.jsonl"
    evalset.save_frame(frame(5), SPEC, path)

    other = FrameSpec(dt.date(2024, 1, 1), dt.date(2025, 1, 1))
    assert evalset.load_frame(other, path) == []
    assert len(evalset.load_frame(SPEC, path)) == 5


# --- reading the sample back ---------------------------------------------


def test_sample_round_trips_with_its_seed(tmp_path):
    """The seed travels with the rows: `evallabel.ordering` derives from it."""
    path = tmp_path / "sample.jsonl"
    plan = evalset.build_plan(frame(300), {}, seed=7, spec=SPEC)
    evalset.save_sample(plan, path)

    rows, seed = evalset.load_sample(path)
    assert seed == 7
    assert [r.message_id for r in rows] == [r.message_id for r in plan.sampled]
    assert rows[0].stratum and rows[0].draw and rows[0].split


def test_a_sample_mixing_two_seeds_is_refused(tmp_path):
    """Two draws in one file leaves the presentation order undefined."""
    path = tmp_path / "sample.jsonl"
    path.write_text(
        json.dumps({"message_id": "a", "stratum": "Residual", "draw": "R",
                    "split": "dev", "seed": 7}) + "\n"
        + json.dumps({"message_id": "b", "stratum": "Residual", "draw": "R",
                      "split": "dev", "seed": 9}) + "\n"
    )
    with pytest.raises(ValueError, match="disagree on the seed"):
        evalset.load_sample(path)
