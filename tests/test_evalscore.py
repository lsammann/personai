"""Scoring tests. No Ollama, no Gmail, no mocks anywhere.

The whole argument for the predict/score split is that scoring is free and
re-runnable, so every number here is checked against one computed by hand
rather than against a golden file. A golden file would lock in whatever the
code did on the day it was written, which is the opposite of the point.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app import evalscore
from app.categories import Category
from app.evallabel import LabelRecord
from app.evalscore import Prediction, score, stability
from app.evalset import Sampled

T, F = 0.8, 0.15


def dist(**kwargs) -> dict[str, float]:
    """A distribution over the six categories, named by keyword.

    Anything unnamed shares what is left, so every test distribution sums to 1
    without the test having to say so six times.
    """
    named = {key.replace("_", " ").title(): value for key, value in kwargs.items()}
    named = {
        ("To Action" if k == "To Action" else k): v for k, v in named.items()
    }
    rest = [c.value for c in Category if c.value not in named]
    spare = max(0.0, 1 - sum(named.values()))
    return {**named, **{c: spare / len(rest) for c in rest}}


def prediction(message_id, category, confidence, distribution=None, **kwargs):
    defaults = {
        "order_name": "default",
        "retained_mass": 0.9,
        "latency_ms": 1000,
        "body_source": "plain",
        "body_len": 1500,
        "error": None,
    }
    defaults.update(kwargs)
    return Prediction(
        message_id=message_id,
        category=category,
        confidence=confidence,
        distribution=distribution or dist(),
        **defaults,
    )


def truth(message_id, category, unsure=False):
    return LabelRecord(message_id, category, unsure, "2026-09-13T00:00:00+00:00")


def sampled(message_id, stratum="Residual", draw="R", split="dev"):
    return Sampled(message_id, stratum, draw, split)


STRATA = {"strata": [
    {"name": "Residual", "N_h": 6010, "n_h": 93, "n_extension": 0},
    {"name": "S_action", "N_h": 74, "n_h": 37, "n_extension": 0},
]}


# --- statistics, each against a known value -------------------------------


def test_wilson_matches_the_published_interval():
    low, high = evalscore.wilson(90, 100)
    assert round(low, 4) == 0.8256
    assert round(high, 4) == 0.9448


def test_wilson_stays_inside_zero_and_one_at_the_extremes():
    """The reason it is Wilson and not the normal approximation, which would
    happily report an upper bound above 1."""
    low, high = evalscore.wilson(100, 100)
    assert low == pytest.approx(0.9629, abs=1e-3)
    assert high == pytest.approx(1.0)      # clamped, modulo float arithmetic
    assert evalscore.wilson(0, 100)[0] == 0.0
    assert evalscore.wilson(0, 100)[1] < 0.05


def test_wilson_on_no_observations_is_not_a_division_error():
    assert evalscore.wilson(0, 0) == (0.0, 0.0)


def test_calibration_buckets_are_half_open_so_0_8_lands_in_the_0_8_bucket():
    """Pinned because an off-by-one here moves the confidence threshold that
    gets read off the table and written into DESIGN.md."""
    assert evalscore.bucket(0.8) == 8
    assert evalscore.bucket(0.799) == 7
    assert evalscore.bucket(1.0) == 9      # top bucket is closed


def test_tv_distance_is_zero_for_identical_and_one_for_disjoint():
    a = {Category.TO_ACTION: 1.0}
    b = {Category.PERSONAL: 1.0}
    assert evalscore.tv_distance(a, a) == 0.0
    assert evalscore.tv_distance(a, b) == 1.0


def test_tv_distance_is_half_the_l1_norm():
    a = {Category.TO_ACTION: 0.6, Category.RECEIPTS: 0.4}
    b = {Category.TO_ACTION: 0.4, Category.RECEIPTS: 0.6}
    assert evalscore.tv_distance(a, b) == pytest.approx(0.2)


def test_mcnemar_counts_only_the_discordant_pairs():
    """Two accuracy numbers cannot tell 4-fixed-0-broken from 12-fixed-8."""
    clean = evalscore.mcnemar([(False, True)] * 4 + [(True, True)] * 50)
    assert (clean["n_fixed"], clean["n_broken"]) == (4, 0)
    assert clean["p_value"] == pytest.approx(0.125)

    noisy = evalscore.mcnemar([(False, True)] * 12 + [(True, False)] * 8)
    assert (noisy["n_fixed"], noisy["n_broken"]) == (12, 8)
    assert noisy["p_value"] > 0.05      # indistinguishable from noise


def test_mcnemar_with_no_disagreement_is_not_significant():
    assert evalscore.mcnemar([(True, True)] * 10)["p_value"] == 1.0


# --- weighting ------------------------------------------------------------


def test_weights_come_from_labelled_counts_not_sampled_ones():
    """`strata.json` records what was SAMPLED. If a message ends up without a
    prediction the real n_h is smaller, and weighting by the sampled figure
    overstates that stratum in exact proportion to the gap."""
    scored = [
        evalscore.Scored(
            message_id=f"m{i}", predicted="Updates", truth="Updates",
            confidence=0.9, retained_mass=0.9, distribution={},
            stratum="S_action", draw="mined", split="dev", unsure=False,
            body_source="plain", latency_ms=1, source="model", error=None,
        )
        for i in range(2)
    ]
    # 37 were sampled; only 2 have predictions here.
    assert evalscore.weights(STRATA, scored)["S_action"] == 74 / 2


def test_weighted_accuracy_against_a_hand_computed_example():
    """Two strata, deliberately unequal weights: the eval set over-samples the
    rare one, so unweighted accuracy describes a population that does not
    exist."""
    def row(stratum, correct):
        return evalscore.Scored(
            message_id=stratum + str(correct), predicted="A" if correct else "B",
            truth="A", confidence=0.9, retained_mass=0.9, distribution={},
            stratum=stratum, draw="R", split="dev", unsure=False,
            body_source="plain", latency_ms=1, source="model", error=None,
        )
    scored = [row("Residual", True), row("S_action", False)]
    weight = {"Residual": 100.0, "S_action": 1.0}
    # 100 / 101, not 1/2.
    assert evalscore.weighted_accuracy(scored, weight) == pytest.approx(100 / 101)


# --- the join -------------------------------------------------------------


def test_join_filters_to_one_split():
    """The lock-box: holdout rows must not reach a dev score by accident."""
    predictions = [prediction("a", "Updates", 0.9), prediction("b", "Updates", 0.9)]
    labels = {"a": truth("a", Category.UPDATES), "b": truth("b", Category.UPDATES)}
    sample = [sampled("a", split="dev"), sampled("b", split="holdout")]

    assert [r.message_id for r in evalscore.join(predictions, labels, sample)] == ["a"]
    everything = evalscore.join(predictions, labels, sample, split=None)
    assert len(everything) == 2


def test_join_ignores_rows_for_another_letter_order():
    predictions = [
        prediction("a", "Updates", 0.9),
        prediction("a", "Receipts", 0.9, order_name="rotated"),
    ]
    labels = {"a": truth("a", Category.UPDATES)}
    joined = evalscore.join(predictions, labels, [sampled("a")])
    assert [r.predicted for r in joined] == ["Updates"]


def test_join_marks_prefilter_hits_from_the_sender():
    predictions = [prediction("a", "Updates", 0.9)]
    labels = {"a": truth("a", Category.UPDATES)}
    joined = evalscore.join(
        predictions, labels, [sampled("a")],
        allowlist={"news.example.com"},
        senders={"a": "Deals <offers@mail.news.example.com>"},
    )
    assert joined[0].source == "prefilter"


# --- score, end to end ----------------------------------------------------


def four_messages():
    """Hand-computed: 2 of 4 correct, 1 of 2 To Action caught by the argmax,
    but 2 of 2 retained once the asymmetric floor is applied."""
    predictions = [
        prediction("a", "To Action", 0.9, dist(to_action=0.9)),
        prediction("b", "Receipts", 0.85, dist(receipts=0.85)),
        prediction("c", "Updates", 0.6, dist(updates=0.6)),
        # Wrong label, but p(To Action) sits exactly on the floor.
        prediction("d", "Receipts", 0.82, dist(receipts=0.82, to_action=0.15)),
    ]
    labels = {
        "a": truth("a", Category.TO_ACTION),
        "b": truth("b", Category.RECEIPTS),
        "c": truth("c", Category.PROMOTIONS),
        "d": truth("d", Category.TO_ACTION),
    }
    sample = [sampled(m) for m in "abcd"]
    return predictions, labels, sample


def report(**kwargs):
    predictions, labels, sample = kwargs.pop("data", four_messages())
    return score(
        predictions, labels, sample, kwargs.pop("strata", STRATA),
        confidence_threshold=kwargs.pop("confidence_threshold", T),
        to_action_floor=kwargs.pop("to_action_floor", F),
        **kwargs,
    )


def test_accuracy_and_its_interval():
    result = report()
    assert result.n == 4
    assert result.accuracy == 0.5
    low, high = result.accuracy_interval
    assert low < 0.5 < high


def test_confusion_matrix_records_the_cell_not_just_the_diagonal():
    result = report()
    assert result.confusion[("Promotions", "Updates")] == 1
    assert result.confusion[("To Action", "Receipts")] == 1


def test_recall_and_retention_are_different_numbers():
    """The whole reason both exist. A bill filed as Receipts at
    p(To Action)=0.15 is not a missed bill - the floor keeps it visible - but
    recall scores it as one."""
    result = report()
    assert result.to_action_recall["all"] == (1, 2)
    assert result.to_action_retention["all"] == (2, 2)
    assert result.to_action_misses == []


def test_retention_drops_when_the_floor_is_raised_above_the_runner_up():
    result = report(to_action_floor=0.2)
    assert result.to_action_retention["all"] == (1, 2)
    assert [row.message_id for row in result.to_action_misses] == ["d"]


def test_to_action_numbers_are_broken_out_by_draw():
    """A recall figure over messages selected for saying "overdue" is not a
    recall figure. The honest one is draw == "R"."""
    predictions, labels, sample = four_messages()
    sample = [sampled("a", draw="R"), sampled("b", draw="R"),
              sampled("c", draw="R"), sampled("d", "S_action", "mined")]
    result = report(data=(predictions, labels, sample))
    assert result.to_action_recall["R"] == (1, 1)
    assert result.to_action_recall["mined"] == (0, 1)


def test_the_action_matrix_collapses_the_free_errors():
    """Receipts vs Bookings costs nothing; crossing keeps/archives is the only
    confusion with a consequence."""
    result = report()
    # c: Promotions predicted Updates - both archive, so the action was right.
    assert result.action_matrix[("archives", "archives")] == 1
    assert result.action_matrix[("keeps", "keeps")] == 2


def test_a_failed_call_is_excluded_from_accuracy_not_counted_as_wrong():
    """A failure applies no labels and the message retries. Scoring it as a
    wrong answer would blame the model for an unreachable Ollama."""
    predictions, labels, sample = four_messages()
    predictions[2] = prediction("c", None, None, error="OllamaError")
    result = report(data=(predictions, labels, sample))
    assert result.failures == 1
    assert result.accuracy == pytest.approx(2 / 3)


def test_unsure_rows_are_reported_both_ways():
    """Headline includes them so nothing is hidden; the second figure says
    whether the model's errors cluster where the human was unsure."""
    predictions, labels, sample = four_messages()
    labels["c"] = truth("c", Category.PROMOTIONS, unsure=True)
    result = report(data=(predictions, labels, sample))
    assert result.n_unsure == 1
    assert result.accuracy == 0.5
    assert result.accuracy_excluding_unsure == pytest.approx(2 / 3)


