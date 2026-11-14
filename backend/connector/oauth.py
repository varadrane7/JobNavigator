"""The OAuth 2.1 authorization server a hosted MCP client (claude.ai) signs in through.

    GET  /.well-known/oauth-protected-resource[/mcp]   RFC 9728: where /mcp's tokens come from
    GET  /.well-known/oauth-authorization-server       RFC 8414
    POST /oauth/register                               RFC 7591, public clients only
    GET  /oauth/authorize                              consent page for the signed-in dashboard account
    POST /oauth/authorize                              the decision; redirects back with a code
    POST /oauth/token                                  code (PKCE S256) or refresh token -> tokens

The person approves in a browser that is signed in to the dashboard: the account that browser
shows is the account the client gets. Clients are public (no secret) and must use PKCE.
Nothing is stored (connector/tokens.py): a client_id is a signed list of its redirect URIs.
"""
import base64
import hashlib
import hmac
import html
import logging
import time
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from backend.accounts import auth as acct_auth
from backend.connector import tokens
from backend.models.db import SessionLocal

logger = logging.getLogger("jobnavigator.connector")
router = APIRouter(tags=["connector"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_PARAMS = ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method",
           "state", "scope", "resource")


async def _form(request: Request) -> dict:
    """An application/x-www-form-urlencoded body as a dict (capped; no multipart)."""
    raw = await request.body()
    if len(raw) > 16384:
        return {}
    try:
        return dict(parse_qsl(raw.decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        return {}


# ── Metadata ─────────────────────────────────────────────────────────────────

def resource_url(request: Request) -> str:
    return tokens.public_base(request) + "/mcp"


def resource_metadata_url(request: Request) -> str:
    return tokens.public_base(request) + "/.well-known/oauth-protected-resource/mcp"


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
def protected_resource(request: Request):
    base = tokens.public_base(request)
    return {"resource": base + "/mcp", "authorization_servers": [base], "scopes_supported": list(tokens.SCOPES),
            "bearer_methods_supported": ["header"], "resource_name": "JobNavigator"}


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
@router.get("/.well-known/oauth-authorization-server/mcp", include_in_schema=False)
def authorization_server(request: Request):
    base = tokens.public_base(request)
    return {"issuer": base, "authorization_endpoint": base + "/oauth/authorize",
            "token_endpoint": base + "/oauth/token", "registration_endpoint": base + "/oauth/register",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(tokens.SCOPES)}


# ── Clients ──────────────────────────────────────────────────────────────────

def _redirect_ok(uri) -> bool:
    """https anywhere, or plain http on this machine (a CLI's local callback)."""
    if not isinstance(uri, str) or len(uri) > 400 or "#" in uri:
        return False
    parts = urlsplit(uri)
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and (parts.hostname or "") in ("localhost", "127.0.0.1", "::1")


def _same_redirect(registered: str, given: str) -> bool:
    """Exact match; a loopback callback may use any port (RFC 8252 §7.3)."""
    if registered == given:
        return True
    a, b = urlsplit(registered), urlsplit(given)
    return (a.scheme == b.scheme == "http" and a.hostname == b.hostname and a.path == b.path
            and a.query == b.query and (a.hostname or "") in ("localhost", "127.0.0.1", "::1"))


def _client(db, client_id) -> Optional[dict]:
    return tokens.read(db, tokens.KIND_CLIENT, client_id)


@router.post("/oauth/register", include_in_schema=False)
async def register(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= 5 or not all(_redirect_ok(u) for u in uris):
        return JSONResponse({"error": "invalid_redirect_uri",
                             "error_description": "1 to 5 redirect URIs: https, or http on localhost"}, status_code=400)
    name = str(body.get("client_name") or "MCP client")[:60]

    def _make():
        db = SessionLocal()
        try:
            return tokens.sign(db, tokens.KIND_CLIENT, tokens.CLIENT_SECONDS, r=uris, n=name)
        finally:
            db.close()
    client_id = await run_in_threadpool(_make)
    return JSONResponse({"client_id": client_id, "client_id_issued_at": int(time.time()), "client_name": name,
                         "redirect_uris": uris, "token_endpoint_auth_method": "none",
                         "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]},
                        status_code=201, headers=_NO_STORE)


# ── Authorize ────────────────────────────────────────────────────────────────

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>JobNavigator</title>
<style>
:root{{color-scheme:light dark;--bg:#f6f5f2;--card:#fff;--ink:#1c1b19;--dim:#6b6860;--line:#dedbd3;--go:#1f6f4a;--go-ink:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151513;--card:#1f1e1c;--ink:#eceae4;--dim:#9c988e;--line:#35332f;--go:#3d9a6c;--go-ink:#0d130f}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif;padding:16px;box-sizing:border-box}}
main{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px;max-width:440px;width:100%;box-sizing:border-box}}
h1{{font-size:20px;margin:0 0 12px}} p{{margin:0 0 14px}} .dim{{color:var(--dim);font-size:14px}}
code{{font-size:13px;word-break:break-all}} label{{display:flex;gap:10px;align-items:flex-start;margin:0 0 18px}}
.row{{display:flex;gap:10px;flex-wrap:wrap}}
button,a.btn{{font:inherit;padding:10px 18px;border-radius:8px;border:1px solid var(--line);background:none;color:var(--ink);cursor:pointer;text-decoration:none;display:inline-block}}
button.go,a.go{{background:var(--go);color:var(--go-ink);border-color:var(--go)}}
</style></head><body><main>{body}</main></body></html>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(body=body), status_code=status, headers={
        **_NO_STORE, "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
        "frame-ancestors 'none'", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})


def _problem(text: str, status: int = 400) -> HTMLResponse:
    return _page(f"<h1>This connection cannot continue</h1><p>{html.escape(text)}</p>", status)


def _signed_in(request: Request):
    """(account dict or None, why not). The dashboard's own check: the install password
    (jn_session) and the account cookie of this browser."""
    from backend import main as _main
    from backend.accounts.settings import get_install_setting
    db = SessionLocal()
    try:
        if not get_install_setting(db, "dashboard_api_key", _main.INITIAL_API_KEY):
            return None, "no_password"
    finally:
        db.close()
    early, res = _main._authenticate_sync("/oauth/authorize", request.cookies.get("jn_session", ""), "",
                                          request.cookies.get(acct_auth.COOKIE_NAME, ""))
    if early is not None or res is None or res.account is None:
        return None, "signed_out"
    return res.account, None


def _request_hash(params: dict) -> str:
    text = "\n".join(str(params.get(k) or "") for k in _PARAMS)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _check(db, params: dict):
    """(client payload, None) or (None, problem page). Nothing is ever redirected to a
    redirect_uri that is not the client's own."""
    client = _client(db, params.get("client_id"))
    if client is None:
        return None, _problem("The app that sent you here is not registered with this JobNavigator. "
                              "Remove the connector in the app and add it again.")
    redirect = params.get("redirect_uri") or ""
    if not any(_same_redirect(r, redirect) for r in client.get("r") or []):
        return None, _problem("The return address does not belong to the app that sent you here.")
    return client, None


def _back(redirect: str, **query) -> RedirectResponse:
    sep = "&" if urlsplit(redirect).query else "?"
    return RedirectResponse(redirect + sep + urlencode({k: v for k, v in query.items() if v}), status_code=302,
                            headers=_NO_STORE)


def _authorize_get(request: Request):
    params = {k: request.query_params.get(k, "") for k in _PARAMS}
    db = SessionLocal()
    try:
        client, problem = _check(db, params)
        if problem is not None:
            return problem
        redirect = params["redirect_uri"]
        if params["response_type"] != "code":
            return _back(redirect, error="unsupported_response_type", state=params["state"])
        if not params["code_challenge"] or params["code_challenge_method"] != "S256":
            return _back(redirect, error="invalid_request", error_description="PKCE with S256 is required",
                         state=params["state"])
        account, why = _signed_in(request)
        if account is None:
            if why == "no_password":
                return _problem("This JobNavigator has no dashboard password yet. Set one in Settings, "
                                "then connect again.", 403)
            # The session cookies are SameSite=Strict: they are not sent on the way in from
            # another site, but are on a click made here.
            here = html.escape(str(request.url.path) + "?" + urlencode(params), quote=True)
            return _page("<h1>Connect to JobNavigator</h1><p>Continue in the browser where you are signed in to "
                         "your JobNavigator dashboard.</p><p class='dim'>Not signed in here? Open the dashboard, "
                         "sign in, then come back to this tab and continue.</p><div class='row'>"
                         f"<a class='btn go' href='{here}'>Continue</a>"
                         "<a class='btn' href='/' target='_blank' rel='noopener'>Open dashboard</a></div>")
        consent = tokens.sign(db, tokens.KIND_CONSENT, tokens.CONSENT_SECONDS, a=str(account["id"]),
                              q=_request_hash(params))
        hidden = "".join(f"<input type='hidden' name='{k}' value='{html.escape(v, quote=True)}'>"
                         for k, v in {**params, "consent": consent}.items())
        host = html.escape(urlsplit(redirect).hostname or redirect)
        write = "checked" if "write" in params["scope"].split() else ""
        return _page(
            f"<h1>Let {html.escape(client.get('n') or 'this app')} use your JobNavigator?</h1>"
            f"<p>Account <b>{html.escape(account.get('name') or 'you')}</b>. It will be able to read your jobs, "
            "applications, résumés, profile and leads.</p>"
            f"<form method='post' action='/oauth/authorize'>{hidden}"
            f"<label><input type='checkbox' name='write' value='1' {write}><span>Also let it update leads and "
            "applications <span class='dim'>(status, notes, drafts, new leads)</span></span></label>"
            f"<p class='dim'>You will be sent back to <code>{host}</code>. Remove access any time in "
            "Settings › Connector.</p><div class='row'><button class='go' name='decision' value='allow'>Allow"
            "</button><button name='decision' value='deny'>Cancel</button></div></form>")
    finally:
        db.close()


@router.get("/oauth/authorize", include_in_schema=False)
async def authorize(request: Request):
    return await run_in_threadpool(_authorize_get, request)


def _authorize_post(request: Request, form: dict):
    params = {k: str(form.get(k) or "") for k in _PARAMS}
    db = SessionLocal()
    try:
        client, problem = _check(db, params)
        if problem is not None:
            return problem
        redirect = params["redirect_uri"]
        account, _why = _signed_in(request)
        consent = tokens.read(db, tokens.KIND_CONSENT, form.get("consent"))
        if (account is None or consent is None or consent.get("a") != str(account["id"])
                or not hmac.compare_digest(str(consent.get("q") or ""), _request_hash(params))):
            return _problem("This page expired or you are signed in as someone else. Start the connection again.")
        if form.get("decision") != "allow":
            return _back(redirect, error="access_denied", state=params["state"])
        scope = "write" if form.get("write") else "read"
        code = tokens.make_code(db, account, scope, client.get("n") or "", params["client_id"], redirect,
                                params["code_challenge"])
        logger.info("connector: %s approved %s (%s)", account.get("name"), client.get("n"), scope)
        return _back(redirect, code=code, state=params["state"])
    finally:
        db.close()


@router.post("/oauth/authorize", include_in_schema=False)
async def authorize_decision(request: Request):
    return await run_in_threadpool(_authorize_post, request, await _form(request))


# ── Token ────────────────────────────────────────────────────────────────────

def _grant_error(code: str, description: str = "", status: int = 400) -> JSONResponse:
    body = {"error": code}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status, headers=_NO_STORE)


def _issue(db, account: dict, scope: str, client: str, client_id: str) -> JSONResponse:
    return JSONResponse({"access_token": tokens.make_access(db, account, scope, client), "token_type": "Bearer",
                         "expires_in": tokens.ACCESS_SECONDS, "scope": scope,
                         "refresh_token": tokens.make_refresh(db, account, scope, client, client_id)},
                        headers=_NO_STORE)


def _token(form: dict):
    db = SessionLocal()
    try:
        client_id = str(form.get("client_id") or "")
        grant = form.get("grant_type")
        if grant == "authorization_code":
            code = tokens.read(db, tokens.KIND_CODE, form.get("code"))
            if code is None or _client(db, client_id) is None or code.get("cid") != tokens.client_key(client_id):
                return _grant_error("invalid_grant", "The code is not valid for this client")
            if code.get("r") != str(form.get("redirect_uri") or ""):
                return _grant_error("invalid_grant", "redirect_uri differs from the authorization request")
            verifier = str(form.get("code_verifier") or "")
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if not 43 <= len(verifier) <= 128 or not hmac.compare_digest(digest, str(code.get("ch") or "")):
                return _grant_error("invalid_grant", "code_verifier does not match")
            account = tokens.account_of(db, code)
            if account is None or not tokens.use_code(code):
                return _grant_error("invalid_grant", "The code was used or the account changed")
            return _issue(db, account, code.get("s") or "read", code.get("c") or "", client_id)
        if grant == "refresh_token":
            old = tokens.read(db, tokens.KIND_REFRESH, form.get("refresh_token"))
            account = tokens.account_of(db, old)
            if old is None or account is None or old.get("cid") != tokens.client_key(client_id):
                return _grant_error("invalid_grant", "The refresh token is no longer valid")
            return _issue(db, account, old.get("s") or "read", old.get("c") or "", client_id)
        return _grant_error("unsupported_grant_type")
    finally:
        db.close()


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request):
    return await run_in_threadpool(_token, await _form(request))
