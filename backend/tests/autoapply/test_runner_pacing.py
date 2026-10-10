from datetime import date, datetime, timedelta, timezone

import pytest

from backend.autoapply import pacing
from backend.autoapply.models import AutoApplyItem, AutoApplyQuestion
from backend.tests.autoapply.test_docs_pdf import _chromium_available
from backend.tests.autoapply.test_submitters import site, no_pauses, _planner, Fake  # noqa: F401  (fixtures)

NOON_NY = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)   # 12:00 America/New_York


@pytest.fixture(autouse=True)
def _reset_pacing():
    pacing.reset()
    yield
    pacing.reset()


def _item(db, job, status="ready", submitter="greenhouse", rank=1, company_key=None, **kw):
    it = AutoApplyItem(job_id=job.id, run_date=date(2026, 10, 9), rank=rank, submitter=submitter, status=status,
                       company_key=company_key or (job.company or "").lower(), **kw)
    db.add(it)
    db.commit()
    return it


# ── pacing ───────────────────────────────────────────────────────────────────

def test_quiet_hours_wrap_midnight():
    cfg = {"autoapply_quiet_hours": "23-7", "autoapply_timezone": "America/New_York"}
    assert pacing.in_quiet_hours(cfg, datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc))      # 00:00 NY
    assert pacing.in_quiet_hours(cfg, datetime(2026, 10, 10, 10, 30, tzinfo=timezone.utc))    # 06:30 NY
    assert not pacing.in_quiet_hours(cfg, NOON_NY)
    assert not pacing.in_quiet_hours({"autoapply_quiet_hours": ""}, NOON_NY)


def test_pick_next_rules(test_db, make_job, aa_settings):
    from backend.autoapply import settings
    aa_settings(autoapply_site_caps={"greenhouse": 1, "lever": 5, "generic": 5, "linkedin": 5})
    cfg = settings.get_all(test_db)
    a = _item(test_db, make_job("A"), rank=1)
    b = _item(test_db, make_job("B"), rank=2, submitter="lever")
    _item(test_db, make_job("C"), rank=0, submitter="icims")               # no handler yet
    it, why = pacing.pick_next(test_db, cfg, NOON_NY)
    assert it.id == a.id

    # greenhouse cap of 1 used up today -> lever item next
    a.status, a.submitted_at = "submitted", NOON_NY - timedelta(hours=1)
    test_db.commit()
    it, _ = pacing.pick_next(test_db, cfg, NOON_NY)
    assert it.id == b.id

    # jitter gap blocks, then expires
    pacing.mark_done(cfg, a, NOON_NY)
    assert pacing.pick_next(test_db, cfg, NOON_NY + timedelta(seconds=10))[1] == "waiting for jitter gap"
    assert pacing.pick_next(test_db, cfg, NOON_NY + timedelta(seconds=181))[0].id == b.id

    # retry backoff
    b.next_attempt_at = NOON_NY + timedelta(hours=1)
    test_db.commit()
    assert pacing.pick_next(test_db, cfg, NOON_NY + timedelta(seconds=200))[1] == "nothing ready"


def test_pick_next_avoids_same_company_back_to_back(test_db, make_job, aa_settings):
    from backend.autoapply import settings
    cfg = settings.get_all(test_db)
    x1 = _item(test_db, make_job("X"), rank=1, company_key="x")
    y = _item(test_db, make_job("Y"), rank=2, company_key="y")
    pacing._state["last_company"] = "x"
    assert pacing.pick_next(test_db, cfg, NOON_NY)[0].id == y.id
    y.status = "submitted"
    test_db.commit()
    assert pacing.pick_next(test_db, cfg, NOON_NY)[0].id == x1.id   # only option left


def test_daily_cap_counts_submissions(test_db, make_job, aa_settings):
    from backend.autoapply import settings
    aa_settings(autoapply_daily_cap=1)
    cfg = settings.get_all(test_db)
    _item(test_db, make_job("A"), status="submitted", submitted_at=NOON_NY - timedelta(hours=2))
    _item(test_db, make_job("B"))
    assert pacing.pick_next(test_db, cfg, NOON_NY)[1] == "daily cap reached"


# ── runner end-to-end ────────────────────────────────────────────────────────

@pytest.fixture
def ready_item(test_db, make_job, aa_settings, tmp_path, site):  # noqa: F811
    aa_settings(autoapply_data_dir=str(tmp_path), autoapply_enabled=True)
    pdf = tmp_path / "Ada_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%fake\n")
    job = make_job("Acme", url=f"{site}/lever/acme/123")
    return _item(test_db, job, submitter="lever", resume_pdf_path=str(pdf), base_resume_name="SDE")


@pytest.mark.skipif(not _chromium_available(), reason="no Playwright Chromium")
async def test_submit_item_live_writes_application(test_db, ready_item, aa_settings):
    from backend.autoapply.runner import submit_item
    from backend.models.db import Application, Job
    aa_settings(autoapply_dry_run=False)
    planner, _ = _planner(Fake(choice=2))
    r = await submit_item(ready_item.id, planner=planner)
    assert r == "Acme: submitted", r
    test_db.expire_all()
    it = test_db.query(AutoApplyItem).get(ready_item.id)
    assert it.status == "submitted" and it.submitted_at is not None
    app = test_db.query(Application).get(it.application_id)
    assert app.cv_version_used == "SDE" and app.status == "applied" and "AutoApply (lever)" in app.notes
    assert test_db.query(Job).get(it.job_id).status == "applied"
    (att,) = it.attempts_log
    assert att.outcome == "submitted" and att.dry_run is False and att.receipt.get("Full name") == "Ada Dev", att.receipt
    assert len(att.screenshots) == 2


