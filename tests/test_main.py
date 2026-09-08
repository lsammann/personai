"""Route behaviour - only the parts that are ours.

FastAPI's routing and serialisation are not under test. The error paths in
/auth/callback are, because they are hand-written branches that only execute
when something has already gone wrong.
"""

import pytest
from fastapi.testclient import TestClient

from app import auth, main


@pytest.fixture
def client():
    return TestClient(main.app, follow_redirects=False)


def test_index_offers_connect_when_unauthenticated(client):
    body = client.get("/").text
    assert "not connected" in body
    assert "/auth/start" in body


def test_index_offers_reconnect_when_connected(client, monkeypatch):
    monkeypatch.setattr(auth, "status", lambda: {
        "connected": True, "needs_auth": False,
        "scopes": auth.SCOPES, "days_since_consent": 2, "days_remaining": 5,
    })
    body = client.get("/").text
    assert "connected" in body
    assert "Reconnect Gmail" in body


def test_index_shows_expiry_warning(client, monkeypatch):
    monkeypatch.setattr(auth, "status", lambda: {
        "connected": True, "needs_auth": False, "scopes": auth.SCOPES,
        "days_since_consent": 6, "days_remaining": 1,
        "expiry_warning": True, "reason": "Gmail access expires in ~1 day(s)",
    })
    assert "expires in ~1 day(s)" in client.get("/").text


def test_auth_start_redirects_to_google(client):
    r = client.get("/auth/start")
    assert r.status_code in (302, 307)
    assert r.headers["location"].startswith("https://accounts.google.com/")


def test_callback_reports_user_denial(client):
    """Declining at the consent screen returns error=, not a code."""
    r = client.get("/auth/callback", params={"error": "access_denied"})
    assert r.status_code == 400
    assert "access_denied" in r.text


def test_callback_rejects_missing_code(client):
    assert client.get("/auth/callback", params={"state": "s"}).status_code == 400


def test_callback_rejects_missing_state(client):
    assert client.get("/auth/callback", params={"code": "c"}).status_code == 400


def test_callback_rejects_unknown_state(client):
    """A forged or replayed callback must not authenticate anything."""
    r = client.get("/auth/callback", params={"code": "c", "state": "forged"})
    assert r.status_code == 400
    assert "state" in r.text.lower()


def test_callback_redirects_home_on_success(client, monkeypatch):
    monkeypatch.setattr(auth, "exchange_code", lambda code, state: None)
    r = client.get("/auth/callback", params={"code": "c", "state": "s"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_auth_status_returns_json(client):
    body = client.get("/auth/status").json()
    assert body["needs_auth"] is True
