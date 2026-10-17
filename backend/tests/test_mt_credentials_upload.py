"""MT-21 · CLI login uploads: where the files land, who may upload, hostile archives."""
import io
import json
import os
import zipfile

import pytest

from backend.accounts import credentials as C
from backend.accounts import settings as S
from backend.accounts.context import use_account
from backend.models.db import Account, SectionPolicy
from backend.tests.r4_support import client  # noqa: F401 (fixture)
from backend.tests import r4_support

GOOD = json.dumps({"OPENAI_API_KEY": None, "tokens": {"id_token": "x", "access_token": "y", "refresh_token": "z"}})


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CODEX_ACCOUNTS_ROOT", str(tmp_path / "codex_accounts"))
    monkeypatch.setattr(C, "AGY_ACCOUNTS_ROOT", str(tmp_path / "agy_accounts"))
    from backend.api import routes_credentials as R
    monkeypatch.setattr(R, "DEFAULT_CODEX_HOME", str(tmp_path / "default_codex"))
    monkeypatch.setattr(R, "DEFAULT_AGY_HOME", str(tmp_path / "default_home"))
    return tmp_path


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _member(test_db, mode=None):
    m = Account(name="Partner", role="member", color="#2e9e6b")
    test_db.add(m)
    test_db.commit()
    if mode:
        test_db.add(SectionPolicy(account_id=m.id, section="models", mode=mode))
        test_db.commit()
    return m


def _as(client, account):
    client.headers["X-Test-Act-As"] = str(account.id)


def test_owner_codex_goes_to_the_default_home(client, homes):
    r = client.put("/api/credentials/codex", json={"auth_json": GOOD})
    assert r.status_code == 200
    path = homes / "default_codex" / "auth.json"
    assert json.loads(path.read_text())["tokens"]["id_token"] == "x"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    st = client.get("/api/credentials").json()
    assert st["scope"] == "install" and st["codex"]["logged_in"] is True
    assert "refresh_token" not in json.dumps(st) and "id_token" not in json.dumps(st)   # nothing is read back


@pytest.mark.parametrize("body", [{}, {"auth_json": ""}, {"auth_json": "nope"},
                                  {"auth_json": json.dumps({"OPENAI_API_KEY": "sk-1"})},
                                  {"auth_json": json.dumps([1])}, {"auth_json": "x" * 70000}])
def test_codex_rejects_bad_input(client, homes, body):
    assert client.put("/api/credentials/codex", json=body).status_code in (413, 422)


def test_member_on_own_gets_a_private_home(client, homes, test_db):
    m = _member(test_db, "own")
    _as(client, m)
    assert client.put("/api/credentials/codex", json={"auth_json": GOOD}).status_code == 200
    assert (homes / "codex_accounts" / str(m.id) / "auth.json").is_file()
    assert not (homes / "default_codex").exists()
    assert client.get("/api/credentials").json()["scope"] == "own"


def test_member_on_inherit_is_refused(client, homes, test_db):
    m = _member(test_db, "inherit")
    _as(client, m)
    assert client.put("/api/credentials/codex", json={"auth_json": GOOD}).status_code == 403
    assert client.put("/api/credentials/agy", content=_zip({"a": "b"})).status_code == 403
    assert client.delete("/api/credentials/codex").status_code == 403
    assert client.get("/api/credentials").json()["scope"] == "inherit"
    assert not (homes / "codex_accounts").exists()


def test_members_do_not_share_homes(client, homes, test_db):
    a = _member(test_db, "own")
    b = Account(name="Other", role="member", color="#d9534f")
    test_db.add(b)
    test_db.commit()
    test_db.add(SectionPolicy(account_id=b.id, section="models", mode="own"))
    test_db.commit()
    _as(client, a)
    client.put("/api/credentials/codex", json={"auth_json": GOOD})
    _as(client, b)
    assert client.get("/api/credentials").json()["codex"]["logged_in"] is False


def test_agy_zip_extracts_under_the_state_dir(client, homes):
    z = _zip({".gemini/antigravity-cli/auth.json": "{}", ".gemini/antigravity-cli/sub/x": "1"})
    r = client.put("/api/credentials/agy", content=z, headers={"Content-Type": "application/zip"})
    assert r.status_code == 200 and r.json()["logged_in"] is True
    assert (homes / "default_home" / ".gemini" / "antigravity-cli" / "sub" / "x").read_text() == "1"
    assert client.get("/api/credentials").json()["agy"]["logged_in"] is True


def test_agy_zip_without_the_gemini_prefix(client, homes):
    z = _zip({"antigravity-cli/auth.json": "{}"})
    assert client.put("/api/credentials/agy", content=z).status_code == 200
    assert (homes / "default_home" / ".gemini" / "antigravity-cli" / "auth.json").is_file()


@pytest.mark.parametrize("name", ["../evil", "a/../../evil", "/abs/evil", "..\\evil"])
def test_agy_zip_slip_is_refused(client, homes, name):
    r = client.put("/api/credentials/agy", content=_zip({name: "x"}))
    assert r.status_code == 422
    assert not list(homes.rglob("evil"))


def test_agy_rejects_non_zip_and_oversize(client, homes):
    assert client.put("/api/credentials/agy", content=b"not a zip").status_code == 422
    assert client.put("/api/credentials/agy", content=b"0" * (11 * 1024 * 1024)).status_code == 413


def test_agy_merge_keeps_unrelated_files(client, homes):
    keep = homes / "default_home" / ".gemini"
    keep.mkdir(parents=True)
    (keep / "other-state").write_text("keep")
    client.put("/api/credentials/agy", content=_zip({"antigravity-cli/a": "1"}))
    assert (keep / "other-state").read_text() == "keep"


def test_delete_removes_the_login(client, homes):
    client.put("/api/credentials/codex", json={"auth_json": GOOD})
    assert client.delete("/api/credentials/codex").status_code == 200
    assert client.get("/api/credentials").json()["codex"]["logged_in"] is False
    assert client.delete("/api/credentials/other").status_code == 404
