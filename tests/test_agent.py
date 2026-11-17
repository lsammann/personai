"""The agent: one message through fetch, route, decide, write and log.

Gmail is faked at the service level (`tests/fakes.py`), so the real
`gmail_client.fetch` parsing and the real `modify_labels` checks run - a test
where the agent passed label names instead of ids would fail on
`modify_labels`' own refusal (step 7 decision Q9). The classifier is
monkeypatched; model quality is the eval set's question, not this file's.
"""

from __future__ import annotations

import base64

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from app import agent, classifier, logbook
from app.agent import classify_and_label
from app.categories import (
    ERROR,
    INBOX,
    NEEDS_REVIEW,
    PROCESSED,
    REQUIRED_LABELS,
    Category,
    argmax,
    label_for,
)
from app.classifier import Interpretation, NoCategoryLetter, OllamaError
from app.config import Config
from app.gmail_client import Forbidden, NotAuthenticated
from tests.fakes import FakeMessages, FakeService, FlakyRequest, http_error

# name -> id, the shape label_ids() returns. INBOX is its own id, as in Gmail.
IDS = {
    name: (name if name == INBOX else f"Label_{n}")
    for n, name in enumerate(sorted(REQUIRED_LABELS), start=2)
}
RECEIPTS = label_for(Category.RECEIPTS)
TO_ACTION = label_for(Category.TO_ACTION)
BOOKINGS = label_for(Category.BOOKINGS)
PERSONAL = label_for(Category.PERSONAL)
PROMOTIONS = label_for(Category.PROMOTIONS)

SHOP = "Shop <orders@shop.example>"
ALLOWLIST = frozenset({"deals.example"})


def ids(*names: str) -> list[str]:
    return sorted(IDS[name] for name in names)


def payload(subject="Your order", sender=SHOP, labels=(INBOX, "UNREAD")) -> dict:
    """A messages.get response, as Gmail shapes it."""
    body = base64.urlsafe_b64encode(b"Thanks for your order.").decode()
    return {
        "id": "m1",
        "threadId": "t1",
        "labelIds": list(labels),
        "internalDate": "1762000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}],
            "body": {"data": body},
        },
    }


def distribution(winner: Category, p: float, **others: float) -> dict[Category, float]:
    """`winner` at p, any named others as given, the rest sharing what is left."""
    named = {Category[k]: v for k, v in others.items()}
    rest = (1.0 - p - sum(named.values())) / (len(Category) - 1 - len(named))
    return {c: p if c is winner else named.get(c, rest) for c in Category}


def interpretation(dist, missing=()) -> Interpretation:
    category = argmax(dist)
    return Interpretation(
        distribution=dist, category=category, confidence=dist[category],
        missing_letters=tuple(missing), retained_mass=0.97,
    )


class FakeClassify:
    """Answers each call from a script: an Interpretation, or an exception."""

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    """Every sleep, recorded instead of waited for. `time` is one module, so
    this also catches gmail_client's backoff."""
    waits: list[float] = []
    monkeypatch.setattr(agent.time, "sleep", waits.append)
    return waits


def run(monkeypatch, *, message=None, classify=None, dry_run=False,
        get_request=None, modify_request=None, ids_by_name=IDS):
    """One classify_and_label call. Returns (row, messages fake, classify fake)."""
    fake = classify or FakeClassify(interpretation(distribution(Category.RECEIPTS, 0.95)))
    monkeypatch.setattr(classifier, "classify", fake)
    messages = FakeMessages(
        message=message or payload(), get_request=get_request, modify_request=modify_request,
    )
    row = classify_and_label(
        FakeService(messages), "m1", cfg=Config(), ids_by_name=ids_by_name,
        allowlist=ALLOWLIST, run_id="20261104T100000-abc123", run_kind="manual",
        dry_run=dry_run,
    )
    return row, messages, fake


def logged() -> list[logbook.Record]:
    records, skipped = logbook.read_all()
    assert skipped == 0
    return records


