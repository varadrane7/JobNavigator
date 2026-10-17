"""OIDC sign-in routes (MT-20; flow and rules in backend/accounts/oidc.py).

GET /api/auth/oidc/config    {enabled, provider_name}: what the login screen needs. Public.
GET /api/auth/oidc/login     302 to the provider. Public.
GET /api/auth/oidc/callback  the provider's redirect target: verifies the login, then sets
                             `jn_session` (the install key, httpOnly: the browser never sees it)
                             and a flagged `jn_account`, and redirects to /. On any failure it
                             redirects to /?oidc_error=<code> and sets nothing. Public.
"""
import logging
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from backend.accounts import auth as acct_auth
from backend.accounts import oidc
from backend.accounts.settings import get_install_setting
from backend.config import INITIAL_API_KEY
from backend.models.db import SessionLocal

logger = logging.getLogger("jobnavigator.oidc")

router = APIRouter(prefix="/auth/oidc", tags=["auth"])


def _base(request: Request) -> str:
    """Scheme + host the browser used (Caddy forwards both); OIDC_PUBLIC_URL overrides."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _error_redirect(code: str) -> RedirectResponse:
    resp = RedirectResponse(f"/?oidc_error={quote(code)}", status_code=302)
    resp.delete_cookie(oidc.STATE_COOKIE, path="/api/auth/oidc")
    return resp


@router.get("/config")
def oidc_config():
    db = SessionLocal()
    try:
        cfg = oidc.config(db)
        on = bool(cfg["discovery_url"] and cfg["client_id"])
        return {"enabled": on, "provider_name": cfg["provider_name"] or "your identity provider"}
    finally:
        db.close()


@router.get("/login")
async def oidc_login(request: Request):
    db = SessionLocal()
    try:
        if not oidc.enabled(db):
            return JSONResponse(status_code=404, content={"detail": "oidc_disabled"})
        try:
            url, state_cookie = await oidc.start_login(db, _base(request))
        except oidc.OidcDenied as e:
            return _error_redirect(e.code)
        cfg = oidc.config(db)
        resp = RedirectResponse(url, status_code=302)
        # Lax, not Strict: the provider's redirect back is a cross-site navigation, and the
        # cookie must come with it.
        resp.set_cookie(oidc.STATE_COOKIE, state_cookie, max_age=oidc.STATE_MAX_AGE, httponly=True,
                        samesite="lax", secure=_base(request).startswith("https") or cfg["public_url"].startswith("https"),
                        path="/api/auth/oidc")
        return resp
    finally:
        db.close()


@router.get("/callback")
async def oidc_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    db = SessionLocal()
    try:
        if not oidc.enabled(db):
            return _error_redirect("oidc_disabled")
        if error:
            logger.info("OIDC provider returned error %s", error)
            return _error_redirect("provider_denied")
        try:
            st = oidc.read_state(db, request.cookies.get(oidc.STATE_COOKIE), state)
            if not code:
                raise oidc.OidcDenied("token_exchange_failed")
            cfg = oidc.config(db)
            doc = await oidc.discovery(cfg["discovery_url"])
            tokens = await oidc.exchange_code(cfg, doc, code, st["v"], _base(request))
            if not tokens.get("id_token"):
                raise oidc.OidcDenied("token_invalid", "The provider returned no identity token.")
            claims = await oidc.verify_id_token(cfg, doc, tokens["id_token"], st["n"])
            if not claims.get("email") or not claims.get("name"):
                claims = {**(await oidc.userinfo(doc, tokens.get("access_token", ""))), **claims}
            account = oidc.resolve_account(db, cfg, doc["issuer"], claims)
        except oidc.OidcDenied as e:
            logger.info("OIDC login denied: %s", e.code)
            return _error_redirect(e.code)

        resp = RedirectResponse("/", status_code=302)
        resp.delete_cookie(oidc.STATE_COOKIE, path="/api/auth/oidc")
        secure = _base(request).startswith("https") or cfg["public_url"].startswith("https")
        resp.set_cookie("jn_session", get_install_setting(db, "dashboard_api_key", INITIAL_API_KEY) or "",
                        httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30, secure=secure)
        acct_auth.set_account_cookie(resp, db, account, oidc=True)
        acct_auth.touch_last_active(db, account["id"], force=True)
        logger.info("OIDC login: account %s", account["id"])
        return resp
    finally:
        db.close()
