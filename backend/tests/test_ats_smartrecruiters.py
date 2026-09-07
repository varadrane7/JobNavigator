"""Tests for ats/smartrecruiters.py — detection + paginated API mock."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock


# ── Detection ────────────────────────────────────────────────────────────────

def test_is_smartrecruiters_detects_jobs_host():
    from backend.scraper.ats.smartrecruiters import is_smartrecruiters
    assert is_smartrecruiters("https://jobs.smartrecruiters.com/Avaloq1")


def test_is_smartrecruiters_detects_careers_host():
    from backend.scraper.ats.smartrecruiters import is_smartrecruiters
    assert is_smartrecruiters("https://careers.smartrecruiters.com/Avaloq1")


def test_is_smartrecruiters_detects_api_host():
    from backend.scraper.ats.smartrecruiters import is_smartrecruiters
    assert is_smartrecruiters(
        "https://api.smartrecruiters.com/v1/companies/Avaloq1/postings"
    )


def test_is_smartrecruiters_rejects_other_ats():
    from backend.scraper.ats.smartrecruiters import is_smartrecruiters
    assert not is_smartrecruiters("https://jobs.lever.co/acme")
    assert not is_smartrecruiters("https://boards.greenhouse.io/acme")
    assert not is_smartrecruiters("https://nvidia.wd5.myworkdayjobs.com/Site")


def test_is_smartrecruiters_rejects_path_injection():
    """Attacker-controlled path with smartrecruiters.com substring must not match."""
    from backend.scraper.ats.smartrecruiters import is_smartrecruiters
    assert not is_smartrecruiters("https://attacker.com/?u=smartrecruiters.com")
    assert not is_smartrecruiters("https://evil.com/jobs.smartrecruiters.com")


def test_is_smartrecruiters_handles_empty_url():
    from backend.scraper.ats.smartrecruiters import is_smartrecruiters
    assert not is_smartrecruiters("")
    assert not is_smartrecruiters(None)  # type: ignore[arg-type]


# ── Slug extraction ─────────────────────────────────────────────────────────

def test_extract_company_slug_from_jobs_host():
    from backend.scraper.ats.smartrecruiters import _extract_company_slug
    assert _extract_company_slug("https://jobs.smartrecruiters.com/Avaloq1") == "Avaloq1"
    assert _extract_company_slug("https://jobs.smartrecruiters.com/Avaloq1/") == "Avaloq1"
    assert _extract_company_slug(
        "https://jobs.smartrecruiters.com/Avaloq1/744000123-some-slug"
    ) == "Avaloq1"


def test_extract_company_slug_from_api_host():
    from backend.scraper.ats.smartrecruiters import _extract_company_slug
    assert _extract_company_slug(
        "https://api.smartrecruiters.com/v1/companies/Avaloq1/postings"
    ) == "Avaloq1"


def test_extract_company_slug_returns_none_when_missing():
    from backend.scraper.ats.smartrecruiters import _extract_company_slug
    assert _extract_company_slug("https://jobs.smartrecruiters.com/") is None
    assert _extract_company_slug("https://api.smartrecruiters.com/v1/health") is None


# ── Scrape ──────────────────────────────────────────────────────────────────

def _make_response(status_code=200, body=None):
    body = body or {}
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = json.dumps(body)
    resp.json = MagicMock(return_value=body)
    resp.raise_for_status = MagicMock()
    return resp


def _patch_client(monkeypatch, responses):
    """Set up an AsyncClient mock that returns `responses` in order."""
    iterator = iter(responses)

    async def _get(_url, *a, **kw):
        try:
            return next(iterator)
        except StopIteration:
            return _make_response(404, {})

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=_get)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock_client)
    return mock_client


@pytest.mark.asyncio
async def test_scrape_parses_single_page(monkeypatch):
    body = {
        "offset": 0,
        "limit": 100,
        "totalFound": 2,
        "content": [
            {"id": "744000000000001", "name": "Senior Product Manager"},
            {"id": "744000000000002", "name": "Staff Software Engineer"},
        ],
    }
    _patch_client(monkeypatch, [_make_response(200, body)])

    from backend.scraper.ats.smartrecruiters import scrape
    result = await scrape("https://jobs.smartrecruiters.com/Avaloq1")
    jobs = result[0] if isinstance(result, tuple) else result

    assert len(jobs) == 2
    assert jobs[0]["title"] == "Senior Product Manager"
    assert jobs[0]["url"] == "https://jobs.smartrecruiters.com/Avaloq1/744000000000001"
    assert jobs[1]["title"] == "Staff Software Engineer"


@pytest.mark.asyncio
async def test_scrape_paginates_until_short_page(monkeypatch):
    """Stop pagination when a returned page has fewer items than the page limit."""
    # Force smaller page size for the test so we don't have to fabricate 100 entries.
    monkeypatch.setattr(
        "backend.scraper.ats.smartrecruiters._PAGE_LIMIT", 2
    )
    page1 = {
        "offset": 0, "limit": 2, "totalFound": 3,
        "content": [
            {"id": "1111111111", "name": "Product Manager One"},
            {"id": "2222222222", "name": "Product Manager Two"},
        ],
    }
    page2 = {
        "offset": 2, "limit": 2, "totalFound": 3,
        "content": [
            {"id": "3333333333", "name": "Product Manager Three"},
        ],
    }
    client = _patch_client(monkeypatch, [
        _make_response(200, page1),
        _make_response(200, page2),
    ])

    from backend.scraper.ats.smartrecruiters import scrape
    result = await scrape("https://jobs.smartrecruiters.com/Avaloq1")
    jobs = result[0] if isinstance(result, tuple) else result

    assert len(jobs) == 3
    titles = [j["title"] for j in jobs]
    assert titles == ["Product Manager One", "Product Manager Two", "Product Manager Three"]
    # Two GETs — page1 (full), page2 (short, terminates loop)
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_scrape_paginates_until_total_reached(monkeypatch):
    """Stop pagination when offset >= totalFound even if page is full."""
    monkeypatch.setattr(
        "backend.scraper.ats.smartrecruiters._PAGE_LIMIT", 2
    )
    page1 = {
        "offset": 0, "limit": 2, "totalFound": 4,
        "content": [
            {"id": "11", "name": "Product Manager One"},
            {"id": "22", "name": "Product Manager Two"},
        ],
    }
    page2 = {
        "offset": 2, "limit": 2, "totalFound": 4,
        "content": [
            {"id": "33", "name": "Product Manager Three"},
            {"id": "44", "name": "Product Manager Four"},
        ],
    }
    client = _patch_client(monkeypatch, [
        _make_response(200, page1),
        _make_response(200, page2),
        _make_response(200, {"content": []}),  # safety: would loop forever otherwise
    ])

    from backend.scraper.ats.smartrecruiters import scrape
    result = await scrape("https://jobs.smartrecruiters.com/Avaloq1")
    jobs = result[0] if isinstance(result, tuple) else result

    assert len(jobs) == 4
    # offset reaches totalFound after page2 → no third GET
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_scrape_handles_http_error(monkeypatch):
    _patch_client(monkeypatch, [_make_response(503, {})])

    from backend.scraper.ats.smartrecruiters import scrape
    result = await scrape("https://jobs.smartrecruiters.com/Avaloq1")
    jobs = result[0] if isinstance(result, tuple) else result
    assert jobs == []


@pytest.mark.asyncio
async def test_scrape_debug_surfaces_mid_pagination_http_error(monkeypatch):
    """A 5xx on page 2 must still be reported in debug-mode rejections,
    even when page 1 returned valid jobs (partial-success contract)."""
    monkeypatch.setattr(
        "backend.scraper.ats.smartrecruiters._PAGE_LIMIT", 2
    )
    page1 = {
        "offset": 0, "limit": 2, "totalFound": 4,
        "content": [
            {"id": "11", "name": "Senior Product Manager"},
            {"id": "22", "name": "Staff Software Engineer"},
        ],
    }
    _patch_client(monkeypatch, [
        _make_response(200, page1),
        _make_response(503, {}),
    ])

    from backend.scraper.ats.smartrecruiters import scrape
    jobs, rejected = await scrape("https://jobs.smartrecruiters.com/Avaloq1", debug=True)
    assert len(jobs) == 2
    assert any("HTTP 503" in r["reason"] for r in rejected)


@pytest.mark.asyncio
async def test_scrape_debug_surfaces_invalid_json(monkeypatch):
    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.text = "not-json{{"
    _patch_client(monkeypatch, [bad_resp])

    from backend.scraper.ats.smartrecruiters import scrape
    jobs, rejected = await scrape("https://jobs.smartrecruiters.com/Avaloq1", debug=True)
    assert jobs == []
    assert any("Invalid JSON" in r["reason"] for r in rejected)


@pytest.mark.asyncio
async def test_scrape_handles_empty_content(monkeypatch):
    _patch_client(monkeypatch, [_make_response(200, {
        "offset": 0, "limit": 100, "totalFound": 0, "content": []
    })])

    from backend.scraper.ats.smartrecruiters import scrape
    result = await scrape("https://jobs.smartrecruiters.com/Avaloq1")
    jobs = result[0] if isinstance(result, tuple) else result
    assert jobs == []


@pytest.mark.asyncio
async def test_scrape_validates_titles(monkeypatch):
    """Garbage titles (too short, generic) must be rejected by _validate_job."""
    body = {
        "offset": 0, "limit": 100, "totalFound": 3,
        "content": [
            {"id": "11", "name": "Apply"},                    # garbage
            {"id": "22", "name": "Senior Product Manager"},   # valid
            {"id": "33", "name": "Hi"},                       # too short
        ],
    }
    _patch_client(monkeypatch, [_make_response(200, body)])

    from backend.scraper.ats.smartrecruiters import scrape
    result = await scrape("https://jobs.smartrecruiters.com/Avaloq1")
    jobs = result[0] if isinstance(result, tuple) else result
    assert [j["title"] for j in jobs] == ["Senior Product Manager"]


@pytest.mark.asyncio
async def test_scrape_skips_postings_missing_id(monkeypatch):
    body = {
        "offset": 0, "limit": 100, "totalFound": 2,
        "content": [
            {"name": "Senior Product Manager"},               # missing id
            {"id": "22", "name": "Staff Software Engineer"},  # valid
        ],
    }
    _patch_client(monkeypatch, [_make_response(200, body)])

    from backend.scraper.ats.smartrecruiters import scrape
    result = await scrape("https://jobs.smartrecruiters.com/Avaloq1")
    jobs = result[0] if isinstance(result, tuple) else result
    assert [j["title"] for j in jobs] == ["Staff Software Engineer"]


@pytest.mark.asyncio
async def test_scrape_forwards_country_filter(monkeypatch):
    """?country=ch in input URL must be forwarded to the API URL."""
    captured_urls = []

    async def _capture(_url, *a, **kw):
        captured_urls.append(_url)
        return _make_response(200, {"offset": 0, "limit": 100, "totalFound": 0, "content": []})

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=_capture)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock_client)

    from backend.scraper.ats.smartrecruiters import scrape
    await scrape("https://careers.smartrecruiters.com/Avaloq1?country=ch")
    assert captured_urls
    assert "country=ch" in captured_urls[0]


@pytest.mark.asyncio
async def test_scrape_forwards_city_and_q(monkeypatch):
    """?city= and ?q= are also whitelisted forwardable filters."""
    captured_urls = []

    async def _capture(_url, *a, **kw):
        captured_urls.append(_url)
        return _make_response(200, {"offset": 0, "limit": 100, "totalFound": 0, "content": []})

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=_capture)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock_client)

    from backend.scraper.ats.smartrecruiters import scrape
    await scrape("https://careers.smartrecruiters.com/Avaloq1?city=Zurich&q=banking")
    assert captured_urls
    assert "city=Zurich" in captured_urls[0]
    assert "q=banking" in captured_urls[0]


@pytest.mark.asyncio
async def test_scrape_drops_unknown_query_params(monkeypatch):
    """Unknown params (e.g. customField) are NOT forwarded to keep dispatch deterministic."""
    captured_urls = []

    async def _capture(_url, *a, **kw):
        captured_urls.append(_url)
        return _make_response(200, {"offset": 0, "limit": 100, "totalFound": 0, "content": []})

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=_capture)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock_client)

    from backend.scraper.ats.smartrecruiters import scrape
    await scrape(
        "https://careers.smartrecruiters.com/Avaloq1?customField.abc=xyz&utm_source=spam"
    )
    assert captured_urls
    assert "customField" not in captured_urls[0]
    assert "utm_source" not in captured_urls[0]


@pytest.mark.asyncio
async def test_scrape_no_company_slug_returns_empty(monkeypatch):
    # No HTTP call should happen; pre-flight check returns empty.
    from backend.scraper.ats.smartrecruiters import scrape
    result = await scrape("https://jobs.smartrecruiters.com/")
    jobs = result[0] if isinstance(result, tuple) else result
    assert jobs == []


@pytest.mark.asyncio
async def test_scrape_debug_returns_rejected_with_reasons(monkeypatch):
    body = {
        "offset": 0, "limit": 100, "totalFound": 1,
        "content": [
            {"id": "11", "name": "Apply"},  # garbage
            {"id": "22", "name": "Senior Product Manager"},
        ],
    }
    _patch_client(monkeypatch, [_make_response(200, body)])

    from backend.scraper.ats.smartrecruiters import scrape
    result = await scrape("https://jobs.smartrecruiters.com/Avaloq1", debug=True)
    assert isinstance(result, tuple)
    jobs, rejected = result
    assert [j["title"] for j in jobs] == ["Senior Product Manager"]
    assert any("Apply" in r["title"] for r in rejected)


# ── Detect_scrape_type integration ──────────────────────────────────────────

def test_detect_scrape_type_returns_smartrecruiters():
    from backend.api.routes_companies import detect_scrape_type
    assert detect_scrape_type(
        "https://careers.smartrecruiters.com/Avaloq1"
    ) == "SmartRecruiters API"
    assert detect_scrape_type(
        "https://jobs.smartrecruiters.com/Avaloq1"
    ) == "SmartRecruiters API"
