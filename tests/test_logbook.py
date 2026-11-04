"""The classification log: round-trips, tolerant reads, and run identity."""

import datetime as dt
import json

import pytest
from pydantic import ValidationError

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
        "prompt_id": "v1",
        "body_chars": 1500,
        "confidence_threshold": 0.8,
        "to_action_floor": 0.15,
        "retained_mass": 0.97,
        "action": Action(add=(label_for(Category.TO_ACTION), PROCESSED)),
        # Required by the first validator whenever `action` is non-empty.
        "prior_labels": (INBOX, "UNREAD"),
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


# --- Phase 3 step 6: prior_labels, labels_after, and the validators ---------


def test_prior_and_after_labels_round_trip_as_ids(tmp_path):
    """Ids, as Gmail gives them - what undo restores to and compares with."""
    path = tmp_path / "log.jsonl"
    logbook.append(
        record(prior_labels=(INBOX, "UNREAD"),
               labels_after=("Label_4", "Label_9", "UNREAD")),
        path,
    )
    stored = logbook.read_all(path)[0][0]
    assert stored.prior_labels == (INBOX, "UNREAD")
    assert stored.labels_after == ("Label_4", "Label_9", "UNREAD")


def test_a_label_change_without_prior_labels_is_refused():
    """Undo cannot trust a change it has nothing to compare against."""
    with pytest.raises(ValidationError, match="prior_labels"):
        record(prior_labels=None)


def test_no_change_needs_no_prior_labels():
    """A failed fetch has no labels to record, and changes nothing."""
    row = record(action=Action(), prior_labels=None, error="fetch failed",
                 category=None, confidence=None, distribution={})
    assert row.prior_labels is None


def test_a_failed_row_carrying_a_label_change_is_refused():
    """CLAUDE.md: failure is not low confidence. A failure applies nothing."""
    with pytest.raises(ValidationError, match="failed row"):
        record(error="ollama unreachable")


def test_a_dry_run_with_labels_after_is_refused():
    """A dry run made no change, so there is no 'after' to report."""
    with pytest.raises(ValidationError, match="dry run"):
        record(dry_run=True, labels_after=("Label_4",))


def test_a_dry_run_with_a_change_and_prior_labels_is_accepted():
    """The normal dry-run row: what it would have done, and what it saw."""
    row = record(dry_run=True, labels_after=None)
    assert row.action.add and row.prior_labels is not None


def test_the_reply_source_is_accepted(tmp_path):
    """Phase 3 step 7: the reply rule's rows carry their own source."""
    path = tmp_path / "log.jsonl"
    logbook.append(record(source="reply", confidence=None, distribution={}), path)
    assert logbook.read_all(path)[0][0].source == "reply"


def test_a_row_without_a_source_must_be_a_failure():
    """None means routing was never reached (a fetch failure, step 7 Q3).
    A successful row always knows where its answer came from."""
    with pytest.raises(ValidationError, match="without a source"):
        record(source=None)


def test_a_failure_before_routing_has_no_source(tmp_path):
    path = tmp_path / "log.jsonl"
    logbook.append(
        record(source=None, error="fetch: GmailApiError: 404", action=Action(),
               prior_labels=None, category=None, confidence=None, distribution={},
               sender="", subject=""),
        path,
    )
    stored = logbook.read_all(path)[0][0]
    assert stored.source is None and stored.error


def test_a_row_breaking_a_validator_is_never_appended(tmp_path):
    """Strict writes: the failure is at construction, before any I/O."""
    path = tmp_path / "log.jsonl"
    with pytest.raises(ValidationError):
        logbook.append(record(error="boom"), path)
    assert not path.exists()


def test_an_invalid_line_on_disk_is_skipped_not_fatal(tmp_path):
    """Reads stay tolerant: a hand-edited line that breaks a validator costs
    one row, counted, rather than the whole log."""
    path = tmp_path / "log.jsonl"
    logbook.append(record(message_id="good"), path)
    raw = json.loads(record().model_dump_json())
    raw["prior_labels"] = None
    with path.open("a") as handle:
        handle.write(json.dumps(raw) + "\n")
    records, skipped = logbook.read_all(path)
    assert [r.message_id for r in records] == ["good"]
    assert skipped == 1
