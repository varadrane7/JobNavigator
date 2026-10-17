"""MT-20 · OIDC sign-in: discovery + PKCE login, ID token checks, who a login becomes, and
that an OIDC browser stays in its own account.

The provider is faked: discovery/JWKS reads (`oidc._get_json`) and the code exchange
(`oidc.exchange_code`) are patched, and the ID tokens are really signed (RS256) with a
throwaway key, so the signature, issuer, audience, expiry and nonce checks run for real.
"""
import json
import time
import urllib.parse as up

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.accounts import admin, oidc
from backend.accounts import auth as A
from backend.models.db import Account, SectionPolicy
from backend.tests.r4_support import API_KEY, client  # noqa: F401 (fixture)

ISSUER = "https://auth.example.com/application/o/jn/"
CLIENT_ID = "jn-client"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
JWK.update({"kid": "k1", "alg": "RS256", "use": "sig"})
DOC = {"issuer": ISSUER, "authorization_endpoint": ISSUER + "authorize/",
       "token_endpoint": ISSUER + "token/", "jwks_uri": ISSUER + "jwks/",
       "userinfo_endpoint": ISSUER + "userinfo/"}


@pytest.fixture
def idp(client, monkeypatch):
    """OIDC on, a fake provider, a browser with no account token."""
    monkeypatch.setenv("OIDC_DISCOVERY_URL", ISSUER + ".well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("OIDC_OWNER_EMAIL", "owner@example.com")
    oidc.forget_provider_cache()

    async def get_json(url, **kw):
        if url.endswith("jwks/"):
            return {"keys": [JWK]}
        if url.endswith("userinfo/"):
            return {}
        return DOC
    monkeypatch.setattr(oidc, "_get_json", get_json)
    client.headers.pop("X-Test-Act-As", None)
    client.headers.pop("X-API-Key", None)
    client.cookies.clear()
    client.next_claims = {}
    return client


def _token(expected_nonce, **over):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "sub-1", "iat": now, "exp": now + 300, "nonce": expected_nonce,
              "email": "owner@example.com", "email_verified": True, "name": "Owner Person"}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


def _login(c, monkeypatch, **claims):
    """Run login + callback for an identity; returns the callback response."""
    r = c.get("/api/auth/oidc/login", follow_redirects=False)
    assert r.status_code == 302
    q = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
    assert q["code_challenge_method"] == "S256" and q["client_id"] == CLIENT_ID

    async def exchange(cfg, doc, code, verifier, base):
        assert oidc._challenge(verifier) == q["code_challenge"]
        return {"id_token": _token(q["nonce"], **claims), "access_token": "at"}
    monkeypatch.setattr(oidc, "exchange_code", exchange)
    return c.get("/api/auth/oidc/callback", params={"code": "abc", "state": q["state"]}, follow_redirects=False)


def _members(test_db):
    return test_db.query(Account).filter(Account.role == "member").all()


# ── config ───────────────────────────────────────────────────────────────────

def test_config_off_without_env(client, monkeypatch):
    for k in ("OIDC_DISCOVERY_URL", "OIDC_CLIENT_ID"):
        monkeypatch.delenv(k, raising=False)
    client.headers.pop("X-API-Key", None)
    assert client.get("/api/auth/oidc/config").json()["enabled"] is False
    assert client.get("/api/auth/oidc/login", follow_redirects=False).status_code == 404


def test_config_on(idp):
    assert idp.get("/api/auth/oidc/config").json()["enabled"] is True


# ── mapping ──────────────────────────────────────────────────────────────────

def test_owner_email_links_the_owner_row(idp, test_db, monkeypatch):
    r = _login(idp, monkeypatch)
    assert r.status_code == 302 and r.headers["location"] == "/"
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    test_db.refresh(owner)
    assert owner.oidc_sub == "sub-1" and owner.oidc_issuer == ISSUER
    assert not _members(test_db)
    me = idp.get("/api/auth/me").json()
    assert me["account"]["role"] == "owner"


