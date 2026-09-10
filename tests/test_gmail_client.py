"""Gmail access: the MIME walk, pagination, and the read-only guarantee.

The walk is pure - it takes the payload dict - so the three real tree shapes
are tested directly. Only the API calls need a fake service, and it is a hand
written one rather than a Mock: the chain under test is
`svc.users().messages().list(...).execute()`, and a Mock would happily answer
any chain at all, including a wrong one.

Tree shapes are taken from real mail; see the module docstring of
`app/gmail_client.py` and `docs/BACKLOG.md` -> Body structure.
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import json
from pathlib import Path

import pytest
from googleapiclient.errors import HttpError

from app import gmail_client
from app.gmail_client import (
    Message,
    NotAuthenticated,
    collect_text_parts,
    count,
    fetch,
    header,
    search_ids,
)


def encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def leaf(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"size": len(text), "data": encode(text)}}


def attachment(mime: str, filename: str, size: int) -> dict:
    """An attachment part: a size and an attachmentId, but no data."""
    return {
        "mimeType": mime,
        "filename": filename,
        "body": {"size": size, "attachmentId": "ANGjdJ_xyz"},
    }


def container(mime: str, *parts: dict) -> dict:
    return {"mimeType": mime, "body": {"size": 0}, "parts": list(parts)}


# --- the MIME walk, over the three real shapes ----------------------------


def test_a_bare_text_html_root_is_a_leaf_not_a_container():
    """23% of this mailbox. `payload` IS the text part - there are no `parts`,
    so anything assuming a container finds nothing."""
    found = collect_text_parts(leaf("text/html", "<p>Hello</p>"))
    assert found == {"text/html": "<p>Hello</p>"}


def test_multipart_alternative_yields_both_representations():
    payload = container(
        "multipart/alternative",
        leaf("text/plain", "plain version"),
        leaf("text/html", "<p>html version</p>"),
    )
    assert collect_text_parts(payload) == {
        "text/plain": "plain version",
        "text/html": "<p>html version</p>",
    }


def test_text_is_found_two_levels_down_beside_attachments():
    """The invoice shape: mixed[ alternative[ plain, html ], pdf, png ].

    The text sits in a sibling branch to the attachments, so a walk that stops
    at the first level, or that assumes parts[0] holds text, finds nothing.
    """
    payload = container(
        "multipart/mixed",
        container(
            "multipart/alternative",
            leaf("text/plain", "Invoice INV125960 attached"),
            leaf("text/html", "<p>Invoice INV125960 attached</p>"),
        ),
        attachment("application/pdf", "INV125960.pdf", 80_629),
        attachment("image/png", "logo.png", 56_795),
    )
    found = collect_text_parts(payload)
    assert found["text/plain"] == "Invoice INV125960 attached"
    assert "application/pdf" not in found
    assert "image/png" not in found


def test_attachments_are_never_decoded_so_nothing_binary_enters_the_app():
    """Gmail returns an attachmentId rather than data; retrieving it needs a
    second call this module never makes. A known blind spot on PDF-only bills,
    recorded in DESIGN.md rather than worked around."""
    payload = container(
        "multipart/mixed", attachment("application/pdf", "bill.pdf", 612_973)
    )
    assert collect_text_parts(payload) == {}


def test_the_longest_text_part_wins_among_this_message_s_own_parts():
    """Tie-break when one message emits several text parts of the same type.

    An earlier version of this test claimed the shape below was forwarded mail
    and that taking the nested part was therefore right. That was wrong twice
    over: a forward "as attachment" nests under `message/rfc822`, which is now
    not descended into at all, and the claim had never been measured. It
    occurs in 0 of 250 sampled messages. What remains here is the plain
    tie-break, which is all this rule was ever really doing.
    """
    payload = container(
        "multipart/mixed",
        leaf("text/plain", "See below."),
        container(
            "multipart/alternative",
            leaf("text/plain", "A considerably longer part of the same message."),
        ),
    )
    found = collect_text_parts(payload)
    assert found["text/plain"] == "A considerably longer part of the same message."


def test_an_attached_message_is_appended_after_the_covering_note():
    """A forward "as attachment" reads note first, forwarded content after.

    Order is the whole point: truncation cuts from the start, so the human's
    note survives any body_chars setting and the forwarded content fills what
    is left. A friend forwarding a promotion stays Personal; a friend
    forwarding an invoice still has "invoice" and "due" within reach.
    """
    payload = container(
        "multipart/mixed",
        leaf("text/plain", "FYI you might want to pay this"),
        container(
            "message/rfc822",
            container(
                "multipart/alternative",
                leaf("text/plain", "Invoice 4471. Amount due 87.42 by 15 October."),
                leaf("text/html", "<p>Invoice 4471</p>"),
            ),
        ),
    )
    found = collect_text_parts(payload)

    assert found["text/plain"].startswith("FYI you might want to pay this")
    assert "Amount due 87.42" in found["text/plain"]
    assert gmail_client.FORWARD_SEPARATOR in found["text/plain"]
    # The nested HTML has no outer counterpart, so it stands alone rather than
    # being prefixed by an empty note and a stray divider.
    assert found["text/html"] == "<p>Invoice 4471</p>"


def test_the_covering_note_leads_even_when_the_attachment_comes_first():
    """Two passes rather than one: part order must not decide what truncation
    keeps."""
    payload = container(
        "multipart/mixed",
        container(
            "message/rfc822",
            leaf("text/plain", "The forwarded original."),
        ),
        leaf("text/plain", "FYI"),
    )
    assert collect_text_parts(payload)["text/plain"].startswith("FYI")


def test_a_message_nested_two_deep_is_not_unwrapped_further():
    """A forward inside a forward is cargo within cargo; one level mirrors what
    inline forwarding produces."""
    payload = container(
        "multipart/mixed",
        leaf("text/plain", "note"),
        container(
            "message/rfc822",
            container(
                "multipart/mixed",
                leaf("text/plain", "first forward"),
                container("message/rfc822", leaf("text/plain", "second forward")),
            ),
        ),
    )
    text = collect_text_parts(payload)["text/plain"]
    assert "first forward" in text
    assert "second forward" not in text


def test_a_text_part_carrying_no_data_is_skipped():
    payload = container("multipart/alternative", {"mimeType": "text/plain", "body": {}})
    assert collect_text_parts(payload) == {}


def test_malformed_charset_costs_one_character_not_a_classification():
    payload = {
        "mimeType": "text/plain",
        "body": {"data": base64.urlsafe_b64encode(b"caf\xff").decode()},
    }
    assert collect_text_parts(payload)["text/plain"].startswith("caf")


# --- headers and dates ----------------------------------------------------


def test_header_lookup_is_case_insensitive():
    payload = {"headers": [{"name": "FROM", "value": "a@b.com"}]}
    assert header(payload, "From") == "a@b.com"


def test_a_missing_header_is_empty_not_an_error():
    assert header({"headers": []}, "Subject") == ""
    assert header({}, "Subject") == ""


def test_internal_date_converts_epoch_milliseconds_to_utc():
    """internalDate, not the Date: header - the header is the sender's clock
    and is occasionally years wrong, and the eval frame is date-scoped."""
    when = gmail_client._internal_date({"internalDate": "1757462400000"})
    assert when.tzinfo is dt.UTC
    assert when == dt.datetime.fromtimestamp(1757462400, dt.UTC)


def test_a_missing_internal_date_does_not_raise():
    assert gmail_client._internal_date({}) == dt.datetime.fromtimestamp(0, dt.UTC)


# --- the API calls, against a hand-written fake ---------------------------


class FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeMessages:
    def __init__(self, pages=None, message=None):
        self._pages = list(pages or [])
        self._message = message
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return FakeExecutable(self._pages.pop(0) if self._pages else {})

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return FakeExecutable(self._message)


class FakeService:
    def __init__(self, messages: FakeMessages):
        self._messages = messages

    def users(self):
        return self

    def messages(self):
        return self._messages


def test_search_ids_paginates_to_the_end():
    messages = FakeMessages(
        pages=[
            {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "t1"},
            {"messages": [{"id": "c"}]},
        ]
    )
    assert search_ids(FakeService(messages), "in:inbox") == ["a", "b", "c"]
    assert messages.list_calls[1]["pageToken"] == "t1"


def test_search_ids_stops_at_the_limit_without_fetching_another_page():
    messages = FakeMessages(
        pages=[{"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "t1"}]
    )
    assert search_ids(FakeService(messages), "q", limit=2) == ["a", "b"]
    assert len(messages.list_calls) == 1


def test_search_ids_never_requests_more_than_the_limit():
    messages = FakeMessages(pages=[{"messages": [{"id": "a"}]}])
    search_ids(FakeService(messages), "q", limit=3)
    assert messages.list_calls[0]["maxResults"] == 3


def test_count_is_exact_pagination_not_an_estimate():
    """resultSizeEstimate drifts by hundreds at this scale, and the stratum
    weights are N_h/n_h - an estimated numerator would put a systematic error
    straight into the headline accuracy figure."""
    messages = FakeMessages(
        pages=[
            {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "t1",
             "resultSizeEstimate": 900},
            {"messages": [{"id": "c"}], "resultSizeEstimate": 900},
        ]
    )
    assert count(FakeService(messages), "q") == 3


def test_fetch_assembles_a_message_with_both_text_parts_and_no_preference():
    payload = container(
        "multipart/alternative",
        leaf("text/plain", "plain body"),
        leaf("text/html", "<p>html body</p>"),
    )
    payload["headers"] = [
        {"name": "From", "value": "billing@octopus.energy"},
        {"name": "Subject", "value": "Your bill"},
    ]
    messages = FakeMessages(
        message={
            "id": "m1",
            "threadId": "t1",
            "labelIds": ["INBOX", "UNREAD"],
            "internalDate": "1757462400000",
            "payload": payload,
        }
    )
    message = fetch(FakeService(messages), "m1")

    assert isinstance(message, Message)
    assert message.sender == "billing@octopus.energy"
    assert message.subject == "Your bill"
    assert message.text_plain == "plain body"
    assert message.text_html == "<p>html body</p>"
    assert message.label_ids == ("INBOX", "UNREAD")
    assert messages.get_calls[0]["format"] == "full"


def test_service_without_credentials_raises_rather_than_returning_none(monkeypatch):
    """Distinct from a transient error: an expired refresh token fails
    identically on every retry and needs a human to re-consent."""
    monkeypatch.setattr(gmail_client.auth, "load_credentials", lambda: None)
    with pytest.raises(NotAuthenticated):
        gmail_client.service()


# --- transient failures ---------------------------------------------------


class FakeHttpResponse:
    """The two attributes HttpError reads.

    A stub rather than httplib2.Response: httplib2 is a transitive dependency
    of google-api-python-client, not one this project declares, and reaching
    through to it in tests would make an undeclared package load-bearing.
    """

    def __init__(self, status: int):
        self.status = status
        self.reason = ""


def http_error(status: int, reason: str = "") -> HttpError:
    """An HttpError shaped like the ones Gmail actually returns."""
    content = json.dumps(
        {"error": {"code": status, "message": reason, "errors": [{"reason": reason}]}}
    ).encode()
    return HttpError(FakeHttpResponse(status), content)


class FlakyRequest:
    """Fails with `error` for the first `failures` calls, then succeeds."""

    def __init__(self, error: HttpError, failures: int, result=None):
        self._error = error
        self._remaining = failures
        self._result = result or {"messages": []}
        self.attempts = 0

    def execute(self):
        self.attempts += 1
        if self._remaining:
            self._remaining -= 1
            raise self._error
        return self._result


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Backoff sleeps are real seconds; the tests should not spend them."""
    monkeypatch.setattr(gmail_client.time, "sleep", lambda _: None)


