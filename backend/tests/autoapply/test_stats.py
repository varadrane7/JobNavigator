from datetime import date, datetime, timedelta, timezone

from backend.autoapply import stats
from backend.autoapply.models import AutoApplyItem, AutoApplyAttempt
from backend.models.db import Application


def _item(db, job, status, sub="greenhouse", when=None, app=None):
    it = AutoApplyItem(job_id=job.id, run_date=date(2026, 10, 9), rank=1, submitter=sub, status=status,
                       company_key=job.company.lower(), submitted_at=when if status == "submitted" else None,
                       application_id=app.id if app else None)
    db.add(it)
    db.commit()
    return it


def test_stats_rollup(test_db, make_job, aa_settings):
    aa_settings()
    now = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    a, b, c, d = (make_job(n) for n in ("A", "B", "C", "D"))
    app = Application(job_id=a.id, status="interview")
    test_db.add(app)
    test_db.commit()
    s1 = _item(test_db, a, "submitted", when=now - timedelta(hours=2), app=app)
    _item(test_db, b, "submitted", sub="lever", when=now - timedelta(days=1))
    _item(test_db, c, "needs_human")
    _item(test_db, d, "failed")
    test_db.add(AutoApplyAttempt(item_id=s1.id, outcome="submitted", llm_calls=3,
                                 started_at=now - timedelta(minutes=3), finished_at=now - timedelta(minutes=1)))
    test_db.commit()
    out = stats.compute(test_db, days=7, now=now)
    t = out["totals"]
    assert t["submitted"] == 2 and t["needs_human"] == 1 and t["failed"] == 1
    assert t["response_rate"] == 50.0 and t["avg_seconds_per_app"] == 120.0 and t["llm_calls_per_app"] == 1.5
    gh = next(s for s in out["by_submitter"] if s["submitter"] == "greenhouse")
    assert gh["total"] == 3 and gh["success_rate"] == 33.3
    assert len(out["daily"]) == 7 and sum(x["submitted"] for x in out["daily"]) == 2


def test_stats_empty_and_route(api_client, aa_settings):
    aa_settings()
    r = api_client.get("/api/autoapply/stats?days=3")
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["submitted"] == 0 and body["totals"]["response_rate"] is None
    assert len(body["daily"]) == 3
