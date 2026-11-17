"""LinkedIn page-load budget kept in the database, and the per-run share (curator/li_budget.py)."""
from datetime import date, timedelta

import pytest

from backend.curator import li_budget


@pytest.fixture
def budget(test_db, monkeypatch):
    settings = {}
    monkeypatch.setattr(li_budget, "_jar", lambda which: f"/jars/{which}.json")
    monkeypatch.setattr(li_budget, "_setting_int", lambda key, default: int(settings.get(key, default)))
    monkeypatch.setattr(li_budget, "_setting_str", lambda key, default: str(settings.get(key, default)))
    li_budget.reset_all()
    yield settings
    li_budget.reset_all()


def test_usage_survives_a_restart(budget):
    assert li_budget.take("main") and li_budget.take("main")
    li_budget.forget_process_state()          # what a restart loses
    assert li_budget.used("main") == 2
    assert li_budget.used("mock") == 0


def test_a_restart_cannot_double_the_days_loads(budget):
    for _ in range(15):
        assert li_budget.take("main")
    li_budget.forget_process_state()
    assert li_budget.take("main") is False and li_budget.used("main") == 15


def test_day_rollover_starts_at_zero(budget, monkeypatch):
    today = date(2026, 11, 16)
    monkeypatch.setattr(li_budget, "local_today", lambda: today)
    li_budget.take("main")
    assert li_budget.used("main") == 1
    monkeypatch.setattr(li_budget, "local_today", lambda: today + timedelta(days=1))
    assert li_budget.used("main") == 0
    assert li_budget.take("main") and li_budget.used("main") == 1
    monkeypatch.setattr(li_budget, "local_today", lambda: today)
    assert li_budget.used("main") == 1          # yesterday's count is kept, not mixed in


def test_each_session_jar_has_its_own_count(budget, monkeypatch):
    li_budget.take("main")
    monkeypatch.setattr(li_budget, "_jar", lambda which: f"/other/{which}.json")
    assert li_budget.used("main") == 0
    monkeypatch.setattr(li_budget, "_jar", lambda which: f"/jars/{which}.json")
    assert li_budget.used("main") == 1


def test_shares_are_8_and_5_of_15_with_2_held_back(budget):
    assert li_budget.run_shares("main", 2) == [8, 5]
    assert li_budget.run_shares("main", 1) == []
    assert li_budget.run_shares("mock", 2) == [32, 20]       # scaled to the mock cap of 60


def _drain(which="main"):
    n = 0
    while li_budget.take(which):
        n += 1
        assert n < 500
    return n


def test_two_runs_first_takes_8_second_5_and_2_stay_free(budget):
    assert li_budget.begin_run(2) == 1
    assert _drain() == 8
    li_budget.end_run()
    assert li_budget.begin_run(2) == 2
    assert _drain() == 5
    li_budget.end_run()
    # outside a run (scout, link lookup) the 2 held back are still there, never more than the cap
    assert li_budget.take("main") and li_budget.take("main") and not li_budget.take("main")
    assert li_budget.used("main") == 15


def test_run_ordinal_survives_a_restart(budget):
    li_budget.begin_run(2)
    li_budget.end_run()
    li_budget.forget_process_state()
    assert li_budget.begin_run(2) == 2


def test_one_run_a_day_is_unchanged(budget):
    assert li_budget.begin_run(1) == 0
    assert _drain() == 15 and li_budget.used("main") == 15


def test_first_run_not_spent_gives_nothing_to_the_second(budget):
    li_budget.begin_run(2)
    li_budget.take("main")
    li_budget.end_run()
    li_budget.begin_run(2)
    assert _drain() == 5      # a short first run does not enlarge the second's share (conservative)


def test_a_third_run_gets_no_share_and_the_reserve_survives(budget):
    for expect in (8, 5):
        li_budget.begin_run(2)
        assert _drain() == expect
        li_budget.end_run()
    assert li_budget.begin_run(2) == 3
    assert _drain() == 0                       # no share of its own: the 2 held back are not eaten
    li_budget.end_run()
    assert li_budget.used("main") == 13 and li_budget.take("main") and li_budget.take("main")


def test_a_manual_run_claims_no_ordinal_and_never_takes_a_scheduled_share(budget):
    assert li_budget.begin_run(2, manual=True) == li_budget.MANUAL
    assert _drain() == 2                       # only the leftover (the reserve), not run 1's 8
    li_budget.end_run()
    assert li_budget.begin_run(2) == 1         # the scheduled runs keep their ordinals ...
    assert _drain() == 8                       # ... and their shares, even though the manual run came first
    li_budget.end_run()
    assert li_budget.begin_run(2) == 2
    assert _drain() == 5
    li_budget.end_run()
    assert li_budget.used("main") == 15


def test_a_manual_run_with_one_run_a_day_is_unchanged(budget):
    assert li_budget.begin_run(1, manual=True) == 0
    assert _drain() == 15


