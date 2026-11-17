"""OAuth: consent flow, token storage, credential validation.

The client is registered with Google as a **Web application**, not a Desktop
app, so the flow runs as ordinary routes in this app rather than through
`InstalledAppFlow.run_local_server()` (which blocks, starts a second web
server, and shells out to a browser). First-time setup and the weekly
re-authentication are therefore the same code path.

Scope was read-only until Phase 3 step 2, gained `gmail.labels` there, and
became `gmail.modify` at step 4 - see CLAUDE.md. What Google *granted* is recorded at consent and checked by
`require_scopes`; the `SCOPES` constant is only what we *ask* for.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from app.config import CREDENTIALS_PATH, DATA_DIR, TOKEN_PATH

# oauthlib refuses non-HTTPS redirect URIs by default. Ours is
# http://localhost:8000/auth/callback - loopback only, never leaves the
# machine - so relaxing this is safe here and required for the flow to run.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
# Google sometimes returns scopes in a different order, or adds ones already
# granted, which oauthlib treats as a scope change and rejects.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

READONLY = "https://www.googleapis.com/auth/gmail.readonly"
LABELS = "https://www.googleapis.com/auth/gmail.labels"
MODIFY = "https://www.googleapis.com/auth/gmail.modify"

# What the consent flow REQUESTS. Phases 0-2 were structurally incapable of
# modifying the mailbox; each widening is its own Phase 3 step and forces a
# re-consent. Never read this to learn what the token can do - Google may
# grant less than was asked, and that is what `granted_scopes()` reports.
#
# Phase 3 step 2 added gmail.labels, which can create labels but cannot touch
# a message. Step 4 replaced both with gmail.modify alone: it covers both
# (`_IMPLIES`), so requesting all three would add nothing, and one scope keeps
# the consent screen exact about what is granted. The token can now change
# messages; whether code can is decided by the allowlist in
# tests/test_write_guard.py. A test pins this list.
SCOPES = [MODIFY]

# gmail.modify covers everything gmail.labels and gmail.readonly allow. Kept
# in one place so no caller has to know the scope hierarchy.
_IMPLIES = {MODIFY: frozenset({LABELS, READONLY})}

REDIRECT_URI = "http://localhost:8000/auth/callback"

# Consent screens left in Testing status issue refresh tokens that expire
# after 7 days. Assumed, not yet confirmed - the warning below is timed off
# it, so it is listed as an open assumption in DESIGN.md.
REFRESH_TOKEN_DAYS = 7
WARN_AFTER_DAYS = 6

# In-flight consent round-trips: CSRF state -> PKCE code verifier.
#
# Two things must survive from /auth/start to /auth/callback. The state proves
# the callback belongs to a flow we started. The code verifier is required
# because Flow enables PKCE by default: authorization_url() generates a random
# verifier and sends Google its hash as `code_challenge`, and fetch_token()
# must present the original verifier for Google to check against that hash.
# The callback builds a fresh Flow object, so without carrying the verifier
# across, the token exchange is rejected.
#
# In-memory is sufficient: the window is seconds, and a server restart in
# between simply means starting the flow again.
_pending_flows: dict[str, str] = {}


class InsufficientScope(Exception):
    """The stored consent does not cover what this entry point needs.

    Lives here rather than under `gmail_client.NotAuthenticated` because
    `gmail_client` imports `auth`; the reverse import would be circular. Like
    an expired token, retrying cannot help - a human has to re-consent.
    """


def _flow() -> Flow:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"{CREDENTIALS_PATH} not found. Download the OAuth client JSON "
            "(type: Web application) from the Google Cloud console."
        )
    return Flow.from_client_secrets_file(
        str(CREDENTIALS_PATH), scopes=SCOPES, redirect_uri=REDIRECT_URI
    )


def authorization_url() -> str:
    """Build the Google consent URL, remembering its state and PKCE verifier."""
    flow = _flow()
    url, state = flow.authorization_url(
        # Required to receive a refresh token at all.
        access_type="offline",
        # Forces the consent screen every time. Without it Google returns no
        # refresh token on repeat authorisations, which breaks precisely the
        # weekly re-auth this flow exists to serve.
        prompt="consent",
    )
    # Generated inside authorization_url() above; needed again at exchange.
    _pending_flows[state] = flow.code_verifier
    return url


def exchange_code(code: str, state: str) -> None:
    """Swap the callback's code for tokens and persist them."""
    if state not in _pending_flows:
        raise ValueError("Unrecognised OAuth state - restart the flow.")
    # Single use: a replayed callback finds nothing here.
    code_verifier = _pending_flows.pop(state)

    flow = _flow()
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    # `granted_scopes` is Google's token response, read by google_auth_oauthlib
    # from the `scope` field. It is NOT kept by `Credentials.to_json()`, so it
    # has to be captured here or it is lost.
    raw_granted = getattr(flow.credentials, "granted_scopes", None)
    _write_token(
        flow.credentials,
        granted=None if raw_granted is None else _normalise_scopes(raw_granted),
    )