def test_a_rate_limited_request_is_retried_until_it_succeeds():
    """Measured, not hypothetical: a 400-message sweep died at 150 with this.

    `messages.get` costs 5 quota units, so a 200-message labelling session
    trips the per-minute limit during normal operation.
    """
    request = FlakyRequest(http_error(403, "rateLimitExceeded"), failures=3)
    assert gmail_client._execute(request) == {"messages": []}
    assert request.attempts == 4


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_statuses_are_retried(status):
    request = FlakyRequest(http_error(status, "backendError"), failures=1)
    gmail_client._execute(request)
    assert request.attempts == 2


def test_a_permission_403_is_never_retried():
    """The one that matters for this project.

    Gmail returns 403 both for "too fast" and for "your token lacks that
    permission" - and the second is exactly what the gmail.readonly scope
    produces if a write ever slips in. Retrying it would burn a minute and then
    report a rate-limit problem that was really a scope violation.
    """
    request = FlakyRequest(http_error(403, "insufficientPermissions"), failures=1)
    with pytest.raises(HttpError):
        gmail_client._execute(request)
    assert request.attempts == 1


def test_a_404_is_never_retried():
    request = FlakyRequest(http_error(404, "notFound"), failures=1)
    with pytest.raises(HttpError):
        gmail_client._execute(request)
    assert request.attempts == 1


