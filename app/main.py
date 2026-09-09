"""FastAPI app: routes only. Logic belongs in the modules they call.

Phase 0 scope is authentication - enough to obtain a token and report
connection state. The dashboard, poller controls and metrics views arrive in
Phase 5.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import auth

app = FastAPI(title="Email Agent")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    s = auth.status()
    if s["connected"]:
        remaining = s.get("days_remaining")
        expiry = (
            f"<p>Consent given {s.get('days_since_consent')} day(s) ago; "
            f"about {remaining} remaining.</p>"
            if remaining is not None
            else ""
        )
        banner = (
            f"<p><strong>{s['reason']}</strong></p>" if s.get("reason") else ""
        )
        body = (
            "<p>Gmail: <strong>connected</strong> "
            f"(scope: {', '.join(s['scopes'])})</p>{expiry}{banner}"
            '<p><a href="/auth/start">Reconnect Gmail</a></p>'
        )
    else:
        body = (
            f"<p>Gmail: <strong>not connected</strong> - {s['reason']}</p>"
            '<p><a href="/auth/start">Connect Gmail</a></p>'
        )
    return (
        "<!doctype html><title>Email Agent</title>"
        "<h1>Email Agent</h1>"
        "<p>Phase 0 - read-only.</p>" + body
    )


@app.get("/auth/start")
def auth_start() -> RedirectResponse:
    return RedirectResponse(auth.authorization_url())


# Returns a redirect on success and HTML on failure, so the annotation is
# the Response base class - FastAPI cannot build a response model from a
# union of Response subclasses.
@app.get("/auth/callback")
def auth_callback(request: Request) -> Response:
    params = request.query_params

    # The user declined at the consent screen, or Google rejected the request.
    if "error" in params:
        return HTMLResponse(
            "<!doctype html><title>Authorisation failed</title>"
            f"<h1>Authorisation failed</h1><p>{params['error']}</p>"
            '<p><a href="/">Back</a></p>',
            status_code=400,
        )

    code, state = params.get("code"), params.get("state")
    if not code or not state:
        return HTMLResponse(
            "<!doctype html><h1>Missing code or state</h1>"
            '<p><a href="/">Back</a></p>',
            status_code=400,
        )

    try:
        auth.exchange_code(code, state)
    except (ValueError, FileNotFoundError) as e:
        return HTMLResponse(
            f"<!doctype html><h1>Authorisation failed</h1><p>{e}</p>"
            '<p><a href="/">Back</a></p>',
            status_code=400,
        )

    return RedirectResponse("/", status_code=303)


@app.get("/auth/status")
def auth_status() -> JSONResponse:
    return JSONResponse(auth.status())