@pytest.mark.skipif(not _chromium_available(), reason="no Playwright Chromium")
async def test_submit_item_needs_human_then_answer_requeues(test_db, ready_item, api_client):
    from backend.autoapply.runner import submit_item
    from backend.models.db import Persona
    planner, _ = _planner(Fake(choice=None))
    await submit_item(ready_item.id, planner=planner)
    test_db.expire_all()
    it = test_db.query(AutoApplyItem).get(ready_item.id)
    assert it.status == "needs_human"
    (q,) = test_db.query(AutoApplyQuestion).filter(AutoApplyQuestion.item_id == it.id).all()
    assert q.options == ["0-2", "3-5", "6+"]

    listed = api_client.get("/api/autoapply/questions").json()
    assert listed[0]["company"] == "Acme"
    r = api_client.post(f"/api/autoapply/questions/{q.id}/answer", json={"answer": "6+"}).json()
    assert r["item_requeued"] is True
    test_db.expire_all()
    assert test_db.query(AutoApplyItem).get(it.id).status == "ready"
    bank = test_db.query(Persona).get(1).qa_bank
    assert {"question": q.question, "answer": "6+"} in bank


async def test_submit_item_failure_is_not_retried(test_db, ready_item):
    from backend.autoapply.runner import submit_item

    class Boom:
        async def __aenter__(self):
            raise RuntimeError("chromium crashed")

        async def __aexit__(self, *a):
            return False
    planner, _ = _planner()
    await submit_item(ready_item.id, browser_factory=Boom, planner=planner)
    test_db.expire_all()
    it = test_db.query(AutoApplyItem).get(ready_item.id)
    assert it.status == "failed" and it.attempts == 1 and "chromium crashed" in it.error


def _fake_submitter(monkeypatch, outcome):
    from backend.autoapply import runner

    class Sub:
        async def run(self, page, packet, plan):
            return outcome

    class NoBrowser:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(runner, "submitter_for", lambda name: Sub)
    return NoBrowser


async def test_clicked_submit_without_confirmation_parks(test_db, ready_item, monkeypatch):
    from backend.autoapply.runner import submit_item
    from backend.autoapply.submitters.base import Outcome
    factory = _fake_submitter(monkeypatch, Outcome(status="failed", error="no confirmation after submit",
                                                   steps=["open x", "clicked submit"]))
    planner, _ = _planner()
    await submit_item(ready_item.id, browser_factory=factory, planner=planner)
    test_db.expire_all()
    it = test_db.query(AutoApplyItem).get(ready_item.id)
    assert it.status == "needs_human" and "submit was clicked" in it.error


async def test_closed_posting_skips_item_and_job(test_db, ready_item, monkeypatch):
    from backend.autoapply.runner import submit_item
    from backend.autoapply.submitters.base import Outcome
    from backend.models.db import Job
    factory = _fake_submitter(monkeypatch, Outcome(status="closed", error="posting closed: no longer accepting applications"))
    planner, _ = _planner()
    await submit_item(ready_item.id, browser_factory=factory, planner=planner)
    test_db.expire_all()
    it = test_db.query(AutoApplyItem).get(ready_item.id)
    assert it.status == "skipped" and "closed" in it.error
    assert test_db.query(Job).get(it.job_id).status == "skip"


def test_mark_applied_records_application(api_client, test_db, ready_item):
    from backend.models.db import Application, Job
    ready_item.status = "needs_human"
    test_db.commit()
    r = api_client.post("/api/autoapply/items/action", json={"action": "applied", "ids": [str(ready_item.id)]}).json()
    assert r["changed"] == [str(ready_item.id)]
    test_db.expire_all()
    it = test_db.query(AutoApplyItem).get(ready_item.id)
    assert it.status == "submitted" and it.submitted_at is not None
    app = test_db.query(Application).get(it.application_id)
    assert app.status == "applied" and "by hand" in app.notes
    assert test_db.query(Job).get(it.job_id).status == "applied"


async def test_tick_disabled_and_quiet(test_db, aa_settings):
    from backend.autoapply.runner import tick
    assert await tick(now=NOON_NY) == "disabled"
    aa_settings(autoapply_enabled=True)
    assert await tick(now=datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc)) == "quiet hours"
    assert await tick(now=NOON_NY) == "nothing ready"


def test_submit_action_moves_parked_items(api_client, test_db, make_job, monkeypatch):
    from backend.autoapply import routes
    monkeypatch.setattr(routes, "_kick_prepare", lambda: None)
    a = _item(test_db, make_job("A"), status="dry_run_ok", resume_pdf_path="/x.pdf", attempts=2)
    b = _item(test_db, make_job("B"), status="needs_human")           # no documents yet
    r = api_client.post("/api/autoapply/items/action", json={"action": "submit", "ids": [str(a.id), str(b.id)]}).json()
    assert sorted(r["changed"]) == sorted([str(a.id), str(b.id)])
    test_db.expire_all()
    assert test_db.query(AutoApplyItem).get(a.id).status == "ready"
    assert test_db.query(AutoApplyItem).get(a.id).attempts == 0
    assert test_db.query(AutoApplyItem).get(b.id).status == "approved"
