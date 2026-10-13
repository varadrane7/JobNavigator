"""Curation end to end on SQLite, with the network (embeddings, LLM, search, LinkedIn) stubbed."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.models.db import Contact, Job, Setting


def _job(i, **kw):
    now = datetime.now(timezone.utc)
    base = dict(external_id=f"e{i}", company=f"Co{i}", title="Software Engineer", url=f"https://boards.greenhouse.io/co{i}/{i}",
                description="Build backend services in Python. 3+ years of experience.", status="new",
                discovered_at=now - timedelta(hours=2), posted_at=now - timedelta(hours=i % 30),
                best_cv_score=60 + (i % 40), best_cv="SDE", cv_scores={"SDE": 60 + (i % 40)},
                loc_country="US", h1b_company_lca_count=i * 3)
    base.update(kw)
    return Job(**base)


@pytest.fixture
def seeded(test_db, monkeypatch):
    test_db.add_all([_job(i) for i in range(1, 31)])
    test_db.add(_job(100, title="Senior Software Engineer"))
    test_db.add(_job(101, h1b_jd_flag=True))
    test_db.add(_job(102, description="Requires 8+ years of experience in distributed systems"))
    test_db.add(_job(103, discovered_at=datetime.now(timezone.utc) - timedelta(days=5),
                     posted_at=datetime.now(timezone.utc) - timedelta(days=5)))          # outside the window
    test_db.add(_job(104, loc_country="IN"))
    test_db.add(Setting(key="curate_count", value="10"))
    test_db.add(Setting(key="curate_title_exclude", value='["senior"]'))
    test_db.commit()

    import backend.curator.run as run
    async def no_sim(resumes, jds):
        return None
    async def no_intel(companies, concurrency=3):
        return {}
    async def no_score(jobs):
        return None
    monkeypatch.setattr(run, "similarity", no_sim)
    monkeypatch.setattr(run, "get_intel", no_intel)
    monkeypatch.setattr(run, "_score_unscored", no_score)
    monkeypatch.setattr(run, "_resume_texts", lambda: ["resume"])
    return test_db


async def test_dry_run_ranks_without_writing(seeded):
    from backend.curator.run import run_daily_curation
    out = await run_daily_curation(dry_run=True)
    assert len(out["top"]) == 10
    assert out["drop_reasons"] == {"title": 1, "no sponsorship": 1, "8+ years": 1}
    scores = [t["score"] for t in out["top"]]
    assert scores == sorted(scores, reverse=True)
    assert seeded.query(Job).filter(Job.status == "saved").count() == 0


async def test_run_saves_top_and_ignores_rest(seeded, monkeypatch):
    import backend.curator.run as run
    async def fake_contacts(top, intel, want):
        seeded.add(Contact(company=top[0]["company"], name="Jane Doe", linkedin_url="https://linkedin.com/in/jd",
                           found_on=run.local_today()))
        seeded.commit()
        return [{"company": top[0]["company"]}]
    sent = []
    async def fake_notify(top, contacts):
        sent.append((len(top), len(contacts)))
    monkeypatch.setattr(run, "find_contacts", fake_contacts)
    monkeypatch.setattr(run, "notify", fake_notify)

    out = await run.run_daily_curation()
    assert out["saved"] == 10 and out["contacts"] == 1 and sent == [(10, 1)]
    seeded.expire_all()
    saved = seeded.query(Job).filter(Job.status == "saved").order_by(Job.curated_rank).all()
    assert [j.curated_rank for j in saved] == list(range(1, 11))
    assert all(j.saved and j.curation["reason"] and j.curation["apply_window"] for j in saved)
    # every other in-window "new" job (including hard drops and the non-US one) is ignored, reversibly marked
    ignored = seeded.query(Job).filter(Job.status == "ignored").all()
    assert len(ignored) == 34 - 10
    assert all(j.curation.get("out") for j in ignored)
    assert seeded.query(Job).filter(Job.external_id == "e103").one().status == "new"   # outside window untouched
    why = {j.external_id: j.curation["why"] for j in ignored}
    assert why["e100"] == "title: senior" and why["e101"] == "no sponsorship"


async def test_per_company_cap(seeded):
    seeded.add_all([_job(200 + i, company="BigCo", best_cv_score=99) for i in range(5)])
    seeded.commit()
    from backend.curator.run import run_daily_curation
    out = await run_daily_curation(dry_run=True)
    assert sum(t["company"] == "BigCo" for t in out["top"]) == 2


def test_today_endpoint_and_contact_patch(api_client, test_db):
    from backend.tz import local_today
    j = _job(1, status="saved", curated_on=local_today(), curated_rank=1,
             curation={"score": 0.8, "parts": {}, "reason": "fit 90", "apply_window": "now"})
    test_db.add(j)
    test_db.commit()
    c = Contact(company="Co1", name="Jane Doe", job_id=j.id, found_on=local_today(), linkedin_url="u")
    test_db.add(c)
    test_db.commit()
    r = api_client.get("/api/curator/today")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["timezone"] and body["jobs"][0]["rank"] == 1 and body["jobs"][0]["apply_window"] == "now"
    assert body["contacts"][0]["name"] == "Jane Doe"
    r = api_client.patch(f"/api/contacts/{c.id}", json={"status": "contacted"})
    assert r.status_code == 200 and r.json()["status"] == "contacted"
    assert api_client.patch(f"/api/contacts/{c.id}", json={"status": "bogus"}).status_code == 422
    assert api_client.patch("/api/contacts/not-a-uuid", json={"status": "skip"}).status_code == 404


def test_jobs_sort_by_rank(api_client, test_db):
    from backend.tz import local_today
    test_db.add_all([_job(1, status="saved", curated_on=local_today(), curated_rank=2),
                     _job(2, status="saved", curated_on=local_today(), curated_rank=1),
                     _job(3, status="saved")])
    test_db.commit()
    r = api_client.get("/api/jobs", params={"sort_by": "rank", "status": "saved"})
    assert r.status_code == 200, r.text
    jobs = r.json()["jobs"] if isinstance(r.json(), dict) else r.json()
    assert [j["curated_rank"] for j in jobs] == [1, 2, None]
