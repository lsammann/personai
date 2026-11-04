"""Gmail API access: search, count, fetch, and the MIME walk.

Transport only. This module knows the shape of Gmail's payload JSON and
nothing about what the model should read - choosing between the plain and HTML
parts is model-input policy and lives in `message_body`. `fetch()` therefore
returns both text parts, raw and decoded, and expresses no preference.

**The only door to the Gmail API.** No other module in `app/` or `scripts/`
may call `svc.users()`, and every API call made here must be on an explicit
allowlist - both enforced by `tests/test_write_guard.py`. Two writes:
`labels.create` (Phase 3 step 1), which can create a label but cannot touch a
message, and `messages.modify` (step 5), the single atomic label change,
reached only through `modify_labels`. Who may call `modify_labels` is itself
an allowlist in that test. The granted OAuth scope is the backstop
underneath.

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
from collections.abc import Collection
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

# The only system label `modify_labels` may touch. Every other id must be a
# user label, which Gmail ids as `Label_<n>`. See `_check_modifiable`.
MODIFIABLE_SYSTEM_LABEL = "INBOX"
USER_LABEL_ID_PREFIX = "Label_"

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


class GmailApiError(GmailError):
    """An API call that failed after `_execute`'s backoff, or was never worth
    retrying. Raised in place of googleapiclient's `HttpError`, `from` it.

    Exists so callers outside this module can tell a Gmail failure from a
    bug in their own code without importing `googleapiclient`, which
    `tests/test_write_guard.py` forbids. The agent turns this into a failure
    row; a bare `except Exception` would turn a bug into one too (Phase 3
    plan, Step 7 Q2).

    `status` is the HTTP status, or None if the response carried none.
    """

    def __init__(self, message: str, status: int | None):
        self.status = status
        super().__init__(message)


class Forbidden(GmailApiError):
    """Any 403 that outlasted the backoff, or was never retried.

    A 403 from Gmail is always about the account, never about one message:
    a scope the token lacks, a daily limit, or a rate limit still in force
    after every attempt. Every later message would fail the same way, so
    callers treat it like an auth failure and halt rather than logging one
    failure per message.
    """


class MissingLabels(GmailError):
    """One or more required labels do not exist in the mailbox.

    Raised by `label_ids` so a run fails closed: classifying into a label
    that is not there would mean either a 400 per message or, worse, a
    silent fallback.
    """

    def __init__(self, missing: Collection[str]):
        self.missing = sorted(missing)
        super().__init__(
            "Missing labels: " + ", ".join(self.missing)
            + ". Run scripts/setup_labels.py."
        )


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
    and the second is exactly what an insufficient scope produces if a call
    the token does not cover ever slips in. Retrying that would burn a minute
    and then report a rate-limit problem that was really a scope violation,
    which is precisely the error this project most wants to see clearly.
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

    What survives the retries is raised as `GmailApiError` (`Forbidden` for
    a 403), never as the raw `HttpError`, so no caller has to import
    `googleapiclient` to catch it.

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
                raise _api_error(error) from error
            retry_count += 1
            delay = min(2.0**attempt, BACKOFF_CAP)
            # Equal jitter: half the delay guaranteed, half randomised.
            time.sleep(delay / 2 + random.uniform(0, delay / 2))
    raise AssertionError("unreachable: the final attempt either returns or raises")


def _api_error(error: HttpError) -> GmailApiError:
    """The `GmailApiError` (or `Forbidden`) a final `HttpError` becomes."""
    status = getattr(error.resp, "status", None)
    status = int(status) if status is not None else None
    kind = Forbidden if status == 403 else GmailApiError
    return kind(str(error), status=status)


def search_refs(svc, query: str, limit: int | None = None) -> list[tuple[str, str]]:
    """(message_id, thread_id) pairs matching a Gmail query, paginating.

    `messages.list` returns both fields on every row, so the thread id is free
    - and the eval sampler's `S_human` stratum needs it. Sent mail is excluded
    from the frame, so the only way to find the received messages in threads I
    replied to is to match thread ids, and doing that from `get` calls would
    cost one request per message instead of one per five hundred.

    Ids only, never bodies. Enumerating the whole eval frame this way is
    thirteen calls and a few seconds, where fetching those messages would be
    thousands.
    """
    refs: list[tuple[str, str]] = []
    page_token = None
    while True:
        page_size = 500 if limit is None else min(500, limit - len(refs))
        response = _execute(
            svc.users()
            .messages()
            .list(userId="me", q=query, maxResults=page_size, pageToken=page_token)
        )
        refs += [
            (message["id"], message.get("threadId", ""))
            for message in response.get("messages", [])
        ]
        page_token = response.get("nextPageToken")
        if not page_token or (limit is not None and len(refs) >= limit):
            break
    return refs if limit is None else refs[:limit]


def search_ids(svc, query: str, limit: int | None = None) -> list[str]:
    """Message ids matching a Gmail query. `search_refs` without the thread ids."""
    return [message_id for message_id, _ in search_refs(svc, query, limit)]


def count(svc, query: str) -> int:
    """Exact count by pagination.

    Not `resultSizeEstimate`, which is an estimate and drifts by hundreds on
    queries this size. The stratum weights are `N_h / n_h`, so an estimated
    numerator would put a quiet systematic error straight into the headline
    accuracy figure.
    """
    return len(search_ids(svc, query))


def fetch_raw(svc, message_id: str) -> dict:
    """The full `messages.get` response, unparsed.

    For measurement scripts that need more of the payload than `Message`
    carries. Exists so they reach the API through here, with backoff, rather
    than calling `svc.users()` themselves.
    """
    return _execute(
        svc.users().messages().get(userId="me", id=message_id, format="full")
    )


def fetch(svc, message_id: str) -> Message:
    """One message, with both text parts decoded.

    Transient failures are retried with backoff inside `_execute`; anything
    that survives that propagates. Callers stay resumable regardless - the
    labelling CLI appends after every decision - but resumability is the
    recovery path for a genuine failure, not a substitute for handling a
    rate limit that normal operation trips on its own.
    """
    payload_response = fetch_raw(svc, message_id)
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


def all_labels(svc) -> dict[str, str]:
    """name -> id for every label in the mailbox, system labels included.

    One `labels.list` call; it is not paginated. `setup_labels` needs every
    name, not just the wanted ones, to see case variants.
    """
    response = _execute(svc.users().labels().list(userId="me"))
    return {label["name"]: label["id"] for label in response.get("labels", [])}


def label_ids(svc, names: Collection[str]) -> dict[str, str]:
    """name -> id for every requested label, matched by exact name.

    Raises `MissingLabels` naming every absent label, not just the first, so
    one rerun of setup fixes all of them. Exact match only: a case variant is
    a different label as far as this is concerned, and `setup_labels` is
    where a clash gets reported.
    """
    by_name = all_labels(svc)
    missing = [name for name in names if name not in by_name]
    if missing:
        raise MissingLabels(missing)
    return {name: by_name[name] for name in names}


def create_label(svc, name: str) -> str:
    """Create one user label and return its id.

    Name only; Gmail's default visibility (shown in the sidebar and on
    messages) is what the agent's labels want. Not idempotent, but still
    wrapped in `_execute`: if a create succeeds and its response is lost to a
    5xx, the retry gets 409 "already exists", which is not retryable and
    raises. Rerunning `setup_labels`, which is idempotent, then finds it.
    """
    response = _execute(svc.users().labels().create(userId="me", body={"name": name}))
    return response["id"]


def modify_labels(
    svc,
    message_id: str,
    *,
    add_ids: Collection[str],
    remove_ids: Collection[str],
) -> tuple[str, ...]:
    """The single atomic label change. Returns the labels Gmail reports after.

    One `messages.modify` call carrying both sets, so an add can never land
    without its remove - CLAUDE.md's atomic-modify rule, enforced by the
    signature rather than by convention. Both keywords are required, even
    when one side is empty, so every call site states both halves.

    Ids, not names; they come from `label_ids()`. Only `INBOX` and user
    labels may be touched - see `_check_modifiable` - which also catches a
    label name passed by mistake.

    Wrapped in `_execute`: modify SETS labels rather than toggling them, so a
    retried call that had already succeeded changes nothing. A permission
    403 is still never retried.

    Returns the post-change labels from Gmail's response, so callers can
    record the state Gmail confirmed rather than the one they asked for.
    """
    add = _label_id_set(add_ids, "add_ids")
    remove = _label_id_set(remove_ids, "remove_ids")
    if not add and not remove:
        raise ValueError("empty label change - a caller bug, not a no-op")
    if add & remove:
        raise ValueError(f"ids on both sides of one change: {sorted(add & remove)}")
    _check_modifiable(add | remove)

    response = _execute(
        svc.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": sorted(add), "removeLabelIds": sorted(remove)},
        )
    )
    return tuple(response.get("labelIds", []) or ())


def _label_id_set(ids: Collection[str], name: str) -> set[str]:
    """`ids` as a set, refusing a bare string.

    A string is a collection of its characters, so `add_ids="Label_3"`
    would otherwise ask for labels `L`, `a`, `b`...
    """
    if isinstance(ids, str):
        raise TypeError(f"{name} must be a collection of ids, not a string: {ids!r}")
    return set(ids)


def _check_modifiable(ids: Collection[str]) -> None:
    """Refuse every system label except INBOX.

    The agent and undo only ever move INBOX and `Agent/` labels, so this
    costs nothing - and in exchange `modify_labels` cannot trash, spam,
    un-spam or mark-read a message whatever a caller passes. User labels
    are recognised by Gmail's `Label_` id form (observed in Phase 3 step 2).
    If Google changed that form, this fails closed: it raises rather than
    lets anything through.
    """
    refused = sorted(
        label_id
        for label_id in ids
        if label_id != MODIFIABLE_SYSTEM_LABEL
        and not label_id.startswith(USER_LABEL_ID_PREFIX)
    )
    if refused:
        raise ValueError(
            f"refusing to modify {refused}: only {MODIFIABLE_SYSTEM_LABEL} and "
            f"user labels ({USER_LABEL_ID_PREFIX}*) may be changed - "
            "pass label ids from label_ids(), not names"
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
