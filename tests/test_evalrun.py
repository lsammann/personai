"""Prediction and the results file. No Ollama - `classify` is injected.

What matters here is not what the model says; it is that a run can be
interrupted and resumed, that a failure is recorded rather than mistaken for a
wrong answer, and that the committed file carries no content.
"""

from __future__ import annotations

import json

import pytest

from app import evalrun
from app.categories import Category
from app.classifier import Interpretation, OllamaError
from app.evallabel import Cached
from app.evalscore import Prediction


def cached(message_id="m1", plain="a body", html=""):
    return Cached(message_id, "sender@example.com", "A subject",
                  "2026-03-11T09:00:00+00:00", plain, html)


def interpretation(category=Category.UPDATES, confidence=0.7):
    spare = (1 - confidence) / 5
    distribution = {c: (confidence if c is category else spare) for c in Category}
    return Interpretation(
        distribution=distribution, category=category,
        confidence=confidence, missing_letters=(), retained_mass=0.88,
    )


def stub(result=None, error=None):
    """A `classify` replacement that records how it was called."""
    calls = []

    def classify(sender, subject, plain, html, **kwargs):
        calls.append({"sender": sender, **kwargs})
        if error is not None:
            raise error
        return result or interpretation()

    classify.calls = calls
    return classify


# --- predict --------------------------------------------------------------


def test_one_row_per_message_and_order():
    rows = evalrun.predict(
        [cached("a"), cached("b")],
        model="llama3.1:8b", body_chars=1500,
        orders=evalrun.PERMUTED_ORDERS, classify=stub(),
    )
    assert len(rows) == 6
    assert {row.order_name for row in rows} == {"default", "rotated", "reversed"}


def test_the_prompt_and_order_reach_the_classifier():
    """The two knobs the sweep turns. If they are not passed through, ten runs
    measure the same thing."""
    classify = stub()
    evalrun.predict(
        [cached()], model="llama3.1:8b", body_chars=800,
        prompt_id="v1", orders=evalrun.PERMUTED_ORDERS, classify=classify,
    )
    assert {call["prompt_id"] for call in classify.calls} == {"v1"}
    assert {call["body_chars"] for call in classify.calls} == {800}
    assert classify.calls[1]["order"] == evalrun.ROTATED
    assert classify.calls[2]["order"] == evalrun.REVERSED


def test_a_failed_call_is_recorded_not_raised():
    """A failure applies no labels and retries; a wrong answer is a
    measurement. `decision.py` draws the same line."""
    rows = evalrun.predict(
        [cached()], model="m", body_chars=1500,
        classify=stub(error=OllamaError("connection refused")),
    )
    assert rows[0].category is None
    assert rows[0].confidence is None
    assert "OllamaError" in rows[0].error
    assert rows[0].failed


def test_body_source_and_length_describe_what_was_actually_sent():
    """So accuracy can be segmented by extraction cohort afterwards - a
    question that is unanswerable once the run is over."""
    rows = evalrun.predict(
        [cached(plain="", html="<p>" + "x" * 5000 + "</p>")],
        model="m", body_chars=800, classify=stub(),
    )
    assert rows[0].body_source == "html"
    assert rows[0].body_len == 800


def test_resume_skips_pairs_already_done():
    rows = evalrun.predict(
        [cached("a"), cached("b")], model="m", body_chars=1500,
        classify=stub(), done=frozenset({("a", "default")}),
    )
    assert [row.message_id for row in rows] == ["b"]


def test_rows_are_appended_as_they_go(tmp_path):
    """Twenty minutes of CPU must not be lost to a stray Ctrl-C."""
    path = tmp_path / "run.jsonl"
    evalrun.predict(
        [cached("a"), cached("b")], model="m", body_chars=1500,
        classify=stub(), path=path,
    )
    assert len(path.read_text().splitlines()) == 2


# --- the results file -----------------------------------------------------