def test_calibration_gap_is_positive_when_confident_answers_are_right():
    result = report()
    assert result.calibration_gap > 0


def test_threshold_sweep_moves_needs_review_in_the_expected_direction():
    result = report()
    rates = [row["needs_review_rate"] for row in result.threshold_sweep]
    assert rates == sorted(rates)
    assert rates[0] < rates[-1]


def test_floor_sweep_skips_values_that_make_the_rule_unreachable():
    """T + F < 1 or the asymmetric rule can never fire - the coupling
    `config._asymmetric_rule_must_be_reachable` refuses to start on."""
    result = report(confidence_threshold=0.8)
    assert all(0.8 + row["floor"] < 1.0 for row in result.floor_sweep)
    assert 0.25 not in [row["floor"] for row in result.floor_sweep]


def test_body_source_accuracy_carries_its_n():
    """plain_markup has 3 members in the real set. Without the n beside it,
    an accuracy of 0.67 reads as a measurement."""
    predictions, labels, sample = four_messages()
    predictions[0] = prediction(
        "a", "To Action", 0.9, dist(to_action=0.9), body_source="plain_markup"
    )
    result = report(data=(predictions, labels, sample))
    assert result.by_source["plain_markup"] == (1, 1)
    assert result.by_source["plain"] == (1, 3)