# --- one test per row of the decision table, live and dry -------------------

RECEIPTS_95 = distribution(Category.RECEIPTS, 0.95)

DECISION_ROWS = {
    # name: (subject, sender, distribution, source, add, remove)
    "confident archive": ("Your order", SHOP, RECEIPTS_95, "model", (PROCESSED, RECEIPTS), (INBOX,)),
    "confident To Action": ("Bill due", SHOP, distribution(Category.TO_ACTION, 0.95),
                            "model", (PROCESSED, TO_ACTION), ()),
    "confident Bookings": ("Your stay", SHOP, distribution(Category.BOOKINGS, 0.95),
                           "model", (BOOKINGS, PROCESSED), ()),
    "needs review": ("Hmm", SHOP, distribution(Category.UPDATES, 0.55),
                     "model", (NEEDS_REVIEW, PROCESSED), ()),
    # Filed as Receipts, kept visible by p(To Action) at the floor.
    "To Action override": ("Order", SHOP, distribution(Category.RECEIPTS, 0.80, TO_ACTION=0.15),
                           "model", (PROCESSED, RECEIPTS), ()),
    "prefilter hit": ("50% off", "Deals <news@mail.deals.example>", RECEIPTS_95,
                      "prefilter", (PROCESSED, PROMOTIONS), (INBOX,)),
    "reply hit": ("Re: dinner", "Sam <sam@friend.example>", RECEIPTS_95,
                  "reply", (PERSONAL, PROCESSED), ()),
}


@pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry"])
@pytest.mark.parametrize("case", DECISION_ROWS, ids=list(DECISION_ROWS))
def test_each_decision_row_reaches_the_right_change(monkeypatch, case, dry_run):
    subject, sender, dist, source, add, remove = DECISION_ROWS[case]
    row, messages, _ = run(
        monkeypatch, message=payload(subject=subject, sender=sender),
        classify=FakeClassify(interpretation(dist)), dry_run=dry_run,
    )

    assert row.source == source
    assert sorted(row.action.add) == sorted(add)
    assert sorted(row.action.remove) == sorted(remove)
    assert row.error is None
    assert row.prior_labels == (INBOX, "UNREAD")
    if dry_run:
        assert messages.modify_calls == []
        assert row.labels_after is None and row.dry_run
    else:
        # One call, both sides, as ids.
        assert messages.modify_calls == [{
            "userId": "me", "id": "m1",
            "body": {"addLabelIds": ids(*add), "removeLabelIds": ids(*remove)},
        }]
        assert row.labels_after == ("Label_2", "Label_6")  # the fake's response
    assert logged() == [row]


def test_a_dry_run_row_equals_the_live_row_but_for_the_write(monkeypatch):
    live, _, _ = run(monkeypatch)
    dry, _, _ = run(monkeypatch, dry_run=True)
    differs = {"timestamp", "dry_run", "labels_after"}
    assert live.model_dump(exclude=differs) == dry.model_dump(exclude=differs)


def test_the_override_is_what_keeps_inbox(monkeypatch):
    """Without the floor the same argmax archives; with it, INBOX stays."""
    row, _, _ = run(monkeypatch, classify=FakeClassify(
        interpretation(distribution(Category.RECEIPTS, 0.80, TO_ACTION=0.15))))
    assert row.category is Category.RECEIPTS
    assert INBOX not in row.action.remove


# --- routing ------------------------------------------------------------------


def test_reply_beats_the_allowlist(monkeypatch):
    """A `Re:` from an allowlisted promotional domain stays visible."""
    row, _, classify = run(
        monkeypatch, message=payload(subject="Re: your question", sender="Help <help@deals.example>"),
    )
    assert row.source == "reply"
    assert INBOX not in row.action.remove
    assert classify.calls == []


