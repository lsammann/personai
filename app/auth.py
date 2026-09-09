"""OAuth: consent flow, token storage, credential validation.

The client is registered with Google as a **Web application**, not a Desktop
app, so the flow runs as ordinary routes in this app rather than through
`InstalledAppFlow.run_local_server()` (which blocks, starts a second web
server, and shells out to a browser). First-time setup and the weekly
re-authentication are therefore the same code path.

Scope is read-only and stays that way until Phase 3 - see CLAUDE.md.
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

# Phase 0-2 are structurally incapable of modifying the mailbox. Widening
# this to gmail.modify is a Phase 3 decision and forces a re-consent.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

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
    _write_token(flow.credentials)


def _write_token(creds: Credentials, consented_at: str | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if consented_at is None:
        consented_at = _now().isoformat()
    TOKEN_PATH.write_text(
        json.dumps(
            {
                "credentials": json.loads(creds.to_json()),
                # When the human last approved consent. Refreshing an access
                # token does not reset this - only a new consent does, which
                # is what the 7-day window actually measures.
                "consented_at": consented_at,
            },
            indent=2,
        )
        + "\n"
    )


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
        # The access token changed; keep the original consent timestamp.
        _write_token(creds, consented_at=stored.get("consented_at"))
        return creds

    return None


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
        "scopes": SCOPES,
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
