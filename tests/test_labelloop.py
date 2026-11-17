"""The shared keypress loop (`app/labelloop.py`), and the eval's wiring of it.

The loop writes ground truth and had no tests before Phase 3 step 9. Each
key is driven through `read` with a scripted sequence, so these tests need
no terminal. The last test runs `scripts/label_eval.py label` end to end
against a temporary cache and labels file, with keys from a pipe.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import io
import json
from collections import deque
from pathlib import Path

import pytest

from app import evallabel, evalset, labelloop
from app.categories import Category
from app.evallabel import KEYS, Cached, LabelRecord
from app.gmail_client import Message

REPO = Path(__file__).resolve().parent.parent
DIGIT = {category: key for key, category in KEYS.items()}


def cached(message_id: str) -> Cached:
    return Cached(
        message_id=message_id,
        sender="a@example.com",
        subject=f"subject {message_id}",
        internal_date="2026-11-01T00:00:00+00:00",
        text_plain="body",
        text_html="",
    )


def keys(*sequence: str):
    """A `read` that returns these keys in order, then fails loudly."""
    it = iter(sequence)

    def read() -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("the loop asked for more keys than scripted") from None

    return read


class Harness:
    """`label_loop` with in-memory storage. `written` is the file, in order."""

    def __init__(self, ids, missing=()):
        self.queue = deque(ids)
        self.resolved: dict[str, LabelRecord] = {}
        self.written: list[LabelRecord] = []
        self.missing = set(missing)
        self.shown: list[tuple[str, bool, int]] = []
        self.prefetched = 0

    def run(self, *sequence: str, limit=None) -> set[str]:
        def fetch(message_id):
            return None if message_id in self.missing else cached(message_id)

        def show(message, show_full, position):
            self.shown.append((message.message_id, show_full, position))
            return message.subject

        def record(message_id, label, unsure):
            row = LabelRecord(message_id, label, unsure, "t")
            self.written.append(row)
            return row

        def prefetch(queue):
            self.prefetched += 1

        return labelloop.label_loop(
            self.queue, self.resolved,
            fetch=fetch, show=show, record=record, prefetch=prefetch,
            limit=limit, read=keys(*sequence),
        )

    def labels(self) -> dict[str, tuple[Category, bool]]:
        return {
            mid: (row.label, row.unsure)
            for mid, row in evallabel.resolve(self.written).items()
        }


def test_a_digit_records_its_category():
    h = Harness(["m1", "m2"])
    skipped = h.run(DIGIT[Category.RECEIPTS], DIGIT[Category.PERSONAL])
    assert h.labels() == {
        "m1": (Category.RECEIPTS, False),
        "m2": (Category.PERSONAL, False),
    }
    assert skipped == set()
    assert not h.queue


def test_every_digit_maps_to_its_own_category():
    ids = [f"m{i}" for i in range(len(KEYS))]
    h = Harness(ids)
    h.run(*KEYS)
    assert [row.label for row in h.written] == list(Category)


def test_u_marks_only_the_next_digit_unsure():
    h = Harness(["m1", "m2"])
    h.run("u", DIGIT[Category.UPDATES], DIGIT[Category.UPDATES])
    assert h.labels() == {
        "m1": (Category.UPDATES, True),
        "m2": (Category.UPDATES, False),
    }


def test_u_twice_cancels_the_flag():
    h = Harness(["m1"])
    h.run("u", "u", DIGIT[Category.UPDATES])
    assert h.labels() == {"m1": (Category.UPDATES, False)}


def test_b_requeues_the_last_decision_and_the_relabel_wins():
    h = Harness(["m1", "m2"])
    h.run(
        DIGIT[Category.PROMOTIONS],  # m1
        "b",                         # back to m1, before m2 is decided
        DIGIT[Category.TO_ACTION],   # m1 again
        DIGIT[Category.RECEIPTS],    # m2
    )
    assert [row.message_id for row in h.written] == ["m1", "m1", "m2"]
    assert h.labels() == {
        "m1": (Category.TO_ACTION, False),
        "m2": (Category.RECEIPTS, False),
    }
    assert h.resolved["m1"].label is Category.TO_ACTION


def test_b_with_nothing_to_go_back_to_stays_put(capsys):
    h = Harness(["m1"])
    h.run("b", DIGIT[Category.PERSONAL])
    assert "nothing to go back to" in capsys.readouterr().out
    assert h.labels() == {"m1": (Category.PERSONAL, False)}


def test_s_skips_without_recording():
    h = Harness(["m1", "m2"])
    skipped = h.run("s", DIGIT[Category.RECEIPTS])
    assert skipped == {"m1"}
    assert h.labels() == {"m2": (Category.RECEIPTS, False)}


def test_q_stops_with_every_decision_already_recorded():
    h = Harness(["m1", "m2", "m3"])
    h.run(DIGIT[Category.RECEIPTS], "q")
    assert h.labels() == {"m1": (Category.RECEIPTS, False)}
    assert not h.queue


def test_ctrl_c_stops_like_q(capsys):
    h = Harness(["m1", "m2"])
    sequence = iter([DIGIT[Category.RECEIPTS]])

    def read():
        try:
            return next(sequence)
        except StopIteration:
            raise KeyboardInterrupt from None

    labelloop.label_loop(
        h.queue, h.resolved,
        fetch=cached, show=lambda m, f, p: "", prefetch=lambda q: None,
        record=lambda mid, label, unsure: LabelRecord(mid, label, unsure, "t"),
        limit=None, read=read,
    )
    assert set(h.resolved) == {"m1"}
    assert "interrupted" in capsys.readouterr().out


def test_m_shows_the_full_message_then_a_digit_decides():
    h = Harness(["m1"])
    h.run("m", DIGIT[Category.UPDATES])
    assert h.shown == [("m1", False, 1), ("m1", True, 1)]
    assert h.labels() == {"m1": (Category.UPDATES, False)}


def test_an_unknown_key_redraws_and_decides_nothing():
    h = Harness(["m1"])
    h.run("x", "\n", DIGIT[Category.UPDATES])
    assert len(h.shown) == 3
    assert len(h.written) == 1


def test_a_message_that_cannot_be_fetched_is_skipped(capsys):
    h = Harness(["m1", "m2"], missing={"m1"})
    skipped = h.run(DIGIT[Category.RECEIPTS])
    assert skipped == {"m1"}
    assert h.labels() == {"m2": (Category.RECEIPTS, False)}
    assert "m1: could not be fetched" in capsys.readouterr().out


def test_limit_counts_decisions_not_keypresses():
    h = Harness(["m1", "m2", "m3"])
    h.run("u", "u", DIGIT[Category.RECEIPTS], "s", DIGIT[Category.UPDATES], limit=2)
    assert set(h.labels()) == {"m1", "m3"}


def test_position_counts_resolved_messages():
    h = Harness(["m1", "m2"])
    h.resolved["old"] = LabelRecord("old", Category.UPDATES, False, "t")
    h.run(DIGIT[Category.RECEIPTS], DIGIT[Category.RECEIPTS])
    assert [position for _, _, position in h.shown] == [2, 3]


def test_prefetch_runs_before_each_message():
    h = Harness(["m1", "m2"])
    h.run(DIGIT[Category.RECEIPTS], DIGIT[Category.RECEIPTS])
    assert h.prefetched == 2


def test_footer_lists_every_key_and_the_unsure_flag():
    plain = labelloop.footer(False)
    for key, category in KEYS.items():
        assert f"{key} {category.value}" in plain
    assert "UNSURE" not in plain
    assert "UNSURE" in labelloop.footer(True)


def test_read_key_off_a_tty_reads_one_line(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("3\n\n"))
    assert labelloop.read_key() == "3"
    # An empty line, then end of input: both quit rather than hang.
    assert labelloop.read_key() == "q"
    assert labelloop.read_key() == "q"


# --- prefetch ---------------------------------------------------------------


def message(message_id: str) -> Message:
    return Message(
        id=message_id, thread_id="t", sender="a@example.com", subject="s",
        internal_date=dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
        text_plain="body", text_html="", label_ids=("INBOX",),
    )


def test_prefetch_fetches_only_the_missing_part_of_the_next_batch(tmp_path):
    evallabel.cache_put(message("m1"), tmp_path)
    fetched: list[str] = []

    def get(message_id):
        fetched.append(message_id)
        return message(message_id)

    labelloop.prefetch(get, deque(["m1", "m2", "m3", "m4"]), 3, tmp_path)
    assert fetched == ["m2", "m3"]
    assert evallabel.cache_missing(["m1", "m2", "m3", "m4"], tmp_path) == ["m4"]


def test_prefetch_with_everything_cached_never_calls_get(tmp_path):
    evallabel.cache_put(message("m1"), tmp_path)

    def get(message_id):
        raise AssertionError("fetched a cached message")

    labelloop.prefetch(get, deque(["m1"]), 50, tmp_path)


# --- scripts/label_eval.py still works --------------------------------------


@pytest.fixture
def label_eval():
    spec = importlib.util.spec_from_file_location(
        "label_eval_under_test", REPO / "scripts" / "label_eval.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_label_eval_sitting_writes_labeled_rows(label_eval, tmp_path, monkeypatch):
    """One scripted sitting through the script's own wiring, offline.

    Every message is cached, so no Gmail service is built; keys come from a
    pipe, which `read_key` reads line by line.
    """
    ids = ["m1", "m2", "m3"]
    sample = [evalset.Sampled(mid, "S", "R", "dev") for mid in ids]
    cache = tmp_path / "eval_cache"
    labeled = tmp_path / "labeled.jsonl"
    for mid in ids:
        evallabel.cache_put(message(mid), cache)

    monkeypatch.setattr(label_eval, "CACHE_DIR", cache)
    monkeypatch.setattr(evallabel, "LABELED_PATH", labeled)
    monkeypatch.setattr(evalset, "load_sample", lambda: (sample, 7))
    monkeypatch.setattr(
        label_eval.gmail_client, "service",
        lambda: pytest.fail("a fully cached sitting must not build a service"),
    )
    order = evallabel.ordering(sample, 7)
    # first: Receipts, unsure; second: Promotions, then back, then To Action;
    # third: skipped; then end of input quits.
    r, p, t = (DIGIT[c] for c in (Category.RECEIPTS, Category.PROMOTIONS, Category.TO_ACTION))
    script = f"u\n{r}\n{p}\nb\n{t}\ns\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(script))

    args = argparse.Namespace(relabel=None, body_chars=1500, limit=None, batch=50)
    assert label_eval.cmd_label(args) == 0

    rows = [json.loads(line) for line in labeled.read_text().splitlines()]
    assert [(r["message_id"], r["label"], r["unsure"], r["pass"]) for r in rows] == [
        (order[0], "Receipts", True, 1),
        (order[1], "Promotions", False, 1),
        (order[1], "To Action", False, 1),
    ]
    assert set(rows[0]) == evallabel.LABEL_KEYS
    records, damaged = evallabel.load_records(labeled)
    assert damaged == 0
    assert evallabel.resolve(records)[order[1]].label is Category.TO_ACTION
    assert order[2] not in evallabel.resolve(records)
