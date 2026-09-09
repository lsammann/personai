"""The classification log: round-trips, tolerant reads, and run identity."""

import datetime as dt
import json

import pytest

from app import logbook
from app.categories import INBOX, PROCESSED, Category, label_for
from app.logbook import Action, Record


def record(**overrides):
    fields = {
        "run_id": "20260909T101112-abc123",
        "run_kind": "poll",
        "message_id": "18f2c",
        "sender": "billing@octopus.energy",
        "subject": "Your bill is ready",
        "source": "model",
        "category": Category.TO_ACTION,
        "confidence": 0.91,
        "distribution": dict.fromkeys(Category, 0.0) | {Category.TO_ACTION: 0.91},
        "model": "llama3.1:8b",
        "prompt_version": 1,
        "body_chars": 1500,
        "confidence_threshold": 0.8,
        "to_action_floor": 0.15,
        "retained_mass": 0.97,
        "action": Action(add=(label_for(Category.TO_ACTION), PROCESSED)),
    }
    return Record(**(fields | overrides))


def test_round_trip(tmp_path):
    path = tmp_path / "log.jsonl"
    logbook.append(record(), path)
    records, skipped = logbook.read_all(path)
    assert skipped == 0
    assert records[0].category is Category.TO_ACTION
    assert records[0].confidence == pytest.approx(0.91)


def test_all_six_probabilities_survive(tmp_path):
    """The runner-up drives the asymmetric rule and a saturated distribution is
    only visible here, so a partial distribution defeats the point."""
    path = tmp_path / "log.jsonl"
    logbook.append(record(), path)
    stored = logbook.read_all(path)[0][0]
    assert set(stored.distribution) == set(Category)


def test_categories_serialise_as_their_label_names(tmp_path):
    path = tmp_path / "log.jsonl"
    logbook.append(record(), path)
    raw = json.loads(path.read_text())
    assert raw["category"] == "To Action"
    assert "To Action" in raw["distribution"]


def test_retained_mass_round_trips(tmp_path):
    """Logged so a confidently-wrong row can be checked for whether the model
    was actually engaged, which `confidence` alone cannot show."""
    path = tmp_path / "log.jsonl"
    logbook.append(record(retained_mass=0.12), path)
    assert logbook.read_all(path)[0][0].retained_mass == pytest.approx(0.12)


def test_retained_mass_is_none_when_no_model_ran(tmp_path):
    """Prefilter hits and corrections never called the model."""
    path = tmp_path / "log.jsonl"
    logbook.append(record(source="prefilter", retained_mass=None,
                          confidence=None, distribution={}), path)
    assert logbook.read_all(path)[0][0].retained_mass is None


def test_append_does_not_rewrite_earlier_rows(tmp_path):
    path = tmp_path / "log.jsonl"
    logbook.append(record(message_id="first"), path)
    logbook.append(record(message_id="second"), path)
    records, _ = logbook.read_all(path)
    assert [r.message_id for r in records] == ["first", "second"]


def test_dry_run_still_records_the_intended_labels(tmp_path):
    """The whole reason `action` and `dry_run` are separate fields. A dry run
    writes no labels by definition, so the log is the only record it produces -
    and a row saying merely "dry_run" would have thrown that record away."""
    path = tmp_path / "log.jsonl"
    logbook.append(
        record(
            dry_run=True,
            action=Action(add=(label_for(Category.RECEIPTS), PROCESSED),
                          remove=(INBOX,)),
        ),
        path,
    )
    stored = logbook.read_all(path)[0][0]
    assert stored.dry_run is True
    assert stored.action.add == (label_for(Category.RECEIPTS), PROCESSED)
    assert stored.action.remove == (INBOX,)


def test_runs_are_separable(tmp_path):
    """A hand-run over twenty emails must not skew the live numbers."""
    path = tmp_path / "log.jsonl"
    logbook.append(record(run_id="run-a", run_kind="manual"), path)
    logbook.append(record(run_id="run-b", run_kind="poll"), path)
    records, _ = logbook.read_all(path)
    assert {r.run_id for r in records} == {"run-a", "run-b"}
    assert [r.run_kind for r in records if r.run_id == "run-a"] == ["manual"]


def test_new_run_ids_do_not_collide():
    """Two runs started in the same second must stay distinct."""
    assert len({logbook.new_run_id() for _ in range(50)}) == 50


def test_new_run_id_prefix_is_a_fixed_width_utc_timestamp():
    """Fixed width is what makes lexicographic order chronological, which is
    the only reason the timestamp is in there rather than a bare uuid."""
    stamp, _, suffix = logbook.new_run_id().partition("-")
    assert len(stamp) == 15
    parsed = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=dt.UTC)
    assert abs((dt.datetime.now(dt.UTC) - parsed).total_seconds()) < 60
    assert suffix


def test_unparseable_line_is_skipped_and_counted(tmp_path):
    """One stale or half-written line must degrade the Metrics view slightly,
    not take it down."""
    path = tmp_path / "log.jsonl"
    logbook.append(record(message_id="good-1"), path)
    with path.open("a") as handle:
        handle.write("{not json\n")
        handle.write('{"run_id": "x"}\n')      # valid JSON, wrong schema
    logbook.append(record(message_id="good-2"), path)

    records, skipped = logbook.read_all(path)
    assert [r.message_id for r in records] == ["good-1", "good-2"]
    assert skipped == 2


def test_blank_lines_are_not_counted_as_damage(tmp_path):
    path = tmp_path / "log.jsonl"
    logbook.append(record(), path)
    with path.open("a") as handle:
        handle.write("\n\n")
    records, skipped = logbook.read_all(path)
    assert len(records) == 1
    assert skipped == 0


def test_missing_file_reads_as_empty(tmp_path):
    assert logbook.read_all(tmp_path / "nope.jsonl") == ([], 0)


def test_failure_row_carries_an_error_and_no_labels(tmp_path):
    path = tmp_path / "log.jsonl"
    logbook.append(
        record(category=None, confidence=None, distribution={},
               action=Action(), error="Ollama call failed: connection refused"),
        path,
    )
    stored = logbook.read_all(path)[0][0]
    assert stored.error is not None
    assert stored.action.add == ()


@pytest.mark.parametrize("field,value", [("run_kind", "whenever"), ("source", "vibes")])
def test_unknown_enum_values_are_rejected_on_write(field, value):
    """Writes are strict: a typo'd run_kind would silently create a fourth
    bucket that no metric knows to look in."""
    with pytest.raises(ValueError):
        record(**{field: value})


def test_default_path_is_resolved_at_call_time(tmp_path, monkeypatch):
    """Guards the conftest redirect. If LOG_PATH were bound as a default
    argument, the whole suite would write to the real data/ log."""
    target = tmp_path / "elsewhere.jsonl"
    monkeypatch.setattr(logbook, "LOG_PATH", target)
    logbook.append(record())
    assert target.exists()
