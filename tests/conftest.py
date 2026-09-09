"""Shared fixtures.

Every test that touches disk is redirected into tmp_path. The real
`data/token.json`, `data/config.json` and `data/classifications.jsonl` must
never be read or written by the suite - a test that clobbers a live OAuth
token would be a memorable way to learn this lesson.

`PREFILTER_PATH` is deliberately NOT redirected: it points at a committed
repo file that one test reads on purpose, and every other prefilter test
passes an explicit tmp_path.
"""

import pytest

from app import auth, config, logbook


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch):
    # auth.py does `from app.config import TOKEN_PATH`, binding the value into
    # its own namespace - so patching app.config.TOKEN_PATH would not affect
    # it. Both names have to be patched.
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LOG_PATH", tmp_path / "classifications.jsonl")
    monkeypatch.setattr(logbook, "LOG_PATH", tmp_path / "classifications.jsonl")
    # Module-level config cache leaks between tests otherwise.
    monkeypatch.setattr(config, "_config", None)
    auth._pending_flows.clear()
    yield
    auth._pending_flows.clear()
