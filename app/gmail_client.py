"""Gmail API access: search, count, fetch, and the MIME walk.

Transport only. This module knows the shape of Gmail's payload JSON and
nothing about what the model should read - choosing between the plain and HTML
parts is model-input policy and lives in `message_body`. `fetch()` therefore
returns both text parts, raw and decoded, and expresses no preference.

**Read-only until Phase 3.** `CLAUDE.md` requires Phases 0-2 to be incapable
of modifying the inbox rather than merely unlikely to, and the token carries
`gmail.readonly`, so a write here would fail at the API. That is a backstop,
not the guarantee: a test parses this module and fails if a modify, trash or
delete call appears in it, the same way a test guards the purity of
`decision`. Phase 3 adds the single atomic `messages.modify` call and updates
that test deliberately.

The MIME walk is the substantial part. An email is headers plus one blob of
text at the wire level; MIME is the convention that lets one message carry
several representations of itself plus attachments, by nesting labelled parts.
Gmail hands that nesting back as recursive JSON, and real mail arrives at
three different depths:

    text/html                        a bare leaf - 23% of this mailbox
    multipart/alternative            same message, two formats
      text/plain
      text/html
    multipart/mixed                  message plus attachments
      multipart/alternative
        text/plain
        text/html
      application/pdf                no data, only an attachmentId
    multipart/mixed                  a forward "as attachment"
      text/plain                     the covering note - THIS is the message
      message/rfc822                 a whole nested message; not descended into
        multipart/alternative
          text/plain
          text/html

Containers carry no text - every one reports `size: 0` and no `body.data` - so
reading `payload.body.data` directly finds nothing for two of those three
shapes, and the depth is not fixed, which is why this is recursion rather than
looking at `parts[0]` and `parts[1]`.
"""

from __future__ import annotations

import base64
import datetime as dt
import random
import time
from dataclasses import dataclass

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app import auth

# The two MIME types worth decoding. Everything else - images, PDFs,
# calendar invites - is skipped, which is also why no attachment is ever
# downloaded: Gmail returns an `attachmentId` rather than data for those, and
# retrieving one needs a second API call this module never makes. The cost is
# a known blind spot on PDF-only bills; see DESIGN.md -> Classification logic.
TEXT_TYPES = ("text/plain", "text/html")

# A forward "as attachment" nests an entire message - headers, its own
# multipart/alternative, its own text parts - under this type.
#
# Its text is appended AFTER the covering note, never merged into it and never
# allowed to lead. Two reasons, and the second is the one that decides it:
#
#   Order matters because truncation cuts from the start. The human's note
#   leads, so "FYI you might be interested" survives any body_chars setting
#   and the forwarded content fills whatever budget is left. A friend
#   forwarding a promotion stays Personal; a friend forwarding an invoice
#   still has "invoice" and "due" in reach.
#
#   Consistency with inline forwarding. Gmail's default Forward button inlines
#   the original into the SAME text part, note first - which is 100% of the
#   forwards actually present in this mailbox (0 of 250 sampled messages carry
#   message/rfc822 at all). Reading only the note here would make one user
#   action classify differently depending on which forward button was pressed.
NESTED_MESSAGE = "message/rfc822"

# Mirrors the divider Gmail writes when forwarding inline, so both forwarding
# styles reach the model looking the same.
FORWARD_SEPARATOR = "\n\n---------- Forwarded message ----------\n"

# Statuses worth retrying. 403 is included but conditionally - see `_retryable`.
RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})

# The reasons that make a 403 a rate limit rather than a permission problem.
RATE_LIMIT_REASONS = ("ratelimitexceeded", "userratelimitexceeded", "quotaexceeded")

# Backoff bounds. EQUAL jitter, not full jitter: full jitter draws from
# [0, delay), which halves the expected wait, and a measured 300-message sweep
# exhausted six such attempts (~31s expected) without the quota window
# reopening. Equal jitter keeps half the delay as a floor, so the retries
# actually span the window they are meant to outlast, while the random half
# still stops concurrent callers retrying in lockstep.
MAX_ATTEMPTS = 7
BACKOFF_CAP = 60.0

# Retries served since import. Not logging from a library module - a bulk
# caller reports this so a slow run is visibly throttling rather than hung.
retry_count = 0


class GmailError(Exception):
    """Base for everything that means "no usable Gmail access"."""


class NotAuthenticated(GmailError):
    """No stored credentials, or the refresh token has expired.

    Distinct from a transient API error because retrying cannot help: an
    expired refresh token fails identically every time and needs a human to
    re-consent.
    """