def test_written_rows_carry_only_allowlisted_keys(tmp_path):
    """The content-leak guard, same as sample.jsonl and labeled.jsonl."""
    path = tmp_path / "run.jsonl"
    evalrun.append_prediction(
        Prediction("a", "default", "Updates", 0.7, {}, 0.9, 100, "plain", 1500),
        path,
    )
    row = json.loads(path.read_text().splitlines()[0])
    assert set(row) == evalrun.RESULT_KEYS
    for banned in ("sender", "subject", "body", "text_plain", "snippet"):
        assert banned not in row


def test_manifest_and_predictions_round_trip(tmp_path):
    path = tmp_path / "run.jsonl"
    manifest = evalrun.build_manifest(
        model="llama3.1:8b", body_chars=1500, run_id="test-run",
        sample_path=tmp_path / "absent.jsonl",
    )
    evalrun.start_result(manifest, path)
    evalrun.predict(
        [cached("a")], model="llama3.1:8b", body_chars=1500,
        classify=stub(), path=path,
    )

    loaded = evalrun.load_result(path)
    assert loaded.manifest.run_id == "test-run"
    assert loaded.manifest.prompt_id == "v1"
    assert loaded.manifest.extraction_version
    assert loaded.manifest.prompt_hash
    assert [row.message_id for row in loaded.predictions] == ["a"]
    assert loaded.manifest.orders == ("default",)


def test_the_manifest_says_how_many_messages_were_run():
    """A --limit smoke run is otherwise indistinguishable from a full one, and
    a reader scoring it later would see n=10 with nothing saying why."""
    manifest = evalrun.build_manifest(model="m", body_chars=300, n_messages=10)
    assert manifest.n_messages == 10


def test_the_manifest_records_what_would_make_two_runs_incomparable():
    """Each of these changes predictions, and a results file carries no trace
    of them unless they are written down beside it."""
    manifest = evalrun.build_manifest(model="m", body_chars=300)
    for field in ("model", "prompt_id", "prompt_hash", "body_chars",
                  "extraction_version", "orders", "sample_hash", "git_commit"):
        assert getattr(manifest, field) is not None


def test_a_half_written_trailing_line_costs_one_row(tmp_path):
    path = tmp_path / "run.jsonl"
    manifest = evalrun.build_manifest(model="m", body_chars=1500, run_id="r")
    evalrun.start_result(manifest, path)
    evalrun.append_prediction(
        Prediction("a", "default", "Updates", 0.7, {}, 0.9, 100, "plain", 1500),
        path,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"message_id": "b", "ord')

    loaded = evalrun.load_result(path)
    assert [row.message_id for row in loaded.predictions] == ["a"]


def test_completed_reports_pairs_for_a_resume(tmp_path):
    path = tmp_path / "run.jsonl"
    evalrun.start_result(
        evalrun.build_manifest(model="m", body_chars=1500, run_id="r"), path
    )
    evalrun.predict(
        [cached("a")], model="m", body_chars=1500,
        orders=evalrun.PERMUTED_ORDERS, classify=stub(), path=path,
    )
    assert evalrun.completed(path) == {
        ("a", "default"), ("a", "rotated"), ("a", "reversed")
    }


def test_completed_on_a_missing_file_is_empty(tmp_path):
    assert evalrun.completed(tmp_path / "nope.jsonl") == frozenset()


def test_an_empty_results_file_is_an_error_not_an_empty_run(tmp_path):
    """Silently scoring nothing would report 0% accuracy as a finding."""
    path = tmp_path / "run.jsonl"
    path.write_text("")
    with pytest.raises(ValueError):
        evalrun.load_result(path)


def test_the_permutation_orders_are_fixed_not_random():
    """Two runs have to be comparable; a random shuffle would make stability
    depend on which permutation came up."""
    assert evalrun.ROTATED == evalrun.ROTATED
    assert set(evalrun.ROTATED) == set(Category)
    assert set(evalrun.REVERSED) == set(Category)
    assert evalrun.ROTATED[0] is not Category.TO_ACTION
    assert evalrun.REVERSED[0] is Category.PERSONAL
