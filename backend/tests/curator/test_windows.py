"""The curator can run more than once a day (curate_cron twice, 12-hour windows): picks are numbered on across
windows, a job surfaced in an earlier window is not surfaced again, and the day's people search stays one budget."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.models.db import Contact, Job
from backend.tz import local_today


def _job(i, **kw):
    now = datetime.now(timezone.utc)
    base = dict(external_id=f"w{i}", company=f"Co{i}", title="Software Engineer", url=f"https://boards.greenhouse.io/co{i}/{i}",
                description="Build backend services in Python.", status="new", discovered_at=now - timedelta(hours=2),
                posted_at=now - timedelta(hours=1), best_cv_score=70, best_cv="SDE", cv_scores={"SDE": 70}, loc_country="US")
    base.update(kw)
    return Job(**base)


def _pick(j):
    return {"id": j.id, "score": 0.9, "parts": {"fresh": 0.5}, "reason": "r", "intel": {}}


def test_second_window_continues_the_numbering_and_never_repeats_a_job(test_db):
    from backend.curator.run import load_pool, write_results
    jobs = [_job(i) for i in range(1, 7)]
    test_db.add_all(jobs)
    test_db.commit()

    pool, ids = load_pool(24)
    assert len(pool) == 6
    write_results([_pick(j) for j in jobs[:3]], ids, auto_ignore=False, dropped={})

    # a later window: the three surfaced jobs are not candidates again
    pool2, ids2 = load_pool(24)
    assert {d["id"] for d in pool2} == {j.id for j in jobs[3:]}
    write_results([_pick(j) for j in jobs[3:5]], ids2, auto_ignore=False, dropped={})

    test_db.expire_all()
    rows = (test_db.query(Job).filter(Job.curated_on == local_today(), Job.curated_rank.isnot(None))
            .order_by(Job.curated_rank).all())
    assert [r.curated_rank for r in rows] == [1, 2, 3, 4, 5]
    assert len({r.id for r in rows}) == 5
    # the apply window reads the rank inside its own run, not the day's running number
    assert rows[3].curation["apply_window"] == "now"


def test_a_job_skipped_or_ignored_in_an_earlier_window_stays_out(test_db):
    from backend.curator.run import load_pool
    test_db.add_all([_job(1, status="ignored"), _job(2, status="skip"), _job(3)])
    test_db.commit()
    pool, _ = load_pool(24)
    assert [d["company"] for d in pool] == ["Co3"]


@pytest.mark.parametrize("daily,runs,found,want", [
    (20, 1, 0, 20),     # one run a day: untouched
    (20, 1, 15, 20),    # ... even when a manual re-run already found people today
    (20, 2, 0, 10),     # two runs share the day's count
    (5, 2, 0, 3),
    (5, 2, 3, 2),       # never past what is left of the day
    (5, 2, 5, 0),
    (0, 2, 0, 0),
])
def test_per_run_quota_keeps_the_people_search_a_daily_budget(daily, runs, found, want):
    from backend.curator.run import per_run_quota
    assert per_run_quota(daily, runs, found) == want


def test_found_today_counts_per_purpose(test_db):
    from backend.curator.run import _found_today
    today = local_today()
    test_db.add_all([
        Contact(company="A", name="a", linkedin_url="u1", found_on=today, purpose="job"),
        Contact(company="B", name="b", linkedin_url="u2", found_on=today, purpose="network", lead_kind="network"),
        Contact(company="C", name="c", linkedin_url="u3", found_on=today - timedelta(days=1), purpose="job"),
    ])
    test_db.commit()
    assert _found_today(test_db, today, "job") == 1
    assert _found_today(test_db, today, "network") == 1


def test_runs_per_day_defaults_to_one_and_is_a_curator_setting():
    from backend.accounts.settings import SETTING_SECTIONS
    from backend.seed import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["curate_runs_per_day"][0] == "1"
    assert "curate_runs_per_day" in str(SETTING_SECTIONS)


def _picked(test_db):
    test_db.expire_all()
    return {r.company for r in test_db.query(Job).filter(Job.curated_rank.isnot(None)).all()}


def test_auto_ignore_false_unpicked_stay_new_and_may_be_picked_later_but_never_twice(test_db):
    """Live setting (curate_auto_ignore=false on both profiles): the unpicked remain candidates."""
    from backend.curator.run import load_pool, write_results
    jobs = [_job(i) for i in range(1, 6)]
    test_db.add_all(jobs)
    test_db.commit()
    pool, ids = load_pool(24)
    write_results([_pick(j) for j in jobs[:2]], ids, auto_ignore=False, dropped={})
    test_db.expire_all()
    assert {j.company for j in test_db.query(Job).filter(Job.status == "new").all()} == {"Co3", "Co4", "Co5"}

    pool2, ids2 = load_pool(24)
    assert {d["company"] for d in pool2} == {"Co3", "Co4", "Co5"}        # never the two already picked
    write_results([_pick(j) for j in jobs[2:4]], ids2, auto_ignore=False, dropped={})

    pool3, ids3 = load_pool(24)
    assert {d["company"] for d in pool3} == {"Co5"}
    assert _picked(test_db) == {"Co1", "Co2", "Co3", "Co4"}
    ranks = sorted(r.curated_rank for r in test_db.query(Job).filter(Job.curated_rank.isnot(None)).all())
    assert ranks == [1, 2, 3, 4]


def test_auto_ignore_true_closes_the_window_for_the_rest(test_db):
    """Code default (curate_auto_ignore=true): what a run did not pick is ignored and not offered again."""
    from backend.curator.run import load_pool, write_results
    jobs = [_job(i) for i in range(1, 5)]
    test_db.add_all(jobs)
    test_db.commit()
    pool, ids = load_pool(24)
    write_results([_pick(j) for j in jobs[:2]], ids, auto_ignore=True, dropped={})
    pool2, _ = load_pool(24)
    assert pool2 == []


def test_scout_leads_do_not_use_up_the_curators_network_quota(test_db):
    from backend.curator.run import _found_today, per_run_quota
    today = local_today()
    test_db.add_all([
        Contact(company="S1", name="s1", linkedin_url="s1", found_on=today, purpose="network", lead_kind="alumni", score=80),
        Contact(company="S2", name="s2", linkedin_url="s2", found_on=today, purpose="network", lead_kind="network", score=70),
        Contact(company="C1", name="c1", linkedin_url="c1", found_on=today, purpose="network", lead_kind="network"),
    ])
    test_db.commit()
    assert _found_today(test_db, today, "network") == 1               # only the curator's own person
    assert per_run_quota(10, 2, _found_today(test_db, today, "network")) == 5


async def test_two_curator_runs_for_one_account_cannot_overlap(test_db):
    """The single-flight guard is job_monitor's: the scheduler and 'run now' both key a run by
    (job type, account), so a second 'curate' while one runs is refused, never run beside it."""
    import backend.job_monitor as jm
    jm._running.clear()
    try:
        async with jm.tracked_run("curate", "scheduler"):
            with pytest.raises(jm.JobAlreadyRunningError):
                async with jm.tracked_run("curate", "manual"):
                    pass
            with pytest.raises(jm.JobAlreadyRunningError):
                jm.launch_background("curate", lambda: None, trigger="manual")
    finally:
        jm._running.clear()