@dataclass(frozen=True)
class Message:
    """One fetched message. Both text parts, no preference expressed.

    `text_plain` and `text_html` are decoded but otherwise untouched - the
    HTML is still markup. `message_body.select_body()` turns the pair into the
    one string the model reads.
    """

    id: str
    thread_id: str
    sender: str
    subject: str
    internal_date: dt.datetime
    text_plain: str
    text_html: str
    label_ids: tuple[str, ...]


def service(credentials: Credentials | None = None):
    """An authenticated Gmail service, or `NotAuthenticated`.

    The return is googleapiclient's dynamically-built `Resource`, which has no
    usable static type - it is assembled from the discovery document at call
    time - so it is left unannotated rather than given a fictional one.

    `cache_discovery=False` because the default file cache warns noisily under
    oauth2client-less installs and buys nothing for a handful of calls.
    """
    if credentials is None:
        credentials = auth.load_credentials()
    if credentials is None:
        raise NotAuthenticated(
            "No usable Gmail credentials. Run the server and visit /auth/start."
        )
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def throttle(seconds: float):
    """A pacing callback for bulk sweeps: call it once per message.

    Measured behaviour, not a guess. Two independent sweeps died on
    `rateLimitExceeded` at the same point - between the 150th and 175th
    `messages.get` - and seven backoff attempts spanning a minute did not
    reopen the window, which puts the ceiling well outside a per-minute burst
    limit and makes reacting to it useless. Pacing under it is the only thing
    that works.

    Deliberately a caller's choice rather than baked into `fetch`: the
    labelling CLI fetches one message every thirty seconds while a human
    reads, and making it sleep would be pure loss. Only sweeps need this.
    """

    def wait() -> None:
        if seconds > 0:
            time.sleep(seconds)

    return wait


def _retryable(error: HttpError) -> bool:
    """Is this worth trying again, or is it permanent?

    The subtlety is 403. Gmail returns it both for "you are going too fast"
    (retry) and for "your token does not carry that permission" (never retry) -
    and the second is exactly what the read-only scope produces if a write ever
    slips in. Retrying that would burn a minute and then report a rate-limit
    problem that was really a scope violation, which is precisely the error this
    project most wants to see clearly.
    """
    status = getattr(error.resp, "status", None)
    if status not in RETRY_STATUSES:
        return False
    if status != 403:
        return True
    return any(reason in str(error).lower() for reason in RATE_LIMIT_REASONS)


def _execute(request):
    """Run one API request, backing off on transient failures.

    Retries live here rather than in the callers, which is a reversal of the
    first design. The argument for pushing them out was that both callers are
    resumable, so an abort costs one message - true, but it misses that a quota
    bounce is *expected* rather than exceptional: `messages.get` costs 5 quota
    units and a 200-message labelling session trips the per-minute limit on its
    own. Measured, not predicted - a 400-message sweep died at message 150 with
    `rateLimitExceeded`. A caller that has to be restarted repeatedly through
    normal operation is not resumable in any useful sense.

    Backoff is the safety net, not the strategy. A sustained sweep should pace
    itself so the limit is never reached - see `throttle` - because once the
    quota window is exhausted, waiting inside it is strictly worse than not
    having filled it.
    """
    global retry_count
    for attempt in range(MAX_ATTEMPTS):
        try:
            return request.execute()
        except HttpError as error:
            if attempt == MAX_ATTEMPTS - 1 or not _retryable(error):
                raise
            retry_count += 1
            delay = min(2.0**attempt, BACKOFF_CAP)
            # Equal jitter: half the delay guaranteed, half randomised.
            time.sleep(delay / 2 + random.uniform(0, delay / 2))
    raise AssertionError("unreachable: the final attempt either returns or raises")


def search_ids(svc, query: str, limit: int | None = None) -> list[str]:
    """Message ids matching a Gmail query, paginating until `limit` or the end.

    Ids only. Enumerating the whole eval frame this way is thirteen calls and
    a few seconds, where fetching those messages would be thousands.
    """
    ids: list[str] = []
    page_token = None
    while True:
        page_size = 500 if limit is None else min(500, limit - len(ids))
        response = _execute(
            svc.users()
            .messages()
            .list(userId="me", q=query, maxResults=page_size, pageToken=page_token)
        )
        ids += [message["id"] for message in response.get("messages", [])]
        page_token = response.get("nextPageToken")
        if not page_token or (limit is not None and len(ids) >= limit):
            break
    return ids if limit is None else ids[:limit]