@pytest.mark.parametrize("cap", [7, 10, 15, 1, 3])
def test_odd_caps_never_exceed_the_total(budget, cap):
    budget["linkedin_main_daily_cap"] = cap
    granted = 0
    for ordinal_manual in (False, False, True, False):
        li_budget.begin_run(2, manual=ordinal_manual)
        granted += _drain()
        li_budget.end_run()
    granted += _drain()          # and a caller outside any run
    assert granted == cap == li_budget.used("main")
    shares = li_budget.run_shares("main", 2)
    assert sum(shares) <= cap - 0 and all(x >= 0 for x in shares)
    if cap == 7:
        assert shares == [3, 2]       # 7*8//15, 7*5//15: floor only shrinks a share
    if cap == 10:
        assert shares == [5, 3]


@pytest.mark.parametrize("split,reserve,cap", [("8,5", 2, 15), ("20,20", 0, 15), ("100,100", 0, 7), ("8,5", 0, 15),
                                               ("0,0", 0, 15), ("x,y", 2, 15), ("", 2, 15), ("8,5,9", 1, 15)])
def test_shares_plus_reserve_never_exceed_the_cap(budget, split, reserve, cap):
    budget.update({"linkedin_run_split": split, "linkedin_run_reserve": reserve, "linkedin_main_daily_cap": cap})
    assert sum(li_budget.run_shares("main", 2)) <= cap
    assert sum(li_budget.run_shares("main", 3)) <= cap


def test_a_run_never_takes_more_than_the_daily_cap(budget):
    budget.update({"linkedin_main_daily_cap": 6, "linkedin_run_split": "9,9", "linkedin_run_reserve": "0"})
    li_budget.begin_run(2)
    n = _drain()
    assert n <= 3 and li_budget.used("main") == n


def test_a_refused_run_load_leaves_the_daily_total_untouched(budget):
    li_budget.begin_run(2)
    assert _drain() == 8
    for _ in range(3):
        assert li_budget.take("main") is False
    assert li_budget.used("main") == 8         # the refusal on the run bucket did not bump the total


def test_a_refused_total_leaves_the_run_bucket_untouched(budget):
    budget["linkedin_main_daily_cap"] = 15
    for _ in range(15):
        li_budget.take("main")                 # outside a run: fills the total
    li_budget.begin_run(2)
    assert li_budget.take("main") is False
    assert li_budget._count("main", "run1") == 0


def test_the_database_unreadable_refuses_instead_of_granting(budget, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(li_budget, "_db_read", boom)
    assert li_budget.used("main") == li_budget.cap("main")       # shown as spent
    assert li_budget.available("main") is False and li_budget.take("main") is False


def test_the_database_unwritable_refuses_instead_of_granting(budget, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(li_budget, "_reserve", boom)
    assert li_budget.take("main") is False
    monkeypatch.undo()
    # nothing was recorded or kept in memory: after the database is back the full cap is still there
    li_budget.forget_process_state()


def test_a_run_that_cannot_be_recorded_gets_no_share(budget, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(li_budget, "_claim", boom)
    assert li_budget.begin_run(2) == li_budget.NOT_PLANNED
    monkeypatch.undo()
    assert li_budget.take("main") is False


async def test_the_share_reaches_the_task_even_though_the_claim_runs_in_a_thread(budget):
    import asyncio
    ordinal = await asyncio.to_thread(li_budget.claim_run, 2)
    li_budget.enter_run(ordinal, 2)
    assert (ordinal, _drain()) == (1, 8)


# ── concurrency: a file database so that threads really have their own connections ─────────────

@pytest.fixture
def filedb(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    import backend.models.db as db_mod
    engine = create_engine(f"sqlite:///{tmp_path / 'li.db'}", connect_args={"timeout": 60, "check_same_thread": False})
    db_mod.LiUsage.__table__.create(engine)
    old = db_mod.SessionLocal.kw.get("bind")
    db_mod.SessionLocal.configure(bind=engine)
    monkeypatch.setattr(li_budget, "_jar", lambda which: f"/jars/{which}.json")
    monkeypatch.setattr(li_budget, "_setting_int", lambda key, default: default)
    monkeypatch.setattr(li_budget, "_setting_str", lambda key, default: default)
    yield engine
    db_mod.SessionLocal.configure(bind=old)
    engine.dispose()


def test_concurrent_takes_never_exceed_the_cap(filedb):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: li_budget.take("main"), range(60)))
    assert sum(results) == 15 == li_budget.used("main")


def test_concurrent_takes_inside_a_run_respect_its_share_and_the_total(filedb):
    from concurrent.futures import ThreadPoolExecutor
    li_budget.begin_run(2)
    ctx = __import__("contextvars").copy_context()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: ctx.copy().run(li_budget.take, "main"), range(40)))
    assert sum(results) == 8 and li_budget._count("main", "run1") == 8 and li_budget.used("main") == 8


def test_concurrent_run_claims_get_distinct_ordinals(filedb):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = sorted(pool.map(lambda _: li_budget.claim_run(2), range(12)))
    assert got == list(range(1, 13))
