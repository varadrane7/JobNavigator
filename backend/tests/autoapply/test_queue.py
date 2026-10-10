from datetime import date

import pytest

from backend.autoapply.models import AutoApplyItem
from backend.autoapply.queue import build_daily, company_key

RUN = date(2026, 10, 9)
NAMES = ("SDE", "Cloud", "DotNet")


@pytest.fixture
def three_resumes(make_resume):
    return {n: make_resume(n) for n in NAMES}


def _items(db):
    db.expire_all()
    return db.query(AutoApplyItem).order_by(AutoApplyItem.rank).all()


async def _no_score(job_id, cv_ids):
    raise AssertionError("nothing should need scoring")


def test_company_key_folds_suffixes():
    assert company_key("Acme, Inc.") == company_key("ACME Corp") == company_key("acme") == "acme"


async def test_ranks_by_best_resume_and_min_score(test_db, three_resumes, make_job, aa_settings):
    aa_settings(autoapply_min_score=60)
    low = make_job("Low", cv_scores={"SDE": 50, "Cloud": 55, "DotNet": 40})
    mid = make_job("Mid", cv_scores={"SDE": 61, "Cloud": 70, "DotNet": 20})
    top = make_job("Top", cv_scores={"SDE": 90, "Cloud": 10, "DotNet": 85})
    r = await build_daily(RUN, score_fn=_no_score)
    items = _items(test_db)
    assert r["queued"] == 2
    assert [i.job_id for i in items] == [top.id, mid.id]
    assert items[0].base_resume_name == "SDE" and items[0].score == 90
    assert items[1].base_resume_name == "Cloud"
    assert items[0].base_resume_id == three_resumes["SDE"].id
    assert all(i.status == "queued" for i in items)
    assert low.id not in {i.job_id for i in items}


async def test_ignores_scores_from_resumes_not_selected(test_db, three_resumes, make_job, aa_settings):
    aa_settings(autoapply_resume_ids=[str(three_resumes["DotNet"].id)])
    make_job("A", cv_scores={"SDE": 99, "Cloud": 99, "DotNet": 72, "Persona": 100})
    await build_daily(RUN, score_fn=_no_score)
    (it,) = _items(test_db)
    assert it.base_resume_name == "DotNet" and it.score == 72


async def test_scores_only_missing_resumes(test_db, three_resumes, make_job, aa_settings):
    j = make_job("A", cv_scores={"SDE": 80})
    calls = []

    async def fake_score(job_id, cv_ids):
        calls.append((job_id, sorted(cv_ids)))
        from backend.models.db import Job, SessionLocal
        s = SessionLocal()
        row = s.query(Job).filter(Job.id == j.id).first()
        row.cv_scores = {**row.cv_scores, "Cloud": 95, "DotNet": 10}
        s.commit()
        s.close()

    r = await build_daily(RUN, score_fn=fake_score)
    assert calls == [(str(j.id), sorted([str(three_resumes["Cloud"].id), str(three_resumes["DotNet"].id)]))]
    assert r["scored"] == 1
    (it,) = _items(test_db)
    assert it.base_resume_name == "Cloud" and it.score == 95


async def test_score_budget_and_scoring_failure(test_db, three_resumes, make_job, aa_settings):
    aa_settings(autoapply_score_budget=1)
    make_job("A")
    make_job("B")
    calls = []

    async def failing(job_id, cv_ids):
        calls.append(job_id)
        raise RuntimeError("LLM down")

    r = await build_daily(RUN, score_fn=failing)
    assert len(calls) == 1 and r["scored"] == 0 and r["queued"] == 0


async def test_site_and_company_caps(test_db, three_resumes, make_job, aa_settings):
    aa_settings(autoapply_site_caps={"linkedin": 1, "greenhouse": 10, "generic": 10},
                autoapply_company_daily_cap=2)
    s = {"SDE": 90, "Cloud": 1, "DotNet": 1}
    make_job("LI1", url="https://www.linkedin.com/jobs/view/1", cv_scores=s)
    make_job("LI2", url="https://www.linkedin.com/jobs/view/2", cv_scores=s)
    for _ in range(3):
        make_job("Zeta Co", cv_scores=s)
    await build_daily(RUN, score_fn=_no_score)
    items = _items(test_db)
    assert sum(i.submitter == "linkedin" for i in items) == 1
    assert sum(i.company_key == "zeta" for i in items) == 2


async def test_daily_cap_and_idempotent_top_up(test_db, three_resumes, make_job, aa_settings):
    aa_settings(autoapply_daily_cap=2)
    for n in range(4):
        make_job(f"C{n}", cv_scores={"SDE": 80 + n})
    await build_daily(RUN, score_fn=_no_score)
    assert len(_items(test_db)) == 2
    await build_daily(RUN, score_fn=_no_score)
    assert len(_items(test_db)) == 2
    aa_settings(autoapply_daily_cap=3)
    await build_daily(RUN, score_fn=_no_score)
    items = _items(test_db)
    assert len(items) == 3 and [i.rank for i in items] == [1, 2, 3]


async def test_exclusions(test_db, three_resumes, make_job, aa_settings):
    from backend.models.db import Application
    aa_settings(autoapply_company_blocklist=["BadCo"], autoapply_title_blocklist=["intern"],
                autoapply_max_age_days=7, autoapply_enabled_submitters=["greenhouse"])
    s = {"SDE": 90}
    applied = make_job("Applied", cv_scores=s)
    test_db.add(Application(job_id=applied.id))
    test_db.commit()
    make_job("BadCo", cv_scores=s)
    make_job("Ok", title="Software Intern", cv_scores=s)
    make_job("Old", cv_scores=s, age_days=30)
    make_job("Skip", cv_scores=s, status="skip")
    make_job("Lever", url="https://jobs.lever.co/x/1", cv_scores=s)
    make_job("Indeed", url="https://www.indeed.com/viewjob?jk=1", cv_scores=s)
    good = make_job("Good", cv_scores=s)
    await build_daily(RUN, score_fn=_no_score)
    assert [i.job_id for i in _items(test_db)] == [good.id]


async def test_content_hash_dedup_across_days(test_db, three_resumes, make_job, aa_settings):
    a = make_job("A", cv_scores={"SDE": 90}, content_hash="h1")
    make_job("A", cv_scores={"SDE": 80}, content_hash="h1")
    await build_daily(RUN, score_fn=_no_score)
    assert [i.job_id for i in _items(test_db)] == [a.id]
    await build_daily(date(2026, 10, 10), score_fn=_no_score)
    assert len(_items(test_db)) == 1


async def test_auto_mode_queues_as_approved(test_db, three_resumes, make_job, aa_settings):
    aa_settings(autoapply_mode="auto")
    make_job("A", cv_scores={"SDE": 90})
    await build_daily(RUN, score_fn=_no_score)
    assert _items(test_db)[0].status == "approved"


async def test_no_base_resumes(test_db, make_job, aa_settings):
    make_job("A", cv_scores={"SDE": 90})
    r = await build_daily(RUN, score_fn=_no_score)
    assert r["queued"] == 0 and r["reason"] == "no base résumés"
