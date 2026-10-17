"""OIDC single sign-on (MT-20): sign in with Authentik (or any OpenID Connect provider).

Authorization-code flow with PKCE. The provider is found through its discovery document
(`OIDC_DISCOVERY_URL`, e.g. https://auth.example.com/application/o/jobnavigator/.well-known/openid-configuration);
the ID token is verified against the provider's JWKS (issuer, audience, expiry, nonce).

Configuration: each value is the install setting `oidc_*` (Settings › Advanced) when set,
else the `OIDC_*` environment variable:

    OIDC_DISCOVERY_URL    discovery document URL; empty = OIDC login off
    OIDC_CLIENT_ID / OIDC_CLIENT_SECRET
    OIDC_SCOPES           default "openid email profile"
    OIDC_OWNER_EMAIL      the email that signs in as the install owner (links the existing owner row)
    OIDC_ALLOWED_GROUPS   comma-separated `groups` claim values allowed in (empty = anyone the provider admits)
    OIDC_PUBLIC_URL       https://jobs.example.com, for the redirect URI <that>/api/auth/oidc/callback
    OIDC_PROVIDER_NAME    label of the sign-in button

Who a login becomes (`resolve_account`): the account already linked to (issuer, sub); else
the owner row when the email is OIDC_OWNER_EMAIL; else a new member (auto-created, up to
MAX_ACCOUNTS accounts, credentials sections on 'own'). A login proves identity, so it skips
PINs, and its `jn_account` cookie is flagged (`auth.oidc_session`): that browser cannot
switch to another account. The install password stays as break-glass.
"""
import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

from backend.accounts import admin
from backend.accounts import auth as acct_auth
from backend.accounts.context import unscoped
from backend.accounts.settings import get_install_setting

logger = logging.getLogger("jobnavigator.oidc")

DEFAULT_SCOPES = "openid email profile"
STATE_COOKIE = "jn_oidc"
STATE_MAX_AGE = 10 * 60
KIND_STATE = "oidc_state"
HTTP_TIMEOUT = 10.0
_ASYMMETRIC_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
_HMAC_ALGS = ("HS256", "HS384", "HS512")   # Authentik signs with the client secret when no signing key is set