@pytest.mark.parametrize("subject, sender", [
    ("Re: dinner", "Sam <sam@friend.example>"),
    ("50% off", "Deals <news@deals.example>"),
])
def test_rule_rows_record_only_what_was_used(monkeypatch, subject, sender):
    """Q10: no model was called and no threshold applied, so neither is
    recorded - a reply row carrying 0.8 would read as if one had been."""
    row, _, classify = run(monkeypatch, message=payload(subject=subject, sender=sender))
    assert classify.calls == []
    assert row.confidence is None and row.category is not None
    assert row.model is None and row.prompt_id is None and row.body_chars is None
    assert row.confidence_threshold is None and row.to_action_floor is None
    assert row.distribution == {} and row.retained_mass is None


def test_a_model_row_records_its_parameters(monkeypatch):
    row, _, classify = run(monkeypatch)
    cfg = Config()
    assert row.model == cfg.ollama_model
    assert row.prompt_id == classifier.DEFAULT_PROMPT_ID
    assert row.body_chars == cfg.body_chars
    assert row.confidence_threshold == cfg.confidence_threshold
    assert row.to_action_floor == cfg.to_action_floor
    assert row.distribution == RECEIPTS_95 and row.retained_mass == 0.97
    assert row.category is Category.RECEIPTS and row.confidence == 0.95
    call = classify.calls[0]
    assert call["args"][:2] == (SHOP, "Your order")
    assert call["model"] == cfg.ollama_model and call["body_chars"] == cfg.body_chars
    assert call["prompt_id"] == classifier.DEFAULT_PROMPT_ID
    assert call["timeout"] == agent.CLASSIFY_TIMEOUT


def test_missing_letters_are_noted(monkeypatch):
    row, _, _ = run(monkeypatch, classify=FakeClassify(interpretation(RECEIPTS_95, missing=("C", "F"))))
    assert row.note == "missing letters: C, F"


# --- the effective change -------------------------------------------------------


def test_an_already_archived_message_gets_no_inbox_remove(monkeypatch):
    """The A3 case: undo must never be told INBOX was removed when it was
    not, or it would put archived mail back in the inbox."""
    row, messages, _ = run(monkeypatch, message=payload(labels=("UNREAD",)))
    assert row.action.remove == ()
    assert messages.modify_calls[0]["body"]["removeLabelIds"] == []


def test_labels_the_agent_does_not_own_are_ignored(monkeypatch):
    row, messages, _ = run(monkeypatch, message=payload(labels=(INBOX, "STARRED", "Label_99")))
    assert sorted(row.action.add) == [PROCESSED, RECEIPTS]
    body = messages.modify_calls[0]["body"]
    assert "STARRED" not in body["removeLabelIds"] + body["addLabelIds"]


# --- terminal state (Q4) ----------------------------------------------------------


@pytest.mark.parametrize("label", [PROCESSED, ERROR])
def test_a_message_in_a_terminal_state_is_left_alone(monkeypatch, label):
    """No classification, no write, no row: labelling it would add a second
    category, or put Processed beside Error."""
    row, messages, classify = run(monkeypatch, message=payload(labels=(INBOX, IDS[label])))
    assert row is None
    assert classify.calls == [] and messages.modify_calls == []
    assert logged() == []


# --- failures: no labels, a row with an error ---------------------------------------


def test_no_category_letter_is_not_retried(monkeypatch, slept):
    row, messages, classify = run(monkeypatch, classify=FakeClassify(NoCategoryLetter("prose")))
    assert len(classify.calls) == 1 and slept == []
    assert row.error.startswith("classify: NoCategoryLetter")
    assert row.action == logbook.Action() and messages.modify_calls == []
    assert row.source == "model" and row.category is None
    assert logged() == [row]


def test_ollama_error_is_tried_five_times_with_the_agreed_delays(monkeypatch, slept):
    row, messages, classify = run(monkeypatch, classify=FakeClassify(OllamaError("refused")))
    assert len(classify.calls) == 5
    assert slept == [2.0, 4.0, 8.0, 16.0]
    assert row.error.startswith("classify: OllamaError")
    assert row.note == "ollama retries: 4"
    assert messages.modify_calls == [] and row.action == logbook.Action()


