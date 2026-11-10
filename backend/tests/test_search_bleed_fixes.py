"""Curator without H-1B filings, Gmail outages and lost emails, ZipRecruiter "$n" timestamps."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from backend.curator import run, scoring
from backend.email_monitor import gmail_client
from backend.scraper.sources import jobspy as jobspy_source


def _job(**kw):
    return {"id": 1, "company": "Tiny Startup", "title": "Backend Engineer", "description": "Build APIs in Python.",
            "jd_flag": False, "h1b_verdict": "unlikely", **kw}


def test_composite_leaves_out_an_unknown_part():
    parts = {"fit": 0.8, "fresh": 0.8, "visa_comp": None, "reply": 0.8, "stability": 0.8, "tracked": 0.8}
    assert scoring.composite(parts) == pytest.approx(0.8, abs=1e-4)


def test_small_filing_count_is_no_longer_dropped_when_filings_are_ignored():
    assert run.hard_drop(_job(), [], [], 6, drop_unlikely=False) is None
    assert run.hard_drop(_job(), [], [], 6, drop_unlikely=True) == "unlikely H-1B sponsor"


def test_a_posting_that_rules_out_visa_holders_is_still_dropped():
    assert run.hard_drop(_job(description="Must be a US citizen."), [], [], 6, drop_unlikely=False)


def test_rank_with_filings_ignored_treats_filer_and_non_filer_alike(monkeypatch):
    monkeypatch.setattr(run, "_resume_names", lambda: {})
    now = datetime.now(timezone.utc)
    base = {"title": "Backend Engineer", "url": "https://x.test/1", "best_cv": None, "best_cv_score": 80,
            "posted_at": now, "description": "", "jd_flag": False, "salary_min": None, "salary_max": None,
            "tracked": False, "embed_pct": 0.5, "loc_country": "US"}
    jobs = [{**base, "id": 1, "company": "Big Filer", "lca": 900, "approval": 99},
            {**base, "id": 2, "company": "New Startup", "lca": None, "approval": None}]
    cfg = {"target_min": 115000, "target_max": 150000, "weights": scoring.DEFAULT_WEIGHTS, "countries": ["US"],
           "ignore_h1b": True}
    ranked = run.rank(jobs, {}, {}, {"big filer": 200000}, cfg, now)
    assert ranked[0]["score"] == ranked[1]["score"]
    assert all(j["parts"]["visa_comp"] is None and "LCA" not in j["reason"] for j in ranked)


def test_rank_still_uses_filings_by_default(monkeypatch):
    monkeypatch.setattr(run, "_resume_names", lambda: {})
    now = datetime.now(timezone.utc)
    base = {"title": "Backend Engineer", "url": "https://x.test/1", "best_cv": None, "best_cv_score": 80,
            "posted_at": now, "description": "", "jd_flag": False, "salary_min": None, "salary_max": None,
            "tracked": False, "embed_pct": 0.5, "loc_country": "US"}
    jobs = [{**base, "id": 1, "company": "Big Filer", "lca": 900, "approval": 99},
            {**base, "id": 2, "company": "New Startup", "lca": None, "approval": None}]
    cfg = {"target_min": 115000, "target_max": 150000, "weights": scoring.DEFAULT_WEIGHTS, "countries": ["US"]}
    ranked = run.rank(jobs, {}, {}, {}, cfg, now)
    assert ranked[0]["company"] == "Big Filer" and ranked[0]["score"] > ranked[1]["score"]


def test_zip_bigint_timestamps_become_numbers():
    raw = {"status": {"postedAtUtc": {"seconds": "$n1794183969"}}, "title": "$note stays", "a": ["$n5"]}
    assert jobspy_source._unwrap_page_bigints(raw) == {
        "status": {"postedAtUtc": {"seconds": 1794183969}}, "title": "$note stays", "a": [5]}


def test_lookback_reaches_the_last_check_that_worked():
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    assert gmail_client._lookback_days({}, now) == gmail_client.DEFAULT_LOOKBACK_DAYS
    assert gmail_client._lookback_days({"last": (now - timedelta(hours=1)).isoformat()}, now) == 3
    assert gmail_client._lookback_days({"last": (now - timedelta(days=19)).isoformat()}, now) == 21
    assert gmail_client._lookback_days({"last": (now - timedelta(days=400)).isoformat()}, now) == 60


@pytest.mark.parametrize("token,raises", [("a-token", True), ("", False)])
@pytest.mark.asyncio
async def test_a_refused_token_fails_the_run_but_no_token_stays_quiet(monkeypatch, token, raises):
    from backend.email_monitor import gmail_auth

    async def no_access():
        return ""
    monkeypatch.setattr(gmail_client, "_get_access_token", no_access)
    monkeypatch.setattr(gmail_client, "SessionLocal", MagicMock())
    monkeypatch.setattr(gmail_auth, "resolve_token", lambda db: (token, "account" if token else None))
    if raises:
        with pytest.raises(gmail_client.GmailTokenRefused):
            await gmail_client.check_emails()
    else:
        assert await gmail_client.check_emails() is None


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data, self.text = data, ""

    def json(self):
        return self._data


class _Client:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        if url.endswith("/messages"):
            return _Resp({"messages": [{"id": "m1"}]})
        return _Resp({"payload": {"headers": [{"name": "From", "value": "a@b.co"}, {"name": "Subject", "value": "Hi"}]}})


@pytest.mark.parametrize("enabled,kept", [("true", False), ("false", True)])
@pytest.mark.asyncio
async def test_an_email_the_model_could_not_read_is_retried(monkeypatch, enabled, kept):
    from backend.email_monitor import response_parser
    import backend.activity as activity

    async def token():
        return "t"

    async def no_answer(*a):
        return None

    async def no_sleep(*a):
        return None
    saved = {}
    monkeypatch.setattr(gmail_client, "_get_access_token", token)
    monkeypatch.setattr(gmail_client, "SessionLocal", MagicMock())
    monkeypatch.setattr(gmail_client, "_load_processed_ids", lambda db: set())
    monkeypatch.setattr(gmail_client, "_save_processed_ids", lambda db, ids: saved.update(ids=set(ids)))
    monkeypatch.setattr(gmail_client, "_build_gmail_query", lambda db, days: "q")
    monkeypatch.setattr(gmail_client, "_load_check_state", lambda db: {})
    monkeypatch.setattr(gmail_client, "get_setting", lambda db, key: enabled)
    monkeypatch.setattr(gmail_client, "set_setting", lambda *a, **kw: None)
    monkeypatch.setattr(gmail_client, "classify_email", lambda s, b: {"classification": "unknown", "confidence": 0.1})
    monkeypatch.setattr(gmail_client, "_get_active_apps_for_llm", lambda db: [])
    monkeypatch.setattr(gmail_client.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(gmail_client.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(response_parser, "classify_email_llm", no_answer)
    monkeypatch.setattr(activity, "log_activity", lambda *a, **kw: None)
    await gmail_client.check_emails()
    assert ("m1" in saved["ids"]) is kept
