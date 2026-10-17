"""The tailored-PDF footer ("{Name} - {Job Title} - {Company}"), from #24 by @mrAlishah.

Opt-in in 3.0: it prints only when the copy's `json_data.footer_enabled` is exactly True,
and only on a résumé tailored against a real, fully-known Job of the current account.
A base résumé, a freeform tailor and every existing copy without the flag export as before.
The footer sits in the bottom gutter every template already reserves in `@page` (#19), so
no template carries footer markup or extra margins.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.accounts.context import use_account
from backend.api.routes_resumes import (
    _footer_html_template, _resume_footer_text, _tailored_resume_footer_text, export_pdf,
)
from backend.models.db import Account, Job, Resume
from backend.tests.r4_support import client, make_job  # noqa: F401

CANDIDATE = "Dana Okonkwo"
TITLE = "Senior Product Manager"
COMPANY = "Acme Corp"
FOOTER = f"{CANDIDATE} - {TITLE} - {COMPANY}"


def _job(**overrides):
    defaults = dict(id=uuid.uuid4(), external_id=uuid.uuid4().hex, title=TITLE, company=COMPANY)
    defaults.update(overrides)
    return Job(**defaults)


def _resume(**overrides):
    defaults = dict(id=uuid.uuid4(), name="Tailored", is_base=False, json_data={"header": {"name": CANDIDATE}})
    defaults.update(overrides)
    return Resume(**defaults)


# ── the text rule (the author's tests) ───────────────────────────────────────

def test_job_linked_tailored_resume_gets_the_footer():
    job = _job()
    assert _tailored_resume_footer_text(_resume(job_id=job.id), job) == FOOTER


def test_footer_uses_ascii_hyphen_only():
    job = _job()
    text = _tailored_resume_footer_text(_resume(job_id=job.id), job)
    assert " - " in text
    for dash in ("–", "—", "−"):  # en dash, em dash, minus sign
        assert dash not in text


def test_base_resume_gets_no_footer():
    assert _tailored_resume_footer_text(_resume(is_base=True, job_id=None), None) == ""


def test_freeform_tailor_without_a_job_gets_no_footer():
    resume = _resume(job_id=None, json_data={"header": {"name": CANDIDATE},
                                             "_tailor_context": {"job_description": "some JD", "source": "freeform"}})
    assert _tailored_resume_footer_text(resume, None) == ""


def test_missing_candidate_name_suppresses_the_footer():
    job = _job()
    assert _tailored_resume_footer_text(_resume(job_id=job.id, json_data={"header": {"name": ""}}), job) == ""
    assert _tailored_resume_footer_text(_resume(job_id=job.id, json_data={"header": None}), job) == ""


def test_missing_job_title_suppresses_the_footer():
    job = _job(title=None)
    assert _tailored_resume_footer_text(_resume(job_id=job.id), job) == ""


def test_missing_company_suppresses_the_footer():
    job = _job(company="")
    assert _tailored_resume_footer_text(_resume(job_id=job.id), job) == ""


def test_job_linked_resume_with_no_job_row_found_gets_no_footer():
    assert _tailored_resume_footer_text(_resume(job_id=uuid.uuid4()), None) == ""


def test_footer_html_escapes_the_candidate_and_job_text():
    job = _job(title="<script>alert(1)</script>")
    html = _footer_html_template(_tailored_resume_footer_text(_resume(job_id=job.id), job))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_empty_footer_text_renders_no_visible_markup():
    assert "font-size" not in _footer_html_template("")


# ── the opt-in (shared by every export format) ───────────────────────────────

@pytest.mark.parametrize("flag", [None, False, "true", 1, "yes"])
def test_footer_is_off_unless_the_flag_is_exactly_true(test_db, flag):
    job = make_job(test_db, title=TITLE, company=COMPANY)
    data = {"header": {"name": CANDIDATE}}
    if flag is not None:
        data["footer_enabled"] = flag
    resume = _resume(job_id=job.id, json_data=data)
    assert _resume_footer_text(test_db, resume) == ""


def test_footer_text_for_an_enabled_job_linked_copy(test_db):
    job = make_job(test_db, title=TITLE, company=COMPANY)
    resume = _resume(job_id=job.id, json_data={"header": {"name": CANDIDATE}, "footer_enabled": True})
    assert _resume_footer_text(test_db, resume) == FOOTER


def test_footer_never_reads_another_accounts_job(test_db):
    b = Account(name="Partner", role="member")
    test_db.add(b)
    test_db.commit()
    with use_account(b.id):
        b_job = make_job(test_db, title="B role", company="BeeCo")
    resume = _resume(job_id=b_job.id, json_data={"header": {"name": CANDIDATE}, "footer_enabled": True})
    assert _resume_footer_text(test_db, resume) == ""


# ── /resumes/{id}/pdf wiring (Playwright stubbed) ────────────────────────────

def _fake_browser(monkeypatch, captured: dict):
    fake_page = MagicMock()

    async def fake_set_content(html, *a, **kw):
        captured["html"] = html

    async def fake_pdf(**kwargs):
        captured.update(kwargs)
        return b"%PDF-1.4\n/Type /Page\n"

    fake_page.set_content = AsyncMock(side_effect=fake_set_content)
    fake_page.pdf = AsyncMock(side_effect=fake_pdf)
    fake_page.close = AsyncMock(return_value=None)
    fake_context = MagicMock()
    fake_context.new_page = AsyncMock(return_value=fake_page)
    fake_context.route = AsyncMock(return_value=None)
    fake_context.close = AsyncMock(return_value=None)
    fake_browser = MagicMock()
    fake_browser.new_context = AsyncMock(return_value=fake_context)

    async def fake_get_browser():
        return fake_browser

    import backend.api.routes_resumes as routes_resumes
    monkeypatch.setattr(routes_resumes, "_get_browser", fake_get_browser)


def _add(test_db, resume):
    test_db.add(resume)
    test_db.commit()
    return resume


def _assert_no_footer(captured):
    assert captured["display_header_footer"] is False
    assert captured["footer_template"] == "<span></span>"
    assert captured["margin"]["bottom"] == "0"


@pytest.mark.asyncio
async def test_pdf_export_adds_footer_when_switched_on(test_db, monkeypatch):
    job = make_job(test_db, title=TITLE, company=COMPANY)
    resume = _add(test_db, _resume(job_id=job.id, json_data={"header": {"name": CANDIDATE}, "footer_enabled": True}))
    captured = {}
    _fake_browser(monkeypatch, captured)

    await export_pdf(str(resume.id), db=test_db)

    assert captured["display_header_footer"] is True
    assert FOOTER in captured["footer_template"]
    assert captured["header_template"] == "<span></span>"
    # The footer lands in the gutter the template's @page already reserves: no extra page margin.
    assert captured["margin"]["bottom"] == "0"
    assert FOOTER not in captured["html"]


@pytest.mark.asyncio
async def test_existing_tailored_copy_without_the_flag_renders_no_footer(test_db, monkeypatch):
    """Every copy made before the port has no `footer_enabled` key: its PDF stays as it was."""
    job = make_job(test_db, title=TITLE, company=COMPANY)
    resume = _add(test_db, _resume(job_id=job.id))
    captured = {}
    _fake_browser(monkeypatch, captured)
    await export_pdf(str(resume.id), db=test_db)
    _assert_no_footer(captured)


@pytest.mark.asyncio
async def test_pdf_export_respects_a_switched_off_footer(test_db, monkeypatch):
    job = make_job(test_db, title=TITLE, company=COMPANY)
    resume = _add(test_db, _resume(job_id=job.id, json_data={"header": {"name": CANDIDATE}, "footer_enabled": False}))
    captured = {}
    _fake_browser(monkeypatch, captured)
    await export_pdf(str(resume.id), db=test_db)
    _assert_no_footer(captured)


@pytest.mark.asyncio
async def test_base_resume_never_renders_a_footer(test_db, monkeypatch):
    """Even a base that somehow carries the flag (and a job id) prints none."""
    job = make_job(test_db, title=TITLE, company=COMPANY)
    resume = _add(test_db, _resume(is_base=True, job_id=job.id,
                                   json_data={"header": {"name": CANDIDATE}, "footer_enabled": True}))
    captured = {}
    _fake_browser(monkeypatch, captured)
    await export_pdf(str(resume.id), db=test_db)
    _assert_no_footer(captured)


@pytest.mark.asyncio
async def test_freeform_tailor_never_renders_a_footer(test_db, monkeypatch):
    resume = _add(test_db, _resume(job_id=None, json_data={
        "header": {"name": CANDIDATE}, "footer_enabled": True,
        "_tailor_context": {"job_description": "some JD", "source": "freeform"}}))
    captured = {}
    _fake_browser(monkeypatch, captured)
    await export_pdf(str(resume.id), db=test_db)
    _assert_no_footer(captured)


# ── through the route, per account ───────────────────────────────────────────

def test_pdf_route_prints_the_footer_for_an_own_copy(client, test_db, monkeypatch):
    job = make_job(test_db, title=TITLE, company=COMPANY)
    resume = _add(test_db, _resume(job_id=job.id, json_data={"header": {"name": CANDIDATE}, "footer_enabled": True}))
    captured = {}
    _fake_browser(monkeypatch, captured)
    r = client.get(f"/api/resumes/{resume.id}/pdf")
    assert r.status_code == 200, r.text
    assert FOOTER in captured["footer_template"]


def test_pdf_route_is_404_for_another_accounts_resume(client, test_db, monkeypatch):
    b = Account(name="Partner", role="member")
    test_db.add(b)
    test_db.commit()
    with use_account(b.id):
        b_job = make_job(test_db, title="B role", company="BeeCo")
        b_resume = _add(test_db, _resume(job_id=b_job.id, json_data={"header": {"name": "Bee"}, "footer_enabled": True}))
        b_id = b_resume.id
    test_db.expunge_all()
    captured = {}
    _fake_browser(monkeypatch, captured)
    for rid in (str(b_id), "not-a-uuid", str(uuid.uuid4())):
        r = client.get(f"/api/resumes/{rid}/pdf")
        assert r.status_code == 404, (rid, r.status_code, r.text)
    assert captured == {}   # nothing was rendered


def test_patch_stores_the_flag_and_the_route_follows_it(client, test_db, monkeypatch):
    """The editor's autosave writes json_data wholesale; the next export reads it."""
    job = make_job(test_db, title=TITLE, company=COMPANY)
    resume = _add(test_db, _resume(job_id=job.id))
    captured = {}
    _fake_browser(monkeypatch, captured)
    r = client.patch(f"/api/resumes/{resume.id}", json={"json_data": {"header": {"name": CANDIDATE}, "footer_enabled": True}})
    assert r.status_code == 200 and r.json()["json_data"]["footer_enabled"] is True
    client.get(f"/api/resumes/{resume.id}/pdf")
    assert captured["display_header_footer"] is True
    r = client.patch(f"/api/resumes/{resume.id}", json={"json_data": {"header": {"name": CANDIDATE}, "footer_enabled": False}})
    captured.clear()
    client.get(f"/api/resumes/{resume.id}/pdf")
    assert captured["display_header_footer"] is False