def test_throttle_paces_a_sweep_and_is_opt_in(monkeypatch):
    """Backoff reacts after the quota is spent; only pacing avoids spending it.

    Opt-in because the labelling CLI fetches one message per human decision,
    roughly every thirty seconds - making that sleep would be pure loss.
    """
    slept: list[float] = []
    monkeypatch.setattr(gmail_client.time, "sleep", slept.append)

    wait = gmail_client.throttle(0.4)
    wait()
    wait()
    gmail_client.throttle(0)()  # zero means no call at all, not sleep(0)

    assert slept == [0.4, 0.4]


def test_retries_are_bounded_and_the_error_survives():
    request = FlakyRequest(http_error(429, "rateLimitExceeded"), failures=99)
    with pytest.raises(HttpError):
        gmail_client._execute(request)
    assert request.attempts == gmail_client.MAX_ATTEMPTS


def test_backoff_doubles_and_is_capped(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(gmail_client.time, "sleep", slept.append)
    monkeypatch.setattr(gmail_client.random, "uniform", lambda _, high: high)

    request = FlakyRequest(http_error(429, "rateLimitExceeded"), failures=99)
    with pytest.raises(HttpError):
        gmail_client._execute(request)

    assert slept == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]
    assert max(slept) <= gmail_client.BACKOFF_CAP


