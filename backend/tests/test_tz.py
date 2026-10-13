"""backend/tz.py: one app zone for day boundaries and the scheduler."""
from datetime import date, datetime, timezone

from backend import tz


def test_default_zone_is_new_york_when_tz_unset(monkeypatch):
    monkeypatch.delenv("APP_TZ", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    assert tz._zone_name() == "America/New_York"


def test_bad_zone_falls_back(monkeypatch):
    monkeypatch.setenv("APP_TZ", "Not/AZone")
    assert tz._zone_name() == "America/New_York"


def test_day_start_is_local_midnight_in_utc():
    if tz.APP_TZ_NAME != "America/New_York":
        return
    assert tz.day_start(date(2026, 10, 13)) == datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)   # EDT
    assert tz.day_start(date(2026, 12, 1)) == datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc)     # EST


def test_day_bounds_span_dst_end():
    if tz.APP_TZ_NAME != "America/New_York":
        return
    start, end = tz.day_bounds(date(2026, 11, 1))
    assert (end - start).total_seconds() == 25 * 3600


def test_scheduler_runs_in_app_zone():
    from backend.scheduler import scheduler
    assert str(scheduler.timezone) == tz.APP_TZ_NAME