def test_ollama_recovering_mid_retry_labels_the_message(monkeypatch, slept):
    classify = FakeClassify(OllamaError("a"), OllamaError("b"), interpretation(RECEIPTS_95))
    row, messages, _ = run(monkeypatch, classify=classify)
    assert len(classify.calls) == 3 and slept == [2.0, 4.0]
    assert row.error is None and len(messages.modify_calls) == 1
    assert row.note == "ollama retries: 2"


def test_a_fetch_failure_is_a_row_with_no_source(monkeypatch):
    """Q3: routing was never reached, so no source would be true."""
    row, messages, classify = run(
        monkeypatch, get_request=FlakyRequest(http_error(404, "notFound"), failures=99),
    )
    assert row.error.startswith("fetch: GmailApiError")
    assert row.source is None and row.prior_labels is None
    assert row.sender == "" and row.subject == ""
    assert classify.calls == [] and messages.modify_calls == []
    assert logged() == [row]


def test_a_modify_failure_is_a_failure_row_noting_the_intended_change(monkeypatch, slept):
    """Q6: probably nothing was applied. If it was, the sweep finds an
    orphan, and the note says what it most likely received."""
    request = FlakyRequest(http_error(500, "backendError"), failures=99)
    row, _, _ = run(monkeypatch, modify_request=request)
    assert request.attempts > 1  # _execute retried before giving up
    assert row.error.startswith("modify: GmailApiError")
    assert row.action == logbook.Action() and row.labels_after is None
    assert row.note == f"intended: Receipts; +{PROCESSED} +{RECEIPTS} -INBOX"
    # A row with an error is never also a classification.
    assert row.category is None and row.distribution == {}
    assert row.prior_labels == (INBOX, "UNREAD") and row.source == "model"
    assert logged() == [row]


# --- halts: propagated, no row ----------------------------------------------------


@pytest.mark.parametrize("error, where", [
    (NotAuthenticated("reconnect"), "get"),
    (RefreshError("invalid_grant"), "get"),
    (RefreshError("invalid_grant"), "modify"),
    (http_error(403, "insufficientPermissions"), "get"),
    (http_error(403, "insufficientPermissions"), "modify"),
    (TimeoutError("timed out"), "get"),   # transport errors are not wrapped
])
def test_mailbox_wide_errors_halt_the_run(monkeypatch, error, where):
    request = FlakyRequest(error, failures=99)
    # A 403 surfaces as gmail_client's Forbidden; the rest pass through as raised.
    expected = Forbidden if isinstance(error, HttpError) else type(error)
    with pytest.raises(expected):
        run(monkeypatch, **{f"{where}_request": request})
    assert logged() == []


def test_a_bug_is_not_turned_into_a_failure_row(monkeypatch):
    """The test `except Exception` would fail (Q2)."""
    with pytest.raises(KeyError):
        run(monkeypatch, classify=FakeClassify(KeyError("bug")))
    assert logged() == []


# --- order and preconditions ------------------------------------------------------


class AssertsLogEmpty:
    """A modify that checks nothing has been logged yet when it runs."""

    def execute(self):
        assert logged() == [], "the row was written before the modify"
        return {"labelIds": ["Label_2"]}


def test_the_row_is_written_after_the_modify(monkeypatch):
    row, _, _ = run(monkeypatch, modify_request=AssertsLogEmpty())
    assert logged() == [row]


def test_a_map_missing_a_label_is_refused_before_any_call():
    short = {name: i for name, i in IDS.items() if name != NEEDS_REVIEW}
    messages = FakeMessages(message=payload())
    with pytest.raises(ValueError, match="Needs Review"):
        classify_and_label(
            FakeService(messages), "m1", cfg=Config(), ids_by_name=short,
            allowlist=ALLOWLIST, run_id="r", run_kind="manual", dry_run=False,
        )
    assert messages.get_calls == [] and logged() == []


