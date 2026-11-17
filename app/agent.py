"""One message, end to end: fetch, route, decide, write, log.

`classify_and_label` is the agent's only job, and its order is the point
(Phase 3 plan, §7 and the Step 7 section):

  1. fetch               keep the label ids as `prior_labels`
  2. terminal state?     already Processed or Error -> None, nothing done
  3. route               reply rule, then the allowlist, then the model
  4. decide              `decision` owns the table; nothing here re-derives it
  5. effective change    the decision against the labels the message has
  6. modify              ONE atomic call, unless this is a dry run
  7. log                 the row is appended AFTER the modify

The reply rule runs before the allowlist so a `Re:` from an allowlisted
promotional domain stays visible - the order the Phase 2 eval scored.

**Failure is not low confidence** (CLAUDE.md). A Gmail error on one message
or a classifier that gave no answer becomes a failure row: no labels, not
even `Agent/Processed`, so the message retries next run. Errors that are
about the whole mailbox - auth, any 403 (`gmail_client.Forbidden`) - are not
caught and halt the run, as is anything else: a transport error, or a bug in
this code. Catching `Exception` here would turn a bug into one quiet failure
row per message (step 7 decision Q2).

Phase 3 never dead-letters: one failure never becomes `Agent/Error`, and
nothing counts failures across runs. That is Phase 4.

Around it, the run-level pieces a caller needs and Phase 4's poller will
reuse (step 8): `queue_query` and `check_limit` say which messages a run may
take, `preflight` stops a run before its first row if Ollama cannot answer,
and `RunTally` counts what happened. `scripts/run_pass.py` is their caller.

The only module in `app/` allowed to call `gmail_client.modify_labels`,
which `tests/test_write_guard.py` enforces. It joined after the undo
rehearsal passed on 2026-11-03 (A1).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from app import classifier, decision, gmail_client, logbook, prefilter
from app.categories import (
    NEEDS_REVIEW,
    RECEIVED_ONLY,
    REQUIRED_LABELS,
    STOP_LABELS,
    UNPROCESSED_TERMS,
    Category,
)
from app.classifier import ClassifierError, Interpretation, OllamaError
from app.config import Config
from app.logbook import Action, Record, RunKind

# Waits between tries of a call that raised OllamaError: 4 retries after the
# first attempt, 5 tries and 30 s in total (plan review, 2026-11-02; delays
# step 7 Q7). Long enough to outlast an Ollama restart. NoCategoryLetter is
# never retried: for the same input the distribution is reproducible, so a
# retry asks the same question and gets the same answer.
OLLAMA_RETRY_DELAYS: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)

# Seconds per classify call, replacing classifier.DEFAULT_TIMEOUT's 600 for
# live runs - five tries at 600 s would hold one message for 50 minutes. A
# warm call takes ~2.6 s; the margin is for a cold model load. Checked
# against a measured cold start by step 7's procedure B.
CLASSIFY_TIMEOUT = 120.0

# The most messages one live run may take (§8). Phase 4 removes it
# deliberately, in its own diff. Dry runs are not capped: the gate's large
# dry run is 300 messages and writes nothing.
LIVE_LIMIT_CAP = 25

# Classified once before a run's first message (step 8 Q2). Synthetic and
# fixed, so the preflight reads no mail.
PREFLIGHT_EMAIL = (
    "preflight@personai.invalid",
    "Preflight check",
    "This message checks that the model loads and answers before a run.",
    "",
)


def queue_query(extra: str | None = None) -> str:
    """The run's Gmail query: received mail not yet in a terminal state,
    narrowed by `extra` if given.

    `extra` is parenthesised, so an `OR` inside it groups with itself and
    cannot loosen the base terms (step 8 Q1). It is a human's flag, so it is
    not otherwise parsed.
    """
    base = f"{RECEIVED_ONLY} {UNPROCESSED_TERMS}"
    extra = (extra or "").strip()
    return f"{base} ({extra})" if extra else base


def check_limit(limit: int, *, live: bool) -> None:
    """ValueError unless `limit` is at least 1 and, for a live run, at most
    `LIVE_LIMIT_CAP`. Called before any network call."""
    if limit < 1:
        raise ValueError(f"--limit must be at least 1 (got {limit})")
    if live and limit > LIVE_LIMIT_CAP:
        raise ValueError(
            f"--live takes at most {LIVE_LIMIT_CAP} messages in Phase 3 (got {limit})"
        )


def preflight(cfg: Config) -> float:
    """Classify `PREFLIGHT_EMAIL` once and return the wall time in seconds.

    No retries, and any ClassifierError propagates, so a run with Ollama
    down halts before it has fetched or logged anything, rather than
    spending ~30 s of retries per message on failure rows (step 8 Q2). It
    also takes the model load, so the run's first message is timed warm.
    """
    started = time.perf_counter()
    classifier.classify(
        *PREFLIGHT_EMAIL,
        model=cfg.ollama_model,
        body_chars=cfg.body_chars,
        prompt_id=classifier.DEFAULT_PROMPT_ID,
        timeout=CLASSIFY_TIMEOUT,
    )
    return time.perf_counter() - started


def classify_and_label(
    svc,
    message_id: str,
    *,
    cfg: Config,
    ids_by_name: Mapping[str, str],
    allowlist: Collection[str],
    run_id: str,
    run_kind: RunKind,
    dry_run: bool,
) -> Record | None:
    """Classify one message, label it unless `dry_run`, and log the row.

    Returns the row it appended, or None - with no write and no row - if the
    message is already in a terminal state. The queue query excludes those,
    but this function takes any id, and labelling one would put a second
    category beside the first, or `Processed` beside `Error`. Checking here
    makes the three-states rule a property of the writer, not only of the
    query (step 7 Q4).

    `ids_by_name` is `gmail_client.label_ids()` over `REQUIRED_LABELS`,
    resolved once per run by the caller. `dry_run` is a parameter rather than
    read from config, so the CLI's `--live` is the only thing that writes.
    """
    missing = REQUIRED_LABELS - set(ids_by_name)
    if missing:
        raise ValueError(f"ids_by_name is missing labels: {sorted(missing)}")
    names_by_id = {label_id: name for name, label_id in ids_by_name.items()}

    row: dict = {
        "run_id": run_id, "run_kind": run_kind,
        "message_id": message_id, "dry_run": dry_run,
    }

    try:
        message = gmail_client.fetch(svc, message_id)
    except gmail_client.Forbidden:
        raise
    except gmail_client.GmailApiError as error:
        # Routing was never reached, so there is no true source (Q3).
        return _append(Record(**row, source=None, error=_error("fetch", error)))

    if {ids_by_name[label] for label in STOP_LABELS} & set(message.label_ids):
        return None

    row |= {
        "sender": message.sender, "subject": message.subject,
        "prior_labels": message.label_ids,
    }
    notes: list[str] = []
    # The model's output and the thresholds that judged it. Added to the row
    # only once the label change has succeeded (or was not attempted).
    classified: dict = {}

    if prefilter.is_reply(message.subject):
        row["source"] = "reply"
        chosen = decision.decide_reply_hit()
    elif prefilter.matches(message.sender, allowlist):
        row["source"] = "prefilter"
        chosen = decision.decide_prefilter_hit()
    else:
        # Only what was used goes in the row (Q10): rule rows carry no model
        # fields and no thresholds, since neither decided them.
        row |= {
            "source": "model", "model": cfg.ollama_model,
            "prompt_id": classifier.DEFAULT_PROMPT_ID, "body_chars": cfg.body_chars,
        }
        try:
            result = _classify(message, cfg, notes)
        except ClassifierError as error:
            return _append(Record(**row, error=_error("classify", error), note=_note(notes)))
        if result.missing_letters:
            notes.insert(0, f"missing letters: {', '.join(result.missing_letters)}")
        chosen = decision.decide(
            result.distribution,
            confidence_threshold=cfg.confidence_threshold,
            to_action_floor=cfg.to_action_floor,
        )
        classified = {
            "distribution": result.distribution, "retained_mass": result.retained_mass,
            "confidence_threshold": cfg.confidence_threshold,
            "to_action_floor": cfg.to_action_floor,
        }

    prior_names = [names_by_id[i] for i in message.label_ids if i in names_by_id]
    add, remove = decision.effective_change(chosen, prior_names)

    labels_after = None
    # After the terminal-state check every successful decision adds
    # Processed (or Error), which cannot already be present, so this is
    # non-empty whenever the decision is not a failure.
    if not dry_run and (add or remove):
        try:
            labels_after = gmail_client.modify_labels(
                svc, message.id,
                add_ids=[ids_by_name[name] for name in add],
                remove_ids=[ids_by_name[name] for name in remove],
            )
        except gmail_client.Forbidden:
            raise
        except gmail_client.GmailApiError as error:
            # Probably nothing was applied. If a call that timed out had in
            # fact landed, the message carries Processed with no applied
            # row: the undo sweep reports it as an orphan, and this note says
            # what it most likely received (Q6). The row keeps the shape of
            # a failure - a row with an error is never also a classification.
            category = chosen.category.value if chosen.category else "-"
            notes.append(f"intended: {category}; {_describe(add, remove)}")
            return _append(Record(**row, error=_error("modify", error), note=_note(notes)))

    return _append(Record(
        **row,
        **classified,
        category=chosen.category,
        confidence=chosen.confidence,
        action=Action(add=add, remove=remove),
        labels_after=labels_after,
        note=_note(notes),
    ))


def _classify(message: gmail_client.Message, cfg: Config, notes: list[str]) -> Interpretation:
    """`classifier.classify`, retried on OllamaError only.

    Retries are recorded in `notes` whether or not a try finally succeeds, so
    a slow or flaky Ollama is visible in the log, not only in the wall time.
    """
    for attempt, delay in enumerate((*OLLAMA_RETRY_DELAYS, None)):
        try:
            result = classifier.classify(
                message.sender, message.subject, message.text_plain, message.text_html,
                model=cfg.ollama_model,
                body_chars=cfg.body_chars,
                prompt_id=classifier.DEFAULT_PROMPT_ID,
                timeout=CLASSIFY_TIMEOUT,
            )
        except OllamaError:
            if delay is None:
                notes.append(f"ollama retries: {attempt}")
                raise
            time.sleep(delay)
            continue
        if attempt:
            notes.append(f"ollama retries: {attempt}")
        return result
    raise AssertionError("unreachable: the last try either returns or raises")


def _append(record: Record) -> Record:
    """Log the row - always the last thing done for a message - and return it."""
    logbook.append(record)
    return record


def _error(stage: str, error: Exception) -> str:
    """`<stage>: <type>: <message>`. The stage lets Phase 4 choose which
    failures count toward dead-lettering; a deleted message's 404 is not a
    prompt the model always fails on."""
    return f"{stage}: {type(error).__name__}: {error}"


def _describe(add: Collection[str], remove: Collection[str]) -> str:
    parts = [f"+{name}" for name in add] + [f"-{name}" for name in remove]
    return " ".join(parts) or "no change"


def _note(notes: list[str]) -> str | None:
    return "; ".join(notes) or None


@dataclass
class RunTally:
    """What one run did, counted from what `classify_and_label` returned.

    Pure: it sees each return value and how long it took, and nothing else.
    """

    skipped: int = 0
    failures: int = 0
    needs_review: int = 0
    by_source: Counter[str] = field(default_factory=Counter)
    by_category: Counter[Category] = field(default_factory=Counter)
    seconds: list[float] = field(default_factory=list)

    def add(self, row: Record | None, seconds: float) -> None:
        self.seconds.append(seconds)
        if row is None:
            self.skipped += 1
            return
        if row.error is not None:
            self.failures += 1
            return
        self.by_source[row.source] += 1
        if row.category is not None:
            self.by_category[row.category] += 1
        if NEEDS_REVIEW in row.action.add:
            self.needs_review += 1

    @property
    def messages(self) -> int:
        return len(self.seconds)

    @property
    def classified(self) -> int:
        return self.messages - self.skipped - self.failures

    @property
    def per_minute(self) -> float:
        """Messages per minute of agent time: the pacing figure §8 asks for."""
        total = sum(self.seconds)
        return 60 * self.messages / total if total else 0.0