def count(svc, query: str) -> int:
    """Exact count by pagination.

    Not `resultSizeEstimate`, which is an estimate and drifts by hundreds on
    queries this size. The stratum weights are `N_h / n_h`, so an estimated
    numerator would put a quiet systematic error straight into the headline
    accuracy figure.
    """
    return len(search_ids(svc, query))


def fetch(svc, message_id: str) -> Message:
    """One message, with both text parts decoded.

    Transient failures are retried with backoff inside `_execute`; anything
    that survives that propagates. Callers stay resumable regardless - the
    labelling CLI appends after every decision - but resumability is the
    recovery path for a genuine failure, not a substitute for handling a
    rate limit that normal operation trips on its own.
    """
    payload_response = _execute(
        svc.users().messages().get(userId="me", id=message_id, format="full")
    )
    payload = payload_response.get("payload", {})
    parts = collect_text_parts(payload)
    return Message(
        id=payload_response.get("id", message_id),
        thread_id=payload_response.get("threadId", ""),
        sender=header(payload, "From"),
        subject=header(payload, "Subject"),
        internal_date=_internal_date(payload_response),
        text_plain=parts.get("text/plain", ""),
        text_html=parts.get("text/html", ""),
        label_ids=tuple(payload_response.get("labelIds", []) or ()),
    )


def collect_text_parts(payload: dict) -> dict[str, str]:
    """Walk the MIME tree, returning the longest text part of each type.

    Longest rather than first: forwarded mail nests an entire original message
    as further parts, so a message can hold several `text/plain` leaves and the
    first one is often a one-line wrapper above the quoted original.

    An attached message's text is appended after the outer message's own, with
    a forwarded-message divider between them - see NESTED_MESSAGE. Two passes
    rather than one so the covering note leads regardless of the order the
    parts happen to appear in, which matters because truncation cuts from the
    start.

    Pure - takes the payload dict, touches no network - so every tree shape
    above is testable without a mock.
    """
    found: dict[str, str] = {}
    _walk(payload, found)

    nested: dict[str, str] = {}
    for attached in _nested_messages(payload):
        # The children, not the node: `_walk` returns immediately on a
        # message/rfc822 node, which is what keeps the first pass out of here.
        for part in attached.get("parts", []) or ():
            _walk(part, nested)

    for mime, text in nested.items():
        outer = found.get(mime, "")
        found[mime] = f"{outer}{FORWARD_SEPARATOR}{text}" if outer else text
    return found


def _nested_messages(node: dict) -> list[dict]:
    """Every attached message, without descending past the first level of each.

    A message forwarded inside a forwarded message is cargo within cargo; one
    level is what mirrors inline forwarding.
    """
    found: list[dict] = []
    for part in node.get("parts", []) or ():
        if part.get("mimeType") == NESTED_MESSAGE:
            found.append(part)
        else:
            found += _nested_messages(part)
    return found


def _walk(node: dict, found: dict[str, str]) -> None:
    mime = node.get("mimeType", "")
    if mime == NESTED_MESSAGE:
        # Handled by the second pass in collect_text_parts, so the note leads.
        return
    if mime in TEXT_TYPES:
        text = _decode(node)
        if len(text) > len(found.get(mime, "")):
            found[mime] = text
    for part in node.get("parts", []) or ():
        _walk(part, found)


def _decode(node: dict) -> str:
    """base64url -> text. A part with no data (an attachment) yields "".

    `errors="replace"` rather than raising: a single mis-declared charset in a
    decade of mail should cost one garbled character, not a failed
    classification.
    """
    data = node.get("body", {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def header(payload: dict, name: str) -> str:
    """A header value by case-insensitive name, or "" if absent.

    Case-insensitive because header casing is not guaranteed by RFC 5322 and
    real senders vary it.
    """
    for entry in payload.get("headers", []) or ():
        if entry.get("name", "").lower() == name.lower():
            return entry.get("value", "")
    return ""


def _internal_date(message: dict) -> dt.datetime:
    """Gmail's `internalDate` (epoch milliseconds, as a string) -> UTC datetime.

    `internalDate` rather than the `Date:` header: it is when Gmail received
    the message, where the header is whatever the sender's clock said and is
    occasionally years wrong. The eval frame is date-scoped, so this is the
    field that decides membership.
    """
    raw = message.get("internalDate")
    if raw is None:
        return dt.datetime.fromtimestamp(0, dt.UTC)
    return dt.datetime.fromtimestamp(int(raw) / 1000, dt.UTC)
