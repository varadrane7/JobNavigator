"""The curator's LinkedIn browser: which saved session it uses, the move to the main session on a
429, and the per-session daily caps (curator/li_budget.py).

Regression: linkedin_people imported linkedin_extension._SESSION_PATH, a constant that the
per-account sessions (MT-16) replaced with a function; the ImportError was swallowed by the
curator and no person was found for ten days."""
import json

import pytest

from backend.curator import li_budget
from backend.curator import linkedin_people as lpm


class FakePage:
    def __init__(self, throttled: set):
        self.url = "about:blank"
        self.throttled = throttled      # sessions LinkedIn answers with a 429
        self.session = None

        class _Mouse:
            async def wheel(self, *a):
                return None
        self.mouse = _Mouse()

    async def goto(self, url, **kw):
        self.url = url

    async def evaluate(self, js, *a):
        return [{"url": "https://www.linkedin.com/in/jane", "text": "Jane Doe\n• 2nd\nEngineering Manager at Acme\nNew York, NY"}]


class FakeContext:
    def __init__(self, page):
        self.page = page

    async def clear_cookies(self):
        self.page.session = None

    async def add_cookies(self, cookies):
        self.page.session = cookies[0]["value"]


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    """Two saved jars (mock, main); LinkedIn answers per the page's `throttled` set."""
    from backend import refresh_linkedin_session as rls
    from backend.scraper.sources import linkedin_personal as lp
    from backend.scraper._shared import linkedin_lookup as ll

    jars = {}
    for which in ("mock", "main"):
        p = tmp_path / f"{which}.json"
        p.write_text(json.dumps([{"name": "li_at", "value": which}]))
        jars[which] = str(p)
    monkeypatch.setattr(rls, "jar", lambda which="mock": jars[which])
    page = FakePage(set())

    async def browser():
        return object(), None, FakeContext(page), page

    async def me_status(p):
        return 200

    async def rate_limited(p):
        return p.session in p.throttled

    async def no_sleep(*a):
        return None

    class _DB:
        def close(self):
            pass

    monkeypatch.setattr(lp, "_get_linkedin_browser", browser)
    monkeypatch.setattr(lp, "_voyager_me_status", me_status)
    monkeypatch.setattr(lp, "_check_rate_limit", rate_limited)
    monkeypatch.setattr(ll, "mock_session_problem", lambda db: None)
    monkeypatch.setattr(lpm, "SessionLocal", _DB)
    monkeypatch.setattr(lpm.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(li_budget, "_setting_int", lambda key, default: default)
    li_budget.reset_all()
    yield page
    li_budget.reset_all()


def test_module_names_no_removed_session_constant():
    src = open(lpm.__file__).read()
    assert "_SESSION_PATH" not in src and "COOKIE_PATH" not in src


async def test_search_runs_on_the_mock_session_first(sessions):
    li = lpm.LinkedInPeople()
    out = await li.search("engineering manager Acme", country="US")
    assert li.account == "mock"
    assert out and out[0]["source"] == "linkedin_mock" and out[0]["degree"] == 2
    assert li_budget.used("mock") == 1 and li_budget.used("main") == 0


async def test_a_429_on_mock_moves_to_main_and_cools_mock(sessions):
    sessions.throttled.add("mock")
    li = lpm.LinkedInPeople()
    out = await li.search("founder Acme")
    assert li.account == "main" and out and out[0]["source"] == "linkedin_main"
    assert li_budget.cooling_until("mock") is not None
    # the next search goes straight to main
    await li.search("cto Acme")
    assert li_budget.used("mock") == 1 and li_budget.used("main") == 2


async def test_network_search_runs_on_main_only(sessions):
    li = lpm.LinkedInPeople()
    await li.search("recruiter Acme", network=["S", "O"])
    assert li.account == "main" and "network=" in sessions.url
    assert li_budget.used("mock") == 0


async def test_both_throttled_gives_nothing(sessions):
    sessions.throttled.update({"mock", "main"})
    li = lpm.LinkedInPeople()
    assert await li.search("founder Acme") == []


def test_daily_cap_per_session(sessions, monkeypatch):
    monkeypatch.setattr(li_budget, "_setting_int", lambda key, default: 1 if key == "linkedin_mock_daily_cap" else default)
    assert li_budget.take("mock") is True
    assert li_budget.take("mock") is False
    assert li_budget.take("main") is True
    st = li_budget.status()
    assert st["mock"] == {"used": 1, "cap": 1, "cooling_until": None}


@pytest.mark.parametrize("text,deg", [("Jane\n• 2nd\nEM", 2), ("Bob\n• 3rd+\nCTO", 3), ("Ann\n• 1st", 1), ("Joe\nCTO", None)])
def test_degree_of(text, deg):
    assert lpm.degree_of(text) == deg
