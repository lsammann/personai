"""`app/review.py`: the dry-run review's rows, order, shown line and score.

All on hand-built log rows and label records. The figures are gate step 2's,
so the boundaries are tested exactly on the agreed marks.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from app import review
from app.categories import (
    INBOX,
    KEEPS_INBOX,
    NEEDS_REVIEW,
    PROCESSED,
    Category,
    label_for,
)
from app.evallabel import LabelRecord
from app.logbook import Action, Record

RUN = "20261104T121423-f46437"
OTHER = "20261104T102240-e04cfe"


def model_row(mid, category, confidence=0.95, *, archive=None, needs_review=False,
              run_id=RUN, prior=("INBOX",)):
    """A model row as the agent writes it on a dry run."""
    if needs_review:
        add, remove = (NEEDS_REVIEW, PROCESSED), ()
    else:
        keep = archive is False or (archive is None and category in KEEPS_INBOX)
        add, remove = (label_for(category), PROCESSED), () if keep else (INBOX,)
    return Record(
        run_id=run_id, run_kind="manual", message_id=mid, source="model",
        category=category, confidence=confidence, prior_labels=prior,
        action=Action(add=add, remove=remove), dry_run=True,
    )


def reply_row(mid):
    return Record(
        run_id=RUN, run_kind="manual", message_id=mid, source="reply",
        category=Category.PERSONAL, prior_labels=("INBOX",),
        action=Action(add=(label_for(Category.PERSONAL), PROCESSED)), dry_run=True,
    )


def failure_row(mid, *, source="model"):
    return Record(
        run_id=RUN, run_kind="manual", message_id=mid, source=source,
        prior_labels=None if source is None else ("INBOX",),
        error="classify: NoCategoryLetter: none", dry_run=True,
    )


def prefilter_row(mid):
    return Record(
        run_id=RUN, run_kind="manual", message_id=mid, source="prefilter",
        category=Category.PROMOTIONS, prior_labels=("INBOX",),
        action=Action(add=(label_for(Category.PROMOTIONS), PROCESSED), remove=(INBOX,)),
        dry_run=True,
    )


def truth(mid, category, unsure=False):
    return LabelRecord(mid, category, unsure, "2026-11-04T00:00:00+00:00")


def rows_of(*records):
    return {r.message_id: r for r in records}


def labels_of(*records):
    return {r.message_id: r for r in records}


# --- run_rows / review_path ------------------------------------------------


def test_run_rows_keeps_only_that_run_in_log_order():
    log = [model_row("a", Category.RECEIPTS),
           model_row("x", Category.RECEIPTS, run_id=OTHER),
           model_row("b", Category.UPDATES)]
    assert list(review.run_rows(log, RUN)) == ["a", "b"]
    assert review.run_rows(log, "20990101T000000-000000") == {}


def test_run_rows_refuses_two_rows_for_one_message():
    log = [model_row("a", Category.RECEIPTS), model_row("a", Category.UPDATES)]
    with pytest.raises(ValueError, match="two rows"):
        review.run_rows(log, RUN)


def test_review_path_takes_only_a_run_id():
    assert review.review_path(RUN) == review.REVIEW_DIR / f"{RUN}.jsonl"
    for bad in ("../token", "20261104T121423-f46437/x", "", "abc"):
        with pytest.raises(ValueError):
            review.review_path(bad)


def test_review_cache_is_not_the_eval_cache():
    assert review.CACHE_DIR.name == "review_cache"
    assert review.REVIEW_DIR.name == "reviews"


# --- ordering ---------------------------------------------------------------


IDS = [f"m{i:03d}" for i in range(60)]


def test_ordering_is_stable_for_one_run_id():
    assert review.ordering(IDS, RUN) == review.ordering(list(reversed(IDS)), RUN)


def test_ordering_differs_between_run_ids():
    assert review.ordering(IDS, RUN) != review.ordering(IDS, OTHER)


def test_ordering_has_every_id_once_and_is_shuffled():
    order = review.ordering(IDS, RUN)
    assert sorted(order) == sorted(IDS)
    assert len(order) == len(set(order))
    assert order != sorted(IDS)


def test_ordering_is_fixed_across_processes():
    """A string seed must not depend on PYTHONHASHSEED, or a resumed sitting
    would see a new order."""
    code = (
        "from app import review;"
        f"print(review.ordering({IDS!r}, {RUN!r}))"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True,
            env={"PYTHONHASHSEED": seed, "PATH": ""},
        ).stdout
        for seed in ("1", "2")
    }
    assert outputs == {f"{review.ordering(IDS, RUN)}\n"}


# --- suggestion -------------------------------------------------------------


def test_suggestion_model_row_shows_category_confidence_action():
    row = model_row("a", Category.PROMOTIONS, 0.959)
    assert review.suggestion(row) == "model:  Promotions 0.959 -> archive"


def test_suggestion_model_row_that_keeps():
    row = model_row("a", Category.TO_ACTION, 0.81)
    assert review.suggestion(row) == "model:  To Action 0.810 -> keep"


def test_suggestion_reply_rule():
    assert review.suggestion(reply_row("a")) == "model:  reply rule -> keep"


def test_suggestion_needs_review():
    row = model_row("a", Category.RECEIPTS, 0.5, needs_review=True)
    assert review.suggestion(row) == "model:  Needs Review -> keep"


def test_suggestion_failure():
    assert review.suggestion(failure_row("a")) == "model:  failed -> keep"
    assert review.suggestion(failure_row("b", source=None)) == "model:  failed -> keep"


def test_suggestion_allowlist():
    assert review.suggestion(prefilter_row("a")) == "model:  allowlist -> archive"


# --- score ------------------------------------------------------------------


def test_archived_personal_is_a_costly_error_and_a_personal_archive():
    rows = rows_of(model_row("a", Category.PROMOTIONS),
                   model_row("b", Category.PERSONAL))
    labels = labels_of(truth("a", Category.PERSONAL), truth("b", Category.PERSONAL))
    report = review.score(rows, labels)
    assert (report.costly.k, report.costly.n) == (1, 2)
    assert report.personal_archived == 1
    assert (report.action.k, report.action.n) == (1, 2)


def test_needs_review_with_archive_truth_is_a_wrong_action():
    rows = rows_of(model_row("a", Category.RECEIPTS, 0.5, needs_review=True),
                   model_row("b", Category.UPDATES, 0.5, needs_review=True))
    labels = labels_of(truth("a", Category.RECEIPTS), truth("b", Category.RECEIPTS))
    report = review.score(rows, labels)
    assert report.action.k == 0          # both kept, both should have archived
    assert report.costly.n == 0          # no keeps-INBOX truth at all
    assert report.six_way.k == 1         # only the one recorded as Receipts
    assert report.needs_review == 2


def test_a_failure_row_counts_as_kept_and_never_six_way_correct():
    rows = rows_of(failure_row("a"), failure_row("b", source=None))
    labels = labels_of(truth("a", Category.TO_ACTION), truth("b", Category.RECEIPTS))
    report = review.score(rows, labels)
    assert (report.action.k, report.action.n) == (1, 2)  # kept: right for a only
    assert report.costly.k == 0
    assert report.six_way.k == 0
    assert report.failures == 2
    assert (report.to_action_retention.k, report.to_action_retention.n) == (1, 1)


def test_an_unreviewed_row_is_in_no_proportion():
    rows = rows_of(model_row("a", Category.RECEIPTS),
                   model_row("b", Category.PROMOTIONS))
    report = review.score(rows, labels_of(truth("a", Category.RECEIPTS)))
    assert (report.rows, report.reviewed, report.not_reviewed) == (2, 1, 1)
    for p in (report.action, report.six_way):
        assert p.n == 1
    assert "incomplete" in "\n".join(review.report_lines(report, RUN))


def test_labels_outside_the_run_are_ignored_and_counted():
    rows = rows_of(model_row("a", Category.RECEIPTS))
    labels = labels_of(truth("a", Category.RECEIPTS), truth("zz", Category.PERSONAL))
    report = review.score(rows, labels)
    assert report.reviewed == 1
    assert report.stray_labels == 1


def test_to_action_retention_and_reply_figures():
    rows = rows_of(model_row("a", Category.TO_ACTION),
                   model_row("b", Category.UPDATES),
                   reply_row("c"), reply_row("d"))
    labels = labels_of(truth("a", Category.TO_ACTION), truth("b", Category.TO_ACTION),
                       truth("c", Category.PERSONAL), truth("d", Category.UPDATES))
    report = review.score(rows, labels)
    assert (report.to_action_retention.k, report.to_action_retention.n) == (1, 2)
    assert (report.reply.k, report.reply.n) == (1, 2)
    assert report.reply_label_exact == 1


def test_unsure_is_counted():
    rows = rows_of(model_row("a", Category.RECEIPTS))
    report = review.score(rows, labels_of(truth("a", Category.RECEIPTS, unsure=True)))
    assert report.unsure == 1


def test_a_row_fetched_outside_the_inbox_refuses_to_score():
    row = model_row("a", Category.RECEIPTS, archive=False, prior=("CATEGORY_UPDATES",))
    rows = rows_of(row)
    assert review.not_in_inbox(rows) == ["a"]
    with pytest.raises(ValueError, match="not in the inbox"):
        review.score(rows, labels_of(truth("a", Category.RECEIPTS)))


def test_a_fetch_failure_is_not_treated_as_outside_the_inbox():
    assert review.not_in_inbox(rows_of(failure_row("a", source=None))) == []


def _action_report(right: int, n: int) -> review.GateReport:
    """n Receipts truths; the first `right` archived (correct), the rest kept."""
    rows, labels = {}, {}
    for i in range(n):
        mid = f"m{i}"
        rows[mid] = model_row(mid, Category.RECEIPTS, archive=i < right)
        labels[mid] = truth(mid, Category.RECEIPTS)
    return review.score(rows, labels)


def test_action_accuracy_passes_exactly_at_084():
    assert _action_report(84, 100).action_passes
    assert _action_report(21, 25).action_passes        # 0.84 again
    assert not _action_report(83, 100).action_passes


def _costly_report(costly: int, keeps: int) -> review.GateReport:
    """`keeps` To Action truths; the first `costly` archived."""
    rows, labels = {}, {}
    for i in range(keeps):
        mid = f"k{i}"
        rows[mid] = model_row(mid, Category.TO_ACTION, archive=i < costly)
        labels[mid] = truth(mid, Category.TO_ACTION)
    return review.score(rows, labels)


def test_costly_rate_passes_exactly_at_two_percent():
    assert _costly_report(2, 100).costly_passes
    assert _costly_report(1, 50).costly_passes         # 0.02 again
    assert not _costly_report(3, 100).costly_passes
    assert not _costly_report(2, 99).costly_passes


def test_nothing_reviewed_does_not_pass():
    report = review.score(rows_of(model_row("a", Category.RECEIPTS)), {})
    assert not report.action_passes
    assert not report.costly_passes
    assert not report.passes


def test_proportion_value_and_interval():
    p = review.Proportion(84, 100)
    assert p.value == pytest.approx(0.84)
    lo, hi = p.interval
    assert lo < 0.84 < hi
    assert review.Proportion(0, 0).value is None


def test_report_prints_the_caveat_and_the_verdict():
    report = _action_report(90, 100)
    lines = review.report_lines(report, RUN)
    assert lines.count(review.CAVEAT) == 2
    assert lines[1] == review.CAVEAT and lines[-1] == review.CAVEAT
    text = "\n".join(lines)
    assert "NOT BLIND" in text
    # No keeps-INBOX truth, so the costly rate is undefined: the gate fails.
    assert "gate step 2: FAIL" in text


def test_report_flags_the_personal_watch_trigger():
    rows = rows_of(model_row("a", Category.PROMOTIONS))
    report = review.score(rows, labels_of(truth("a", Category.PERSONAL)))
    assert "WATCH TRIGGER" in "\n".join(review.report_lines(report, RUN))


# --- purity -----------------------------------------------------------------


def test_review_imports_nothing_that_does_io():
    """Pure: no Gmail, no auth, no files, no terminal."""
    source = Path(review.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= {
        "__future__", "random", "re", "collections.abc", "dataclasses", "fractions",
        "pathlib", "app.categories", "app.config", "app.evallabel", "app.evalscore",
        "app.logbook",
    }
