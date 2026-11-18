"""Workday requests name the tool and are spaced by workday_request_pause_seconds (ats/workday.py)."""
import asyncio
import time

from backend.scraper.ats import workday
from backend.scraper.ats.workday import request_pause as real_request_pause   # before conftest stubs it


def test_requests_carry_an_honest_user_agent():
    assert workday._HEADERS["User-Agent"].startswith("JobNavigator/")
    assert workday.DETAIL_HEADERS["User-Agent"] == workday.USER_AGENT
    assert "personal job search" in workday.USER_AGENT


def test_pace_spaces_requests_by_the_configured_pause(monkeypatch):
    monkeypatch.setattr(workday, "request_pause", lambda: 0.2)
    workday._last_request["at"] = 0.0

    async def three():
        stamps = []
        for _ in range(3):
            await workday.pace()
            stamps.append(time.monotonic())
        return stamps

    s = asyncio.run(three())
    assert s[1] - s[0] >= 0.19 and s[2] - s[1] >= 0.19


def test_concurrent_callers_take_turns(monkeypatch):
    monkeypatch.setattr(workday, "request_pause", lambda: 0.15)
    workday._last_request["at"] = 0.0

    async def many():
        stamps = []

        async def one():
            await workday.pace()
            stamps.append(time.monotonic())
        await asyncio.gather(*(one() for _ in range(3)))
        return sorted(stamps)

    s = asyncio.run(many())
    assert s[2] - s[0] >= 0.29


def test_pause_setting_is_read_and_bad_values_fall_back(monkeypatch):
    import backend.accounts.settings as settings
    for raw, want in (("7", 7.0), ("-2", 0.0), ("abc", workday.DEFAULT_PAUSE), ("", workday.DEFAULT_PAUSE)):
        workday._pause_cache.update(value=None, at=0.0)
        monkeypatch.setattr(settings, "get_setting", lambda db, key, default=None, r=raw: r)
        assert real_request_pause() == want