def test_jitter_is_equal_not_full_so_retries_span_the_window(monkeypatch):
    """The distinction that made backoff useless the first time.

    Full jitter draws from [0, delay), halving the expected wait - six such
    attempts averaged ~31s and a real sweep exhausted them without the quota
    window reopening. Equal jitter keeps half the delay as a floor, so the
    worst case is never near zero, while the random half still stops
    concurrent callers retrying in lockstep.
    """
    slept: list[float] = []
    monkeypatch.setattr(gmail_client.time, "sleep", slept.append)
    monkeypatch.setattr(gmail_client.random, "uniform", lambda low, _: low)

    request = FlakyRequest(http_error(429, "rateLimitExceeded"), failures=99)
    with pytest.raises(HttpError):
        gmail_client._execute(request)

    # Even with the jitter drawing its minimum, every wait is half the delay.
    assert slept == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
    assert sum(slept) > 30


# --- the read-only guarantee ---------------------------------------------


def test_gmail_client_contains_no_write_call():
    """CLAUDE.md requires Phases 0-2 to be INCAPABLE of modifying the inbox,
    not merely unlikely to. The gmail.readonly token is the backstop; this is
    the guarantee. Phase 3 adds messages.modify and updates this deliberately.
    """
    forbidden = {
        "modify", "batchModify", "trash", "untrash", "delete", "batchDelete",
        "insert", "send", "create", "update", "patch",
    }
    source = Path(gmail_client.__file__).read_text()
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & forbidden, f"write calls: {sorted(called & forbidden)}"