def test_latency_is_reported_over_answered_rows():
    assert report().latency["p50"] == 1000


def test_retained_mass_percentiles_and_deciles_are_present():
    result = report()
    assert result.retained_mass["p10"] == pytest.approx(0.9)
    assert result.accuracy_by_retained_decile


# --- the prefilter bucket -------------------------------------------------


def test_prefilter_is_scored_on_the_action_not_the_six_way_label():
    """The rule can only emit Promotions. A prefiltered Updates message is an
    error 6-way and entirely correct in practice - both archive."""
    predictions = [prediction("a", "Updates", 0.9, dist(updates=0.9))]
    labels = {"a": truth("a", Category.UPDATES)}
    result = score(
        predictions, labels, [sampled("a")], STRATA,
        confidence_threshold=T, to_action_floor=F,
        allowlist={"example.com"}, senders={"a": "x@example.com"},
    )
    assert result.prefilter["n"] == 1
    assert result.prefilter["rule_action_correct"] == 1
    assert result.prefilter["label_exact"] == 0


def test_prefilter_rows_stay_out_of_the_calibration_table():
    """A rule has no confidence. Putting one in the table corrupts it."""
    predictions = [prediction("a", "Updates", 0.9, dist(updates=0.9))]
    labels = {"a": truth("a", Category.UPDATES)}
    result = score(
        predictions, labels, [sampled("a")], STRATA,
        confidence_threshold=T, to_action_floor=F,
        allowlist={"example.com"}, senders={"a": "x@example.com"},
    )
    assert result.calibration == []


