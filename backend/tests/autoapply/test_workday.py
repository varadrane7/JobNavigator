"""Workday submitter against a look-alike tenant: account creation, saved-login sign-in, email
verification parking, and that a dry run never creates an account."""
import json
import os

import pytest

from backend.tests.autoapply.test_submitters import site, no_pauses, _planner, Fake, packet  # noqa: F401
from backend.tests.autoapply.test_docs_pdf import _chromium_available

pytestmark = pytest.mark.skipif(not _chromium_available(), reason="no Playwright Chromium")


async def _run(url, packet, planner, logins=None, dry_run=True):
    from backend.autoapply.runner import HeadlessBrowser
    from backend.autoapply.submitters.workday import Workday
    pkt = packet(url, dry_run=dry_run)
    saved = {}
    pkt.get_login = lambda host: logins.get(host) if logins else None
    pkt.save_login = lambda host, email, pw: saved.update(host=host, email=email, pw=pw)
    async with HeadlessBrowser() as page:
        out = await Workday().run(page, pkt, planner)
        acct = await page.evaluate("() => localStorage.getItem('wd_account')")
        sub = await page.evaluate("() => localStorage.getItem('submitted')")
    return out, saved, json.loads(acct) if acct else None, json.loads(sub) if sub else None


async def test_dry_run_without_login_creates_nothing(site, packet):  # noqa: F811
    planner, _ = _planner(Fake(choice=0))
    out, saved, acct, sub = await _run(f"{site}/wd/job/", packet, planner)
    assert out.status == "dry_run_ok", (out.error, out.steps)
    assert saved == {} and acct is None and sub is None
    assert any("would create an account" in s for s in out.steps)


async def test_creates_account_and_submits(site, packet):  # noqa: F811
    planner, _ = _planner(Fake(choice=0))
    out, saved, acct, sub = await _run(f"{site}/wd/job/", packet, planner, dry_run=False)
    assert out.status == "submitted", (out.error, out.steps)
    assert saved["email"] == "ada@example.com" and len(saved["pw"]) >= 16
    assert acct == {"email": "ada@example.com", "pw": saved["pw"]}      # the stored password is the one used
    assert sub["first"] == "Ada" and sub["last"] == "Dev" and sub["auth"] == "Yes"
    assert sub["resume"] == "Ada_Resume.pdf"
    assert "submitted" in out.confirmation_text.lower()


async def test_signs_in_with_saved_login(site, packet):  # noqa: F811
    planner, _ = _planner(Fake(choice=0))
    url = f"{site}/wd/job/?seed=ada@example.com:Secret-1234"
    out, saved, acct, sub = await _run(url, packet, planner,
                                       logins={"127.0.0.1": ("ada@example.com", "Secret-1234")}, dry_run=False)
    assert out.status == "submitted", (out.error, out.steps)
    assert saved == {} and sub["first"] == "Ada"


async def test_wrong_saved_password_parks(site, packet):  # noqa: F811
    planner, _ = _planner(Fake(choice=0))
    url = f"{site}/wd/job/?seed=ada@example.com:Secret-1234"
    out, *_ = await _run(url, packet, planner, logins={"127.0.0.1": ("ada@example.com", "nope")}, dry_run=False)
    assert out.status == "needs_human" and "login failed" in out.error


async def test_email_verification_parks(site, packet):  # noqa: F811
    planner, _ = _planner(Fake(choice=0))
    out, saved, acct, sub = await _run(f"{site}/wd/job/?verify=1", packet, planner, dry_run=False)
    assert out.status == "needs_human" and "verif" in out.error.lower()
    assert saved["email"] == "ada@example.com" and sub is None        # login kept for the retry


def test_credentials_roundtrip(test_db, tmp_path):
    from backend.autoapply import credentials
    credentials.save(test_db, str(tmp_path), "acme.wd5.myworkdayjobs.com", "a@b.co", "pw-1")
    assert credentials.load(test_db, str(tmp_path), "acme.wd5.myworkdayjobs.com") == ("a@b.co", "pw-1")
    from backend.autoapply.models import AutoApplyCredential
    assert "pw-1" not in test_db.query(AutoApplyCredential).first().password_enc
    assert oct(os.stat(tmp_path / ".secret_key").st_mode & 0o777) == "0o600"
    assert credentials.load(test_db, str(tmp_path), "other") is None
    assert credentials.listing(test_db)[0]["host"] == "acme.wd5.myworkdayjobs.com"
    assert credentials.delete(test_db, "acme.wd5.myworkdayjobs.com") is True


def test_new_password_rules():
    from backend.autoapply.credentials import new_password
    pw = new_password()
    assert len(pw) == 20 and any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw)
