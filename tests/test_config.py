"""Config defaults, validation constraints, and disk round-trip."""

import json

import pytest
from pydantic import ValidationError

from app import config
from app.config import REPO_ROOT, Config


def test_defaults():
    c = Config()
    assert c.poll_interval_seconds == 60
    assert c.confidence_threshold == 0.8
    assert c.to_action_floor == 0.15
    assert c.ollama_model == "llama3.1:8b"
    assert c.max_failures_before_error == 3


def test_dry_run_defaults_true():
    """A fresh install must not be able to write to the inbox by accident."""
    assert Config().dry_run is True


def test_example_file_matches_defaults():
    """Guards against config.example.json silently drifting from the model.

    The example is committed documentation that nothing loads, so drift is
    invisible until someone copies it and gets different behaviour.
    """
    example = (REPO_ROOT / "config.example.json").read_text()
    assert Config.model_validate_json(example) == Config()


@pytest.mark.parametrize(
    "field,value",
    [
        ("confidence_threshold", 1.5),   # not a probability
        ("confidence_threshold", -0.1),
        ("to_action_floor", 1.5),
        ("to_action_floor", -0.1),
        ("poll_interval_seconds", 0),    # would hot-loop the Gmail API
        ("poll_interval_seconds", -1),
        ("max_failures_before_error", 0),  # give up before trying
    ],
)
def test_rejects_out_of_range(field, value):
    """These are silent behavioural bugs, not crashes, if they get through.

    A threshold of 1.5 is valid Python - it just quietly routes 100% of mail
    to Needs-Review.
    """
    with pytest.raises(ValidationError):
        Config(**{field: value})


def test_accepts_boundary_values():
    assert Config(confidence_threshold=0.0).confidence_threshold == 0.0
    assert Config(confidence_threshold=1.0).confidence_threshold == 1.0
    assert Config(poll_interval_seconds=10).poll_interval_seconds == 10


def test_save_then_load_round_trip():
    config.save(Config(ollama_model="qwen2.5:3b", dry_run=False))
    monkeyed = config.load()
    assert monkeyed.ollama_model == "qwen2.5:3b"
    assert monkeyed.dry_run is False


def test_partial_file_falls_back_to_defaults():
    """A file need only carry what was deliberately changed."""
    config.CONFIG_PATH.write_text(json.dumps({"ollama_model": "qwen2.5:3b"}))
    c = config.load()
    assert c.ollama_model == "qwen2.5:3b"
    assert c.confidence_threshold == 0.8       # untouched default
    assert c.dry_run is True


def test_missing_file_uses_defaults():
    assert not config.CONFIG_PATH.exists()
    assert config.load() == Config()


def test_save_replaces_in_memory_object():
    """save() swaps the object rather than mutating it.

    Anything holding a reference from before a save keeps the stale one, which
    is why the poller must call config.get() inside its loop.
    """
    before = config.get()
    after = config.save(Config(ollama_model="llama3.2:3b"))
    assert config.get() is after
    assert config.get() is not before
