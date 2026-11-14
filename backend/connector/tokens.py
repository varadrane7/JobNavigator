"""Signed connector tokens.

Every value the connector hands out is `sign_cookie(install secret, payload)`, told apart by
its kind ("k"). An account's tokens die together when it bumps its revocation counter
("g", the `connector_epoch` setting), changes its PIN ("p") or is removed.

A connector token is not an account API token: it opens the tools in tools.py and nothing
else, and an account API token does not open /mcp.
"""
import os
import secrets
import time
from typing import Optional

from backend.accounts import auth as acct_auth

KIND_ACCESS = "mcp"            # bearer for /mcp
KIND_REFRESH = "mcp_rt"
KIND_CODE = "mcp_code"         # authorization code
KIND_CLIENT = "mcp_client"     # a dynamically registered client: its client_id is this value
KIND_CONSENT = "mcp_consent"   # binds the consent form to the account that saw it

SCOPES = ("read", "write")     # write includes read
ACCESS_SECONDS = 60 * 60
REFRESH_SECONDS = 60 * 60 * 24 * 90
STATIC_SECONDS = 60 * 60 * 24 * 365      # a token made in Settings
CODE_SECONDS = 120
CLIENT_SECONDS = 60 * 60 * 24 * 365 * 5
CONSENT_SECONDS = 15 * 60
EPOCH_KEY = "connector_epoch"

_used_codes: dict[str, float] = {}       # code nonce -> expiry; one process, so memory is enough


def _now() -> int:
    return int(time.time())


def sign(db, kind: str, ttl: int, **fields) -> str:
    return acct_auth.sign_cookie(acct_auth.cookie_secret(db), {"k": kind, "e": _now() + ttl, **fields})


def read(db, kind: str, value) -> Optional[dict]:
    """The payload of a well-signed, unexpired value of this kind, else None."""
    payload = acct_auth.read_cookie(acct_auth.cookie_secret(db), value.strip() if isinstance(value, str) else None)
    return payload if payload is not None and payload.get("k") == kind else None


# ── Revocation counter ───────────────────────────────────────────────────────

def epoch(db, account_id) -> int:
    from backend.accounts.settings import _account_row
    row = _account_row(db, account_id, EPOCH_KEY)
    try:
        return int(row.value) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def revoke_all(db, account_id) -> int:
    """Every connector token of this account stops working. Commits."""
    from backend.accounts.settings import put_setting
    value = epoch(db, account_id) + 1
    put_setting(db, EPOCH_KEY, str(value), account_id=account_id,
                description="Connector tokens issued before this count are refused")
    db.commit()
    return value


# ── Account-bound tokens ─────────────────────────────────────────────────────

def _bound(db, account: dict, scope: str, client: str) -> dict:
    return {"a": str(account["id"]), "p": acct_auth.pin_fingerprint(account["pin_hash"]),
            "g": epoch(db, account["id"]), "s": scope if scope in SCOPES else "read", "c": (client or "")[:80]}


def account_of(db, payload: Optional[dict]) -> Optional[dict]:
    """The account a token payload still speaks for, else None."""
    if payload is None:
        return None
    acc = acct_auth._find(acct_auth.cached_accounts(db), payload.get("a"))
    if acc is None or payload.get("p", "") != acct_auth.pin_fingerprint(acc["pin_hash"]):
        return None
    if payload.get("g") != epoch(db, acc["id"]):
        return None
    return acc


def make_access(db, account: dict, scope: str, client: str, ttl: int = ACCESS_SECONDS) -> str:
    return sign(db, KIND_ACCESS, ttl, **_bound(db, account, scope, client))


def make_refresh(db, account: dict, scope: str, client: str, client_id: str) -> str:
    return sign(db, KIND_REFRESH, REFRESH_SECONDS, cid=client_key(client_id), **_bound(db, account, scope, client))


def make_code(db, account: dict, scope: str, client: str, client_id: str, redirect_uri: str,
              challenge: str) -> str:
    return sign(db, KIND_CODE, CODE_SECONDS, cid=client_key(client_id), r=redirect_uri, ch=challenge,
                j=secrets.token_urlsafe(12), **_bound(db, account, scope, client))


def use_code(payload: dict) -> bool:
    """False when this code was already exchanged."""
    now = time.time()
    for nonce in [n for n, exp in _used_codes.items() if exp < now]:
        _used_codes.pop(nonce, None)
    nonce = payload.get("j") or ""
    if not nonce or nonce in _used_codes:
        return False
    _used_codes[nonce] = float(payload.get("e") or now) + 5
    return True


def client_key(client_id: str) -> str:
    """A short stand-in for a client_id inside other tokens (a client_id is itself long)."""
    import hashlib
    return hashlib.sha256((client_id or "").encode()).hexdigest()[:24]


# ── Where the install is reachable ───────────────────────────────────────────

def public_base(request) -> str:
    """Scheme and host clients reach this install at. JN_PUBLIC_URL wins, then the URL
    already set for sign-in or Gmail (TLS ends in front of Caddy there), then what the
    proxy forwarded."""
    for name in ("JN_PUBLIC_URL", "OIDC_PUBLIC_URL", "GMAIL_PUBLIC_URL"):
        value = (os.getenv(name) or "").strip().rstrip("/")
        if value:
            return value
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host.split(',')[0].strip()}"