# --- permutation stability ------------------------------------------------


def test_stability_counts_unchanged_argmax_and_mean_tv():
    predictions = [
        prediction("a", "To Action", 0.9, dist(to_action=0.9)),
        prediction("a", "To Action", 0.9, dist(to_action=0.9), order_name="rotated"),
        prediction("b", "Receipts", 0.7, dist(receipts=0.7)),
        prediction("b", "Updates", 0.7, dist(updates=0.7), order_name="rotated"),
    ]
    result = stability(predictions)
    assert result["n"] == 2
    assert result["unchanged"] == 0.5
    assert result["mean_tv"] > 0


def test_stability_is_empty_when_only_one_order_was_run():
    assert stability([prediction("a", "Updates", 0.9)])["n"] == 0


# --- the purity claim, made executable ------------------------------------


def test_evalscore_imports_nothing_impure():
    """The predict/score split only pays off if scoring cannot reach a socket.
    Claims like that erode quietly; this does not."""
    allowed = {
        "__future__", "math", "collections.abc", "dataclasses",
        "app.categories", "app.decision", "app.evallabel", "app.evalset",
        "app.prefilter",
    }
    source = Path(evalscore.__file__).read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert imported <= allowed, f"impure imports: {sorted(imported - allowed)}"


# --- the reply rule bucket ------------------------------------------------


REPLY_STRATA = {"strata": [{"name": "Residual", "N_h": 100}]}


def reply_setup(truth_category, predicted, confidence=0.99, subject="Re: hi"):
    """An explicit distribution: the default spreads leftover mass evenly, which
    puts p(To Action) at 0.167 and trips the asymmetric floor, so every row
    would keep INBOX for the wrong reason."""
    spread = dist(**{predicted.lower().replace(" ", "_"): confidence})
    spread = {k: (confidence if k == predicted else
                  (1 - confidence) / 5) for k in spread}
    preds = [prediction("m1", predicted, confidence, distribution=spread)]
    labels = {"m1": truth("m1", truth_category)}
    plan = [sampled("m1")]
    return preds, labels, plan, {"m1": subject}