# --- the run-level helpers (step 8) -------------------------------------------

def test_the_queue_is_received_mail_not_yet_terminal():
    from app.categories import RECEIVED_ONLY, UNPROCESSED_TERMS

    assert agent.queue_query() == f"{RECEIVED_ONLY} {UNPROCESSED_TERMS}"


def test_extra_terms_are_parenthesised_so_an_or_cannot_escape():
    """The grouping is explicit rather than left to Gmail's precedence
    between OR and the implicit AND, so whatever `--query` says is ANDed
    with the base terms as one unit."""
    query = agent.queue_query("newer_than:1d OR from:x@y.example")
    assert query.endswith(" (newer_than:1d OR from:x@y.example)")
    assert query.startswith(agent.queue_query())


@pytest.mark.parametrize("extra", [None, "", "   "])
def test_no_extra_terms_means_no_parentheses(extra):
    assert "(" not in agent.queue_query(extra)


@pytest.mark.parametrize("limit, live", [(1, True), (25, True), (300, False)])
def test_limits_within_the_rule_are_accepted(limit, live):
    agent.check_limit(limit, live=live)


@pytest.mark.parametrize("limit, live, message", [
    (0, False, "at least 1"),
    (-5, True, "at least 1"),
    (26, True, "at most 25"),
])
def test_limits_outside_the_rule_are_refused_saying_why(limit, live, message):
    with pytest.raises(ValueError, match=message):
        agent.check_limit(limit, live=live)


def test_the_live_cap_is_25():
    """§8. Changing it is Phase 4's deliberate diff, not a side effect."""
    assert agent.LIVE_LIMIT_CAP == 25


def test_preflight_classifies_the_fixed_email_with_the_run_timeout(monkeypatch):
    fake = FakeClassify(interpretation(RECEIPTS_95))
    monkeypatch.setattr(classifier, "classify", fake)
    seconds = agent.preflight(Config())
    assert seconds >= 0
    [call] = fake.calls
    assert call["args"] == agent.PREFLIGHT_EMAIL
    assert call["timeout"] == agent.CLASSIFY_TIMEOUT
    assert call["prompt_id"] == classifier.DEFAULT_PROMPT_ID


@pytest.mark.parametrize("error", [OllamaError("down"), NoCategoryLetter("no letter")])
def test_a_preflight_failure_propagates_untried_and_logs_nothing(monkeypatch, slept, error):
    """A run with the model down stops here: one try, no sleep, no row."""
    fake = FakeClassify(error)
    monkeypatch.setattr(classifier, "classify", fake)
    with pytest.raises(type(error)):
        agent.preflight(Config())
    assert len(fake.calls) == 1
    assert slept == []
    assert logged() == []


def test_the_tally_counts_each_kind_of_outcome(monkeypatch):
    tally = agent.RunTally()
    model_row, _, _ = run(monkeypatch)
    review_row, _, _ = run(monkeypatch, classify=FakeClassify(
        interpretation(distribution(Category.UPDATES, 0.55))))
    reply_row, _, _ = run(monkeypatch, message=payload(subject="Re: dinner"))
    failed_row, _, _ = run(monkeypatch, classify=FakeClassify(NoCategoryLetter("x")))

    for row, seconds in [(model_row, 2.0), (review_row, 3.0), (reply_row, 0.5),
                         (failed_row, 2.5), (None, 2.0)]:
        tally.add(row, seconds)

    assert (tally.messages, tally.classified, tally.skipped, tally.failures) == (5, 3, 1, 1)
    assert tally.by_source == {"model": 2, "reply": 1}
    assert tally.by_category == {Category.RECEIPTS: 1, Category.UPDATES: 1, Category.PERSONAL: 1}
    assert tally.needs_review == 1
    assert tally.per_minute == pytest.approx(60 * 5 / 10.0)


def test_an_empty_tally_has_no_rate():
    assert agent.RunTally().per_minute == 0.0