def _normalise_scopes(scopes: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Google's `scope` field is space-separated; the library sometimes splits it."""
    if scopes is None:
        return []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return sorted(set(scopes))


def _write_token(
    creds: Credentials,
    consented_at: str | None = None,
    granted: list[str] | None = None,
) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if consented_at is None:
        consented_at = _now().isoformat()
    record: dict[str, Any] = {
        "credentials": json.loads(creds.to_json()),
        # When the human last approved consent. Refreshing an access
        # token does not reset this - only a new consent does, which
        # is what the 7-day window actually measures.
        "consented_at": consented_at,
    }
    # What that consent granted. Top level, beside consented_at, for the same
    # reason: it is a fact about the consent, not the access token. Omitted
    # rather than written empty when unknown, so "never recorded" stays
    # distinguishable on disk.
    if granted is not None:
        record["granted_scopes"] = granted
    TOKEN_PATH.write_text(json.dumps(record, indent=2) + "\n")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _read_token() -> dict[str, Any] | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text())
    except json.JSONDecodeError:
        return None


def load_credentials() -> Credentials | None:
    """Return usable credentials, refreshing if needed.

    Returns None when re-consent is required. Callers must treat that as the
    `needs_auth` state rather than as a transient error: an expired refresh
    token fails identically on every retry, so retrying accomplishes nothing.
    """
    stored = _read_token()
    if stored is None:
        return None

    creds = Credentials.from_authorized_user_info(stored["credentials"], SCOPES)
    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            return None
        # The access token changed; the consent did not. Keep both facts
        # about it as they were recorded.
        _write_token(
            creds,
            consented_at=stored.get("consented_at"),
            granted=stored.get("granted_scopes"),
        )
        return creds

    return None


def granted_scopes() -> frozenset[str]:
    """What the last consent actually granted, as Google returned it.

    Empty when nothing is recorded - no token, or a token written before this
    was captured. Empty means `require_scopes` fails closed, which is the
    point: an unknown grant is not assumed to be the requested one.
    """
    stored = _read_token()
    if stored is None:
        return frozenset()
    return frozenset(_normalise_scopes(stored.get("granted_scopes")))


def require_scopes(scope: str, *more: str) -> None:
    """Raise `InsufficientScope` unless the granted set covers every scope.

    Checks the recorded grant, not `SCOPES`: requesting a scope proves
    nothing about having it. Each entry point calls this once, with every
    scope it needs, before making any API call. The error names every
    missing scope, so one re-consent fixes all of them.

    Separate arguments rather than a list: a list parameter given a single
    string would iterate its characters. The first is positional and
    required, so calling with no scopes at all is a TypeError rather than a
    check that silently passes.
    """
    granted = granted_scopes()
    covered = set(granted)
    for held in granted:
        covered |= _IMPLIES.get(held, frozenset())
    missing = [needed for needed in (scope, *more) if needed not in covered]
    if missing:
        held_text = ", ".join(sorted(granted)) or "nothing recorded"
        raise InsufficientScope(
            f"Needs {', '.join(missing)}, but the stored consent grants: "
            f"{held_text}. Reconnect via /auth/start."
        )


def status() -> dict[str, Any]:
    """Connection state for the dashboard and the Start gate."""
    stored = _read_token()
    if stored is None:
        return {
            "connected": False,
            "needs_auth": True,
            "reason": "Never authenticated.",
        }

    if load_credentials() is None:
        return {
            "connected": False,
            "needs_auth": True,
            "reason": "Refresh token expired or revoked. Reconnect Gmail.",
        }

    consented_at = stored.get("consented_at")
    result: dict[str, Any] = {
        "connected": True,
        "needs_auth": False,
        "consented_at": consented_at,
        # What Google granted, not what we asked for. Empty means the
        # consent predates recording it.
        "granted_scopes": sorted(granted_scopes()),
    }
    if consented_at:
        age_days = (_now() - dt.datetime.fromisoformat(consented_at)).days
        remaining = REFRESH_TOKEN_DAYS - age_days
        result["days_since_consent"] = age_days
        result["days_remaining"] = remaining
        result["expiry_warning"] = age_days >= WARN_AFTER_DAYS
        if result["expiry_warning"]:
            result["reason"] = (
                f"Gmail access expires in ~{max(remaining, 0)} day(s) - "
                "reconnect soon."
            )
    return result