def test_first_login_creates_a_member_on_own_credentials(idp, test_db, monkeypatch):
    r = _login(idp, monkeypatch, sub="sub-2", email="amy@example.com", name="Amy")
    assert r.headers["location"] == "/"
    [m] = _members(test_db)
    assert m.name == "Amy" and m.oidc_sub == "sub-2"
    modes = {p.section: p.mode for p in test_db.query(SectionPolicy).filter(SectionPolicy.account_id == m.id)}
    assert modes == {s: "own" for s in admin.OWN_BY_DEFAULT_SECTIONS}
    assert idp.get("/api/auth/me").json()["account"]["id"] == str(m.id)


def test_second_login_reuses_the_account(idp, test_db, monkeypatch):
    _login(idp, monkeypatch, sub="sub-2", email="amy@example.com", name="Amy")
    idp.cookies.clear()
    _login(idp, monkeypatch, sub="sub-2", email="amy@example.com", name="Amy")
    assert len(_members(test_db)) == 1


def test_account_limit(idp, test_db, monkeypatch):
    for i in range(admin.MAX_ACCOUNTS - 1):   # owner + 4 members = full
        test_db.add(Account(name=f"M{i}", role="member", color="#2e9e6b"))
    test_db.commit()
    r = _login(idp, monkeypatch, sub="sub-9", email="late@example.com", name="Late")
    assert r.headers["location"] == "/?oidc_error=account_limit"
    assert "jn_account" not in r.cookies and "jn_session" not in r.cookies


def test_unverified_owner_email_is_refused_not_made_a_member(idp, test_db, monkeypatch):
    r = _login(idp, monkeypatch, sub="evil", email="owner@example.com", email_verified=False)
    assert r.headers["location"] == "/?oidc_error=email_unverified"
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    test_db.refresh(owner)
    assert owner.oidc_sub is None and not _members(test_db)


def test_trust_email_links_the_owner_despite_unverified(idp, test_db, monkeypatch):
    monkeypatch.setenv("OIDC_TRUST_EMAIL", "true")
    r = _login(idp, monkeypatch, sub="sub-1", email="owner@example.com", email_verified=False)
    assert r.headers["location"] == "/"
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    test_db.refresh(owner)
    assert owner.oidc_sub == "sub-1" and not _members(test_db)


def test_owner_already_linked_to_someone_else(idp, test_db, monkeypatch):
    _login(idp, monkeypatch)
    idp.cookies.clear()
    r = _login(idp, monkeypatch, sub="other-sub")   # same owner email, other identity
    assert r.headers["location"] == "/?oidc_error=owner_conflict"


def test_allowed_groups(idp, monkeypatch):
    monkeypatch.setenv("OIDC_ALLOWED_GROUPS", "jn-users")
    assert _login(idp, monkeypatch, sub="s3", email="x@example.com", groups=["other"]).headers["location"] \
        == "/?oidc_error=not_allowed"
    idp.cookies.clear()
    assert _login(idp, monkeypatch, sub="s4", email="y@example.com", groups=["JN-Users"]).headers["location"] == "/"


# ── token checks ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("over", [
    {"aud": "someone-else"}, {"iss": "https://evil.example.com/"}, {"exp": int(time.time()) - 600},
    {"nonce": "wrong"}, {"sub": None}])
def test_bad_tokens_are_refused(idp, monkeypatch, over):
    r = _login(idp, monkeypatch, **over)
    assert r.headers["location"] == "/?oidc_error=token_invalid"
    assert "jn_session" not in r.cookies and "jn_account" not in r.cookies


