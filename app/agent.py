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

The only module in `app/` allowed to call `gmail_client.modify_labels`,
which `tests/test_write_guard.py` enforces. It joined after the undo
rehearsal passed on 2026-11-03 (A1).
"""

from __future__ import annotations

import time
from collections.abc import Collection, Mapping

from app import classifier, decision, gmail_client, logbook, prefilter
from app.categories import AGENT_LABELS, INBOX, STOP_LABELS
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

# Every label a decision can name. The map passed in must cover all of them.
REQUIRED_LABELS: frozenset[str] = AGENT_LABELS | {INBOX}


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
