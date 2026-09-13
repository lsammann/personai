"""Running predictions over the eval set, and the results file they land in.

The other half of the split from `evalscore`: this is the part that costs an
hour of CPU, so it is done once and its output is scored many times. It talks
to Ollama and to the disk; nothing here computes a metric.

Two things it deliberately does NOT do. It never touches Gmail - the cache
written at label time is the input, which is what makes an eval run
reproducible, offline and independent of what the mailbox looks like today.
And it never picks a body: `classifier.build_user_message` does that, from the
same four fields the live poller will pass, so the eval measures the pipeline
that will actually run rather than a lookalike.

The results file is committed. It holds ids, probabilities and timings - no
sender, subject or body - so the regression record is auditable without
putting mail in git. A key allowlist is checked on write, as in `evalset` and
`evallabel`.

See `docs/PHASE2_PLAN.md` -> Step 5 implementation.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from app import classifier
from app.categories import DEFAULT_ORDER, Category
from app.classifier import DEFAULT_PROMPT_ID, ClassifierError
from app.evallabel import Cached
from app.evalscore import Prediction
from app.evalset import EVAL_DIR, SAMPLE_PATH
from app.logbook import new_run_id
from app.message_body import EXTRACTION_VERSION, select_body

RESULTS_DIR = EVAL_DIR / "results"

# Ids and numbers only. Never a sender, subject, body or snippet.
RESULT_KEYS = frozenset({
    "message_id", "order_name", "category", "confidence", "distribution",
    "retained_mass", "latency_ms", "body_source", "body_len", "error",
})

# The letter orders a permutation run uses. Fixed rather than random: two runs
# have to be comparable, and a random permutation would make the stability
# number depend on which shuffle came up. A rotation moves every category by
# one position; a reversal moves the first to last, which is the arrangement
# most likely to expose a model anchored on "A".
ROTATED = DEFAULT_ORDER[1:] + DEFAULT_ORDER[:1]
REVERSED = tuple(reversed(DEFAULT_ORDER))

DEFAULT_ORDERS: tuple[tuple[str, tuple[Category, ...]], ...] = (
    ("default", DEFAULT_ORDER),
)
PERMUTED_ORDERS = DEFAULT_ORDERS + (
    ("rotated", ROTATED),
    ("reversed", REVERSED),
)


@dataclass(frozen=True)
class Manifest:
    """What was true of the run. Every field is something that changes predictions.

    Two runs differing in any of these are not comparable, and the failure is
    silent - a results file carries no trace of the prompt that produced it
    unless the prompt is written down beside it. `prompt_hash` is the backstop
    for `prompt_id`: it catches a `categories.DESCRIPTIONS` edit made without a
    new id. `extraction_version` does the same job for body selection, and
    `sample_hash` for a re-drawn eval set.
    """

    run_id: str
    model: str
    prompt_id: str
    prompt_hash: str
    body_chars: int
    extraction_version: str
    orders: tuple[str, ...]
    sample_hash: str
    git_commit: str
    created_at: str
    # How many messages were put through. A `--limit 10` smoke run is
    # otherwise indistinguishable from a full 200-message run in its manifest,
    # and a reader scoring it months later would see n=10 with nothing saying
    # why. Found by running the first smoke slice and reading the file back.
    n_messages: int = 0


@dataclass(frozen=True)
class RunResult:
    manifest: Manifest
    predictions: list[Prediction]


def build_manifest(
    *,
    model: str,
    body_chars: int,
    prompt_id: str = DEFAULT_PROMPT_ID,
    orders: Sequence[tuple[str, tuple[Category, ...]]] = DEFAULT_ORDERS,
    sample_path: Path = SAMPLE_PATH,
    run_id: str | None = None,
    n_messages: int = 0,
) -> Manifest:
    return Manifest(
        run_id=run_id or new_run_id(),
        model=model,
        prompt_id=prompt_id,
        prompt_hash=classifier.prompt_hash(prompt_id),
        body_chars=body_chars,
        extraction_version=EXTRACTION_VERSION,
        orders=tuple(name for name, _ in orders),
        sample_hash=file_hash(sample_path),
        git_commit=git_commit(),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        n_messages=n_messages,
    )


def file_hash(path: Path) -> str:
    """A short digest of a file's bytes, or "missing"."""
    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def git_commit() -> str:
    """The working commit, so a result can be tied back to the code that made it.

    Best-effort: a run from a tarball with no git available should still
    produce a results file, just one that says so.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


# --- prediction -----------------------------------------------------------


def predict(
    messages: Sequence[Cached],
    *,
    model: str,
    body_chars: int,
    prompt_id: str = DEFAULT_PROMPT_ID,
    orders: Sequence[tuple[str, tuple[Category, ...]]] = DEFAULT_ORDERS,
    classify: Callable[..., classifier.Interpretation] = classifier.classify,
    path: Path | None = None,
    done: frozenset[tuple[str, str]] = frozenset(),
    on_row: Callable[[Prediction], None] | None = None,
) -> list[Prediction]:
    """One row per (message, letter order). Appends as it goes.

    `classify` is injected rather than imported at the call site so the tests
    drive the whole loop with a stub and never open a socket. The production
    default is the real thing.

    A `ClassifierError` is caught and recorded as a row with `error` set and no
    category - never re-raised, and never silently turned into a wrong answer.
    `decision.py` draws the same line for the same reason: a failure applies no
    labels and retries, where a wrong answer is a measurement. Scoring an
    unreachable Ollama as a misclassification would blame the model for the
    network.

    `done` lets an interrupted run resume. Two hundred messages at ~6 seconds
    is twenty minutes of CPU; losing it to a stray Ctrl-C would make the sweep
    in step 6 painful enough to cut corners on.
    """
    rows: list[Prediction] = []
    for message in messages:
        selection = select_body(message.text_plain, message.text_html)
        body_len = len(selection.text[:body_chars])
        for order_name, order in orders:
            if (message.message_id, order_name) in done:
                continue
            started = time.perf_counter()
            try:
                result = classify(
                    message.sender,
                    message.subject,
                    message.text_plain,
                    message.text_html,
                    model=model,
                    body_chars=body_chars,
                    prompt_id=prompt_id,
                    order=order,
                )
                row = Prediction(
                    message_id=message.message_id,
                    order_name=order_name,
                    category=result.category.value,
                    confidence=result.confidence,
                    distribution={
                        category.value: probability
                        for category, probability in result.distribution.items()
                    },
                    retained_mass=result.retained_mass,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    body_source=selection.source,
                    body_len=body_len,
                )
            except ClassifierError as exc:
                row = Prediction(
                    message_id=message.message_id,
                    order_name=order_name,
                    category=None,
                    confidence=None,
                    distribution={},
                    retained_mass=None,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    body_source=selection.source,
                    body_len=body_len,
                    error=f"{type(exc).__name__}: {exc}",
                )
            rows.append(row)
            if path is not None:
                append_prediction(row, path)
            if on_row is not None:
                on_row(row)
    return rows


# --- the results file -----------------------------------------------------


def start_result(manifest: Manifest, path: Path) -> None:
    """Write the manifest as line 1, the idiom `evalset.save_frame` uses.

    In the file rather than a sidecar so the two cannot be separated: a
    results file that has lost its manifest is a column of numbers whose
    meaning is unrecoverable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"manifest": asdict(manifest)}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def append_prediction(row: Prediction, path: Path) -> None:
    """Append one prediction, flushed. Strict: a bad row is never written."""
    payload = asdict(row)
    extra = set(payload) - RESULT_KEYS
    if extra:
        raise ValueError(f"refusing to write non-allowlisted keys: {sorted(extra)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def load_result(path: Path) -> RunResult:
    """Manifest plus predictions. Tolerant of a half-written trailing line.

    Same strict-write / tolerant-read split as `logbook` and `evallabel`: an
    interrupted run must cost the row it was writing, not the twenty minutes
    before it.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError(f"{path} is empty")
    fields = json.loads(lines[0])["manifest"]
    # JSON has no tuples, so `orders` returns as a list. Coerced back rather
    # than left as-is: the field is compared between runs when deciding
    # whether two results files can be scored against each other.
    fields["orders"] = tuple(fields.get("orders", ()))
    manifest = Manifest(**fields)

    predictions: list[Prediction] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            predictions.append(Prediction(**row))
        except (ValueError, TypeError):
            continue
    return RunResult(manifest=manifest, predictions=predictions)


def completed(path: Path) -> frozenset[tuple[str, str]]:
    """Which (message, order) pairs a partial file already holds."""
    if not path.exists():
        return frozenset()
    return frozenset(
        (row.message_id, row.order_name) for row in load_result(path).predictions
    )


def result_path(run_id: str, directory: Path = RESULTS_DIR) -> Path:
    return directory / f"{run_id}.jsonl"
