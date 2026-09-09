"""Auth state machine: expiry arithmetic and the consent round-trip.

Deliberately not tested: fetch_token, credential refresh, and anything else
that is Google's library rather than ours. What is tested is the branching and
date arithmetic around them, because those fail silently - a warning that
never fires looks identical to a warning that was not needed.
"""

import datetime as dt
import json

import pytest

from app import auth


def fake_token_file(consented_days_ago: float | None = 0) -> None:
    """Write a token file. Contents are never parsed as real credentials -
    tests that get past the file check monkeypatch load_credentials."""
    payload = {"credentials": {"token": "x", "refresh_token": "y"}}
    if consented_days_ago is not None:
        payload["consented_at"] = (
            auth._now() - dt.timedelta(days=consented_days_ago)
        ).isoformat()
    auth.TOKEN_PATH.write_text(json.dumps(payload))


@pytest.fixture
def credentials_ok(monkeypatch):
    monkeypatch.setattr(auth, "load_credentials", lambda: object())


# ---------- status() ----------

def test_status_no_token_file():
    s = auth.status()
    assert s["connected"] is False
    assert s["needs_auth"] is True
    assert "Never authenticated" in s["reason"]


def test_status_token_present_but_refresh_dead(monkeypatch):
    """An expired refresh token is a needs_auth state, not a transient error."""
    fake_token_file()
    monkeypatch.setattr(auth, "load_credentials", lambda: None)
    s = auth.status()
    assert s["connected"] is False
    assert s["needs_auth"] is True
    assert "Reconnect" in s["reason"]


def test_status_fresh_consent(credentials_ok):
    fake_token_file(consented_days_ago=0)
    s = auth.status()
    assert s["connected"] is True
    assert s["needs_auth"] is False
    assert s["days_since_consent"] == 0
    assert s["days_remaining"] == auth.REFRESH_TOKEN_DAYS
    assert s["expiry_warning"] is False


@pytest.mark.parametrize(
    "days_ago,warn",
    [(0, False), (4, False), (5, False), (6, True), (7, True), (9, True)],
)
def test_status_expiry_warning_boundary(credentials_ok, days_ago, warn):
    """The warning must fire on day 6 - the whole point is that expiry is
    never silent. An off-by-one here means it never fires at all."""
    fake_token_file(consented_days_ago=days_ago)
    assert auth.status()["expiry_warning"] is warn


def test_status_past_expiry_does_not_report_negative_days(credentials_ok):
    """Credentials can outlive the assumed window; the message must not read
    'expires in -2 days'."""
    fake_token_file(consented_days_ago=9)
    s = auth.status()
    assert s["days_remaining"] < 0        # the raw figure may go negative
    assert "in -" not in s["reason"]      # but the message must clamp it
    assert "~0 day(s)" in s["reason"]


def test_status_without_consent_timestamp(credentials_ok):
    """Older token files predate the timestamp; connected, just no countdown."""
    fake_token_file(consented_days_ago=None)
    s = auth.status()
    assert s["connected"] is True
    assert "days_remaining" not in s


# ---------- consent round-trip ----------

class FakeCredentials:
    def to_json(self):
        return json.dumps({"token": "new", "refresh_token": "r"})


class FakeFlow:
    def __init__(self):
        self.code_verifier = None
        self.fetched_with = None
        self.credentials = FakeCredentials()

    def fetch_token(self, code):
        self.fetched_with = code


def test_authorization_url_stores_state_and_verifier():
    """Both must survive to the callback: the state proves the callback is
    ours, the PKCE verifier is required for the exchange to be accepted."""
    auth.authorization_url()
    assert len(auth._pending_flows) == 1
    state, verifier = next(iter(auth._pending_flows.items()))
    assert state and verifier


def test_exchange_rejects_unknown_state(monkeypatch):
    monkeypatch.setattr(auth, "_flow", FakeFlow)
    with pytest.raises(ValueError):
        auth.exchange_code("code", "never-issued")


def test_exchange_restores_verifier_onto_fresh_flow(monkeypatch):
    """The callback builds a new Flow, so the verifier from /auth/start has to
    be put back onto it or Google rejects the exchange."""
    auth._pending_flows["s1"] = "verifier-123"
    flow = FakeFlow()
    monkeypatch.setattr(auth, "_flow", lambda: flow)

    auth.exchange_code("the-code", "s1")

    assert flow.code_verifier == "verifier-123"
    assert flow.fetched_with == "the-code"
    assert auth.TOKEN_PATH.exists()


def test_state_is_single_use(monkeypatch):
    """A replayed callback URL must not authenticate a second time."""
    auth._pending_flows["s1"] = "v"
    monkeypatch.setattr(auth, "_flow", lambda: FakeFlow())

    auth.exchange_code("code", "s1")
    with pytest.raises(ValueError):
        auth.exchange_code("code", "s1")


def test_exchange_records_consent_timestamp(monkeypatch):
    auth._pending_flows["s1"] = "v"
    monkeypatch.setattr(auth, "_flow", lambda: FakeFlow())
    auth.exchange_code("code", "s1")
    stored = json.loads(auth.TOKEN_PATH.read_text())
    assert "consented_at" in stored
    assert stored["credentials"]["refresh_token"] == "r"
