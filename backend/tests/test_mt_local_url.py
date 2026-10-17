"""MT-21 · a member's own local model server: owner permission and the address check."""
import socket

import pytest

from backend.accounts import local_urls as L
from backend.accounts.context import use_account
from backend.analyzer import llm_client as lc
from backend.models.db import Account, SessionLocal
from backend.tests.r4_support import client  # noqa: F401 (fixture)


@pytest.fixture
def fake_dns(monkeypatch):
    table = {"gpu.lan": "192.168.1.50", "box.local": "10.0.0.7", "evil.example": "93.184.216.34",
             "meta.example": "169.254.169.254", "lo.example": "127.0.0.1"}
    real = socket.getaddrinfo

    def gai(host, port, *a, **k):
        if host in table:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port or 0))]
        return real(host, port, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", gai)


@pytest.mark.parametrize("url,ok", [
    ("http://gpu.lan:11434", True), ("https://box.local/v1", True), ("http://192.168.1.5:1234/v1", True),
    ("http://127.0.0.1:1234/v1", True), ("http://[::1]:1234", True), ("http://172.17.0.1:11434", True),
    ("http://evil.example", False), ("http://8.8.8.8", False), ("http://meta.example", False),
    ("http://169.254.169.254/latest", False), ("http://0.0.0.0:80", False), ("http://[fe80::1]", False),
    ("ftp://gpu.lan", False), ("gpu.lan:11434", False), ("http://user:pw@gpu.lan", False), ("", False),
    ("http://[::ffff:169.254.169.254]/", False)])
def test_check_url(fake_dns, url, ok):
    assert (L.check_url(url) is None) is ok


@pytest.fixture
def member(test_db):
    m = Account(name="Partner", role="member", color="#2e9e6b")
    test_db.add(m)
    test_db.commit()
    return m


def test_owner_toggles_permission(client, test_db, member):
    r = client.patch(f"/api/accounts/{member.id}", json={"allow_local_url": True})
    assert r.status_code == 200 and r.json()["account"]["allow_local_url"] is True
    assert L.is_allowed(test_db, member.id)
    rows = {a["id"]: a for a in client.get("/api/accounts").json()["accounts"]}
    assert rows[str(member.id)]["allow_local_url"] is True
    client.patch(f"/api/accounts/{member.id}", json={"allow_local_url": False})
    assert not L.is_allowed(test_db, member.id)


def test_member_cannot_grant_itself(client, test_db, member):
    client.headers["X-Test-Act-As"] = str(member.id)
    assert client.patch(f"/api/accounts/{member.id}", json={"allow_local_url": True}).status_code == 403
    assert not L.is_allowed(test_db, member.id)


def test_member_write_needs_permission_and_a_local_host(client, test_db, member, fake_dns):
    client.headers["X-Test-Act-As"] = str(member.id)
    url = {"member_ollama_base_url": "http://gpu.lan:11434"}
    assert client.patch("/api/settings", json=url).status_code == 403          # not allowed yet
    L.set_allowed(test_db, member.id, True)
    test_db.commit()
    assert client.patch("/api/settings", json=url).status_code == 200
    bad = client.patch("/api/settings", json={"member_ollama_base_url": "http://meta.example"})
    assert bad.status_code == 400 and "link-local" in bad.text
    assert client.patch("/api/settings", json={"member_ollama_base_url": "http://evil.example"}).status_code == 400


def test_member_url_is_used_only_when_allowed(client, test_db, member, fake_dns):
    client.headers["X-Test-Act-As"] = str(member.id)
    L.set_allowed(test_db, member.id, True)
    test_db.commit()
    client.patch("/api/settings", json={"member_ollama_base_url": "http://gpu.lan:11434",
                                        "member_llm_local_base_url": "http://box.local:1234/v1"})
    with use_account(member.id):
        assert lc.local_base_url("ollama") == "http://gpu.lan:11434"
        assert lc._local_base_url() == "http://box.local:1234/v1"
        L.set_allowed(test_db, member.id, False)
        test_db.commit()
        assert lc.local_base_url("ollama") != "http://gpu.lan:11434"      # back to the install's server
        assert lc._local_base_url() != "http://box.local:1234/v1"


def test_a_stored_url_that_now_resolves_publicly_fails_the_call(client, test_db, member, monkeypatch, fake_dns):
    client.headers["X-Test-Act-As"] = str(member.id)
    L.set_allowed(test_db, member.id, True)
    test_db.commit()
    client.patch("/api/settings", json={"member_ollama_base_url": "http://gpu.lan:11434"})
    monkeypatch.setattr(L, "check_url", lambda u: "must be a local or private-network address (public hosts are not allowed)")
    with use_account(member.id):
        with pytest.raises(lc.NonRetryableLLMError):
            lc.local_base_url("ollama")


def test_members_own_url_never_reaches_the_owner(client, test_db, member, fake_dns):
    client.headers["X-Test-Act-As"] = str(member.id)
    L.set_allowed(test_db, member.id, True)
    test_db.commit()
    client.patch("/api/settings", json={"member_ollama_base_url": "http://gpu.lan:11434"})
    client.headers.pop("X-Test-Act-As")
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    with use_account(owner.id):
        assert lc.local_base_url("ollama") != "http://gpu.lan:11434"