def test_token_signed_with_another_key_is_refused(idp, monkeypatch):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    r = idp.get("/api/auth/oidc/login", follow_redirects=False)
    q = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
    now = int(time.time())
    forged = jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "iat": now, "exp": now + 99,
                         "nonce": q["nonce"], "email": "a@example.com"}, other, algorithm="RS256",
                        headers={"kid": "k1"})

    async def exchange(*a, **k):
        return {"id_token": forged}
    monkeypatch.setattr(oidc, "exchange_code", exchange)
    r = idp.get("/api/auth/oidc/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/?oidc_error=token_invalid"


def test_alg_none_is_refused(idp, monkeypatch):
    r = idp.get("/api/auth/oidc/login", follow_redirects=False)
    q = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
    forged = jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "iat": 1, "exp": 9999999999,
                         "nonce": q["nonce"]}, None, algorithm="none")

    async def exchange(*a, **k):
        return {"id_token": forged}
    monkeypatch.setattr(oidc, "exchange_code", exchange)
    r = idp.get("/api/auth/oidc/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/?oidc_error=token_invalid"


def test_state_must_match_the_cookie(idp):
    idp.get("/api/auth/oidc/login", follow_redirects=False)
    r = idp.get("/api/auth/oidc/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert r.headers["location"] == "/?oidc_error=bad_state"


def test_callback_without_login_cookie(idp):
    r = idp.get("/api/auth/oidc/callback", params={"code": "c", "state": "x"}, follow_redirects=False)
    assert r.headers["location"] == "/?oidc_error=bad_state"


# ── an OIDC browser stays in its own account ─────────────────────────────────

def test_oidc_browser_cannot_switch_mint_tokens_or_unlock(idp, test_db, monkeypatch):
    _login(idp, monkeypatch)   # owner
    idp.cookies.clear()
    _login(idp, monkeypatch, sub="sub-2", email="amy@example.com", name="Amy")
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    assert idp.post("/api/auth/switch", json={"account_id": str(owner.id)}).status_code == 403
    assert idp.post("/api/auth/verify-pin", json={"account_id": str(owner.id)}).status_code == 403
    assert idp.post("/api/auth/verify-owner-pin", json={}).status_code == 403
    assert [a["role"] for a in idp.get("/api/auth/accounts").json()] == ["member"]
    assert idp.post(f"/api/accounts/{owner.id}/api-token").status_code in (401, 403)


def test_oidc_flag_survives_a_pin_change(idp, test_db, monkeypatch):
    _login(idp, monkeypatch, sub="sub-2", email="amy@example.com", name="Amy")
    [m] = _members(test_db)
    r = idp.patch(f"/api/accounts/{m.id}", json={"pin": "1234"})
    assert r.status_code == 200
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    assert idp.post("/api/auth/switch", json={"account_id": str(owner.id)}).status_code == 403


def test_account_token_stands_in_for_the_install_key_when_oidc_is_on(idp, test_db, monkeypatch):
    _login(idp, monkeypatch, sub="sub-2", email="amy@example.com", name="Amy")
    [m] = _members(test_db)
    token = idp.post(f"/api/accounts/{m.id}/api-token").json()["token"]
    idp.cookies.clear()
    r = idp.get("/api/auth/me", headers={"X-Account-Token": token})
    assert r.status_code == 200 and r.json()["account"]["id"] == str(m.id)
    assert idp.get("/api/auth/me").status_code == 401


def test_account_token_is_not_an_install_key_when_oidc_is_off(client, test_db, monkeypatch):
    for k in ("OIDC_DISCOVERY_URL", "OIDC_CLIENT_ID"):
        monkeypatch.delenv(k, raising=False)
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    token = A.make_api_token(test_db, A.load_accounts(test_db)[0])
    client.headers.pop("X-API-Key", None)
    client.headers.pop("X-Test-Act-As", None)
    assert client.get("/api/auth/me", headers={"X-Account-Token": token}).status_code == 401


def test_break_glass_install_password_still_works(idp):
    r = idp.post("/api/auth/set-session", json={"api_key": API_KEY})
    assert r.status_code == 200