class OidcDenied(Exception):
    """The login cannot proceed; `code` is shown to the user (`?oidc_error=<code>`)."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


# ── Configuration ────────────────────────────────────────────────────────────

_ENV = {"discovery_url": "OIDC_DISCOVERY_URL", "client_id": "OIDC_CLIENT_ID",
        "client_secret": "OIDC_CLIENT_SECRET", "scopes": "OIDC_SCOPES",
        "owner_email": "OIDC_OWNER_EMAIL", "allowed_groups": "OIDC_ALLOWED_GROUPS",
        "public_url": "OIDC_PUBLIC_URL", "provider_name": "OIDC_PROVIDER_NAME"}


def config(db) -> dict:
    out = {}
    for key, env in _ENV.items():
        value = (get_install_setting(db, f"oidc_{key}", "") or "").strip()
        out[key] = value or os.environ.get(env, "").strip()
    out["scopes"] = out["scopes"] or DEFAULT_SCOPES
    # Authentik (and others) mark emails unverified unless told otherwise; the install owner may
    # decide the provider's emails can be trusted (env only, never a Settings value).
    out["trust_email"] = os.environ.get("OIDC_TRUST_EMAIL", "").strip().lower() in ("1", "true", "yes")
    return out


def enabled(db) -> bool:
    cfg = config(db)
    return bool(cfg["discovery_url"] and cfg["client_id"])


def redirect_uri(cfg: dict, request_base: str) -> str:
    base = (cfg["public_url"] or request_base).rstrip("/")
    return f"{base}/api/auth/oidc/callback"


# ── Provider metadata ────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_discovery_cache: dict = {}   # url -> (monotonic time, document)
_jwks_cache: dict = {}        # jwks_uri -> (monotonic time, keys)
CACHE_TTL = 600.0


async def _get_json(url: str, **kw) -> dict:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
        resp = await client.get(url, **kw)
    resp.raise_for_status()
    return resp.json()


async def discovery(url: str) -> dict:
    now = time.monotonic()
    with _cache_lock:
        hit = _discovery_cache.get(url)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        doc = await _get_json(url)
    except Exception as e:
        logger.warning("OIDC discovery failed for %s: %s", url, e)
        raise OidcDenied("provider_unreachable", "The identity provider could not be reached.")
    for field in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(doc.get(field), str):
            raise OidcDenied("provider_invalid", f"Discovery document has no {field}.")
    with _cache_lock:
        _discovery_cache[url] = (now, doc)
    return doc


async def _jwks(uri: str, kid: Optional[str]) -> list:
    now = time.monotonic()
    with _cache_lock:
        hit = _jwks_cache.get(uri)
    if hit and now - hit[0] < CACHE_TTL and (kid is None or any(k.get("kid") == kid for k in hit[1])):
        return hit[1]
    keys = (await _get_json(uri)).get("keys") or []
    with _cache_lock:
        _jwks_cache[uri] = (now, keys)
    return keys


def forget_provider_cache() -> None:
    with _cache_lock:
        _discovery_cache.clear()
        _jwks_cache.clear()


# ── Login state (PKCE + state + nonce, in a signed short-lived cookie) ───────

def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


async def start_login(db, request_base: str) -> tuple:
    """(authorization URL, state cookie value)."""
    cfg = config(db)
    doc = await discovery(cfg["discovery_url"])
    state, nonce, verifier = (secrets.token_urlsafe(24) for _ in range(3))
    params = {"response_type": "code", "client_id": cfg["client_id"], "scope": cfg["scopes"],
              "redirect_uri": redirect_uri(cfg, request_base), "state": state, "nonce": nonce,
              "code_challenge": _challenge(verifier), "code_challenge_method": "S256"}
    cookie = acct_auth.sign_cookie(acct_auth.cookie_secret(db), {
        "k": KIND_STATE, "s": state, "n": nonce, "v": verifier, "e": int(acct_auth._now() + STATE_MAX_AGE)})
    return f"{doc['authorization_endpoint']}?{urlencode(params)}", cookie


def read_state(db, cookie: Optional[str], state: Optional[str]) -> dict:
    payload = acct_auth.read_cookie(acct_auth.cookie_secret(db), cookie)
    if (payload is None or payload.get("k") != KIND_STATE or not state
            or not secrets.compare_digest(str(payload.get("s", "")), state)):
        raise OidcDenied("bad_state", "The sign-in request expired or did not start here. Try again.")
    return payload


# ── Token exchange and verification ──────────────────────────────────────────

async def exchange_code(cfg: dict, doc: dict, code: str, verifier: str, request_base: str) -> dict:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(cfg, request_base),
            "client_id": cfg["client_id"], "code_verifier": verifier}
    if cfg["client_secret"]:
        data["client_secret"] = cfg["client_secret"]
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
            resp = await client.post(doc["token_endpoint"], data=data, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("OIDC token exchange failed: %s", e)
        raise OidcDenied("token_exchange_failed", "The identity provider rejected the sign-in.")


async def verify_id_token(cfg: dict, doc: dict, id_token: str, nonce: str) -> dict:
    import jwt
    try:
        alg = jwt.get_unverified_header(id_token).get("alg")
        kid = jwt.get_unverified_header(id_token).get("kid")
        if alg in _HMAC_ALGS:
            if not cfg["client_secret"]:
                raise OidcDenied("token_invalid", "HMAC-signed token without a client secret.")
            key = cfg["client_secret"]
        elif alg in _ASYMMETRIC_ALGS:
            keys = await _jwks(doc["jwks_uri"], kid)
            jwk = next((k for k in keys if kid is None or k.get("kid") == kid), None)
            if jwk is None:
                raise OidcDenied("token_invalid", "No signing key matches the token.")
            key = jwt.PyJWK(jwk).key
        else:
            raise OidcDenied("token_invalid", f"Unsupported token algorithm {alg!r}.")
        claims = jwt.decode(id_token, key, algorithms=[alg], audience=cfg["client_id"], issuer=doc["issuer"],
                            options={"require": ["exp", "iat", "sub", "iss", "aud"]}, leeway=60)
    except OidcDenied:
        raise
    except Exception as e:
        logger.warning("OIDC ID token rejected: %s", e)
        raise OidcDenied("token_invalid", "The identity token could not be verified.")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OidcDenied("token_invalid", "The identity token does not match this sign-in.")
    return claims


async def userinfo(doc: dict, access_token: str) -> dict:
    endpoint = doc.get("userinfo_endpoint")
    if not endpoint or not access_token:
        return {}
    try:
        return await _get_json(endpoint, headers={"Authorization": f"Bearer {access_token}"})
    except Exception as e:
        logger.info("OIDC userinfo unavailable: %s", e)
        return {}


# ── Claims → account ─────────────────────────────────────────────────────────

def _groups(claims: dict) -> set:
    raw = claims.get("groups") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(g).strip().lower() for g in raw}


def _display_name(claims: dict) -> str:
    for key in ("name", "preferred_username", "nickname"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    email = claims.get("email")
    if isinstance(email, str) and "@" in email:
        return email.split("@", 1)[0]
    return "Member"


def resolve_account(db, cfg: dict, issuer: str, claims: dict) -> dict:
    """The account dict this identity signs in as; creates the member on a first login.
    Raises OidcDenied: not_allowed, no_email, owner_conflict, account_limit."""
    from sqlalchemy.exc import IntegrityError
    from backend.models.db import Account

    allowed = {g.strip().lower() for g in cfg["allowed_groups"].split(",") if g.strip()}
    if allowed and not (allowed & _groups(claims)):
        raise OidcDenied("not_allowed", "Your account is not in a group allowed to use this install.")
    sub = str(claims["sub"])
    email = (claims.get("email") or "").strip().lower() if isinstance(claims.get("email"), str) else ""
    verified = claims.get("email_verified") is not False or cfg["trust_email"]   # absent counts as verified, an explicit false does not
    logger.info("OIDC login: sub=%s email=%s email_verified=%s", sub, email or "-", claims.get("email_verified"))

    with unscoped():
        row = db.query(Account).filter(Account.oidc_issuer == issuer, Account.oidc_sub == sub).first()
        if row is None:
            owner_email = cfg["owner_email"].strip().lower()
            if email and owner_email and email == owner_email and not verified:
                # never fall through to creating a second account for the owner's address
                raise OidcDenied("email_unverified",
                                 "The provider reports this email as unverified; mark it verified there or set OIDC_TRUST_EMAIL=true.")
            if email and verified and owner_email and email == owner_email:
                owner = db.query(Account).filter(Account.role == "owner").first()
                if owner is None:
                    raise OidcDenied("owner_conflict", "This install has no owner account yet.")
                if owner.oidc_sub is not None:
                    raise OidcDenied("owner_conflict", "The owner account is already linked to another identity.")
                owner.oidc_issuer, owner.oidc_sub, owner.oidc_email = issuer, sub, email
                db.commit()
                row = owner
                logger.info("OIDC: linked the owner account to %s", email)
            else:
                if not email:
                    raise OidcDenied("no_email", "The identity provider did not share an email address.")
                try:
                    row = admin.create_member(db, _display_name(claims), issuer, sub, email)
                except admin.AccountLimitReached:
                    raise OidcDenied("account_limit",
                                     f"This install already has {admin.MAX_ACCOUNTS} accounts; ask the owner to remove one.")
                except IntegrityError:   # two first logins of one person raced
                    db.rollback()
                    row = db.query(Account).filter(Account.oidc_issuer == issuer, Account.oidc_sub == sub).first()
                    if row is None:
                        raise
                logger.info("OIDC: created member %s for %s", row.id, email)
        elif email and row.oidc_email != email:
            row.oidc_email = email
            db.commit()
        account_id = row.id
    acct_auth.invalidate_accounts_cache()
    return acct_auth._find(acct_auth.load_accounts(db), account_id)