def test_a_reply_is_tagged_as_a_rule_hit_not_a_model_answer():
    preds, labels, plan, subjects = reply_setup(Category.PERSONAL, "Promotions")
    rows = evalscore.join(preds, labels, plan, subjects=subjects)
    assert rows[0].source == evalscore.REPLY


def test_a_non_reply_subject_is_left_to_the_model():
    preds, labels, plan, _ = reply_setup(Category.PERSONAL, "Promotions")
    rows = evalscore.join(preds, labels, plan, subjects={"m1": "Reminder: pay"})
    assert rows[0].source == "model"


def test_the_reply_rule_beats_the_sender_allowlist():
    """The live order, and the reader's call: a "Re:" from an allowlisted
    promotional domain is a reply to something they sent, so they want to see
    it. Checking the allowlist first would archive exactly those."""
    preds, labels, plan, subjects = reply_setup(Category.PERSONAL, "Promotions")
    rows = evalscore.join(
        preds, labels, plan, subjects=subjects,
        allowlist={"uniqlo.co.uk"}, senders={"m1": "offers@uniqlo.co.uk"},
    )
    assert rows[0].source == evalscore.REPLY


def test_a_rule_routed_message_keeps_inbox_whatever_the_model_said():
    """`decide_reply_hit()` removes nothing, so scoring the model's would-be
    decision here would measure a call the live system never makes."""
    preds, labels, plan, subjects = reply_setup(
        Category.PERSONAL, "Promotions", confidence=0.99
    )
    rows = evalscore.join(preds, labels, plan, subjects=subjects)
    assert evalscore.keeps_inbox(rows[0], 0.8, 0.15) is True

    model_rows = evalscore.join(preds, labels, plan, subjects={"m1": "Sale now"})
    assert evalscore.keeps_inbox(model_rows[0], 0.8, 0.15) is False


def test_rule_hits_stay_out_of_the_calibration_table():
    """They have no confidence in the live system. The eval has one only
    because `predict` classifies every message regardless of which rules
    would have intercepted it."""
    low = {c.value: 0.002 for c in Category}
    preds = [prediction("m1", "Promotions", 0.99, {**low, "Promotions": 0.99}),
             prediction("m2", "Receipts", 0.95, {**low, "Receipts": 0.95})]
    labels = {"m1": truth("m1", Category.PERSONAL),
              "m2": truth("m2", Category.RECEIPTS)}
    plan = [sampled("m1"), sampled("m2")]
    report = evalscore.score(
        preds, labels, plan, REPLY_STRATA, confidence_threshold=0.8,
        to_action_floor=0.15, subjects={"m1": "Re: hi"},
    )
    assert sum(bucket["n"] for bucket in report.calibration) == 1


def test_the_reply_bucket_scores_the_action_not_the_label():
    """A reply whose truth is `To Action` is a 6-way error and operationally
    correct - both keep the inbox."""
    preds, labels, plan, subjects = reply_setup(Category.TO_ACTION, "To Action")
    report = evalscore.score(
        preds, labels, plan, REPLY_STRATA, confidence_threshold=0.8,
        to_action_floor=0.15, subjects=subjects,
    )
    assert report.reply["n"] == 1
    assert report.reply["rule_action_correct"] == 1
    assert report.reply["label_exact"] == 0


def test_the_reply_bucket_reports_what_the_model_would_have_done():
    """The comparison that justifies the rule: where it scores higher than the
    model would have, it is earning its place."""
    preds, labels, plan, subjects = reply_setup(Category.PERSONAL, "Promotions")
    report = evalscore.score(
        preds, labels, plan, REPLY_STRATA, confidence_threshold=0.8,
        to_action_floor=0.15, subjects=subjects,
    )
    assert report.reply["rule_action_correct"] == 1
    assert report.reply["model_action_correct"] == 0


def test_no_replies_means_no_bucket():
    preds, labels, plan, _ = reply_setup(Category.PERSONAL, "Personal")
    report = evalscore.score(
        preds, labels, plan, REPLY_STRATA, confidence_threshold=0.8,
        to_action_floor=0.15, subjects={"m1": "A subject"},
    )
    assert report.reply == {}
