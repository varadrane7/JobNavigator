"""The Telegram nag's wording follows the day's numbers (curator/nag.py)."""
import random
from datetime import date, datetime, timedelta, timezone

from backend.models.db import Application, Contact, Job

CFG = {"apply_t": 10, "req_t": 5, "calendar": {"2026-11-03": "post #1 goes out"}, "url": "https://jn.example"}
DAY = date(2026, 11, 3)


def test_morning_names_picks_and_the_calendar_task():
    from backend.curator.nag import compose
    t = compose({"picks": 20, "apps": 0, "reqs": 0, "due": 4, "streak": 2}, CFG, 9, DAY, random.Random(1))
    assert "post #1 goes out" in t and "https://jn.example/today" in t


def test_behind_vs_done_vs_night():
    from backend.curator.nag import compose, DONE, NIGHT
    behind = compose({"picks": 5, "apps": 3, "reqs": 1, "due": 2, "streak": 3}, CFG, 18, DAY, random.Random(2))
    assert ("3/10" in behind) or ("7 applications" in behind) or ("3 applications" in behind)
    done = compose({"picks": 0, "apps": 12, "reqs": 6, "due": 0, "streak": 5}, CFG, 18, DAY, random.Random(3))
    assert any(done.startswith(x.split("{")[0]) for x in DONE) and "https://" not in done
    night = compose({"picks": 0, "apps": 2, "reqs": 0, "due": 1, "streak": 1}, CFG, 21, DAY, random.Random(4))
    assert night.startswith("Final count for Nov 03") and "post #1" not in night


def test_stats_count_today_only(test_db):
    from backend.curator.nag import stats
    from backend.tz import local_today, day_start
    today = local_today()
    j = Job(external_id="x1", company="A", title="SWE", url="u", status="saved", curated_on=today)
    test_db.add(j)
    test_db.flush()
    test_db.add(Application(job_id=j.id, status="applied", applied_at=day_start(today) + timedelta(hours=10)))
    test_db.add(Contact(name="P", company="A", linkedin_url="l", status="requested",
                        last_action_at=day_start(today) + timedelta(hours=11)))
    test_db.add(Contact(name="Q", company="A", linkedin_url="m", status="new", next_action_at=today))
    test_db.commit()
    s = stats(today)
    assert s == {"picks": 1, "apps": 1, "reqs": 1, "due": 1, "streak": 1}


def test_each_telegram_kind_has_its_own_switch(test_db):
    from backend.models.db import Setting
    from backend.notifier import telegram as tg
    test_db.add(Setting(key="telegram_enabled", value="true"))
    test_db.add(Setting(key="telegram_job_alerts", value="false"))
    test_db.commit()
    assert tg.feature_on("telegram_job_alerts") is False
    assert tg.feature_on("telegram_nag") is True          # unset reads as on
    test_db.query(Setting).filter(Setting.key == "telegram_enabled").update({"value": "false"})
    test_db.commit()
    assert tg.feature_on("telegram_nag") is False         # the master switch wins
