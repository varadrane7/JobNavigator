"""Upstream integration points: scheduler hook, router, and the upstream helpers AutoApply
imports. If an upstream pull renames one of these, this file is where it shows."""
from datetime import date

import pytest


def test_upstream_contract():
    from backend.analyzer import cv_scorer, llm_client, llm_logger, model_json, prompt_fence
    from backend.api import routes_resumes, routes_applications
    from backend import autofill_schema, job_monitor, activity
    from backend.scraper._shared import browser, urls, bot_wall
    for mod, names in [
        (cv_scorer, ["score_single_job"]),
        (llm_client, ["call_llm", "resolve_llm_config", "NonRetryableLLMError", "_QUOTA_RE"]),
        (llm_logger, ["track_llm_call"]),
        (model_json, ["parse_model_json"]),
        (prompt_fence, ["fence"]),
        (routes_resumes, ["_resolve_tailoring_jd", "_render_html", "_rewrite_urls_with_tracers", "_get_browser"]),
        (routes_applications, ["create_application"]),
        (autofill_schema, ["project_answers", "ANSWER_SCHEMA"]),
        (job_monitor, ["tracked_run", "launch_background", "JobAlreadyRunningError"]),
        (activity, ["log_activity"]),
        (browser, ["_get_browser", "_new_page", "_close_page"]),
        (urls, ["host_matches"]),
        (bot_wall, ["is_bot_wall"]),
    ]:
        missing = [n for n in names if not hasattr(mod, n)]
        assert not missing, f"{mod.__name__} lost {missing}"


@pytest.fixture
def real_scheduler(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    import backend.scheduler as sched_mod
    s = AsyncIOScheduler()
    monkeypatch.setattr(sched_mod, "scheduler", s)
    return s


def test_jobs_survive_configure_scheduler(test_db, aa_settings, real_scheduler):
    from backend.scheduler import configure_scheduler
    aa_settings(autoapply_enabled=True)
    configure_scheduler()
    assert real_scheduler.get_job("autoapply_queue") is not None
    configure_scheduler()   # remove_all_jobs() then our hook again
    assert real_scheduler.get_job("autoapply_queue") is not None


def test_disabled_registers_nothing(test_db, aa_settings, real_scheduler):
    from backend.scheduler import configure_scheduler
    configure_scheduler()
    assert real_scheduler.get_job("autoapply_queue") is None


def test_settings_routes(api_client):
    r = api_client.get("/api/autoapply/settings")
    assert r.status_code == 200
    assert r.json()["values"]["autoapply_daily_cap"] == 100
    r = api_client.put("/api/autoapply/settings", json={"autoapply_daily_cap": "x"})
    assert r.status_code == 422 and "autoapply_daily_cap" in r.json()["detail"]
    r = api_client.put("/api/autoapply/settings", json={"autoapply_daily_cap": 50})
    assert r.status_code == 200 and r.json()["values"]["autoapply_daily_cap"] == 50


def test_items_and_actions(api_client, test_db, make_job):
    from backend.autoapply.models import AutoApplyItem
    j1, j2 = make_job("A"), make_job("B")
    a = AutoApplyItem(job_id=j1.id, run_date=date(2026, 10, 9), rank=1, submitter="greenhouse", status="queued")
    b = AutoApplyItem(job_id=j2.id, run_date=date(2026, 10, 9), rank=2, submitter="greenhouse", status="submitted")
    test_db.add_all([a, b])
    test_db.commit()

    rows = api_client.get("/api/autoapply/items?run_date=2026-10-09").json()
    assert [r["company"] for r in rows] == ["A", "B"]
    assert api_client.get("/api/autoapply/items?status=bogus").status_code == 422

    r = api_client.post("/api/autoapply/items/action", json={"action": "approve", "ids": [str(a.id), str(b.id)]}).json()
    assert r["changed"] == [str(a.id)]
    assert r["unchanged"] == [{"id": str(b.id), "status": "submitted"}]
    assert api_client.get("/api/autoapply/items?status=approved").json()[0]["id"] == str(a.id)

    s = api_client.get("/api/autoapply/summary?run_date=2026-10-09").json()
    assert s["total"] == 2 and s["by_status"] == {"approved": 1, "submitted": 1}
    assert api_client.post("/api/autoapply/items/action", json={"action": "nuke", "ids": []}).status_code == 422
