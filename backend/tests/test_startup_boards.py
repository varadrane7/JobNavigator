"""Y Combinator Jobs and Wellfound (scraper/sources/ycombinator.py, wellfound.py): page parsing,
the pages a search reads, filters, and the feed ingest storing their pay and remote flag."""
import html
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from backend.scraper.sources import wellfound as wf
from backend.scraper.sources import ycombinator as yc

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _search(**kw):
    base = dict(id=None, name="t", search_term=None, location=None, direct_url=None, sources=[], hours_old=24,
                max_pages=2, require_salary=False, title_include_keywords=[], title_exclude_keywords=[],
                company_filter=[], company_exclude=[], exclude_active_companies=False, results_wanted=50)
    base.update(kw)
    return SimpleNamespace(**base)


# ── Y Combinator ─────────────────────────────────────────────────────────────

def _yc_post(i, **kw):
    base = {"id": i, "title": f"Software Engineer {i}", "url": f"/companies/acme/jobs/x{i}-swe",
            "location": "New York, NY, US", "type": "Full-time", "role": "eng", "salaryRange": "$150K - $200K",
            "minExperience": "Any (new grads ok)", "visa": "Will sponsor", "companyName": "Acme",
            "companyBatchName": "W24", "companyOneLiner": "Rockets for everyone.", "createdAt": "3 days"}
    return {**base, **kw}


def _yc_page(props):
    data = html.escape(json.dumps({"component": "WaasJobListingsPage", "props": props}), quote=True)
    return f'<html><div id="app" data-page="{data}"></div></html>'


def test_yc_page_props_reads_the_inertia_attribute():
    page = _yc_page({"jobPostings": [_yc_post(1)]})
    assert yc.page_props(page)["jobPostings"][0]["id"] == 1
    assert yc.page_props("<html>no data</html>") == {}


def test_yc_parse_listing():
    j = yc.parse_listing(_yc_post(7, location="Remote (US)", salaryRange="$120K - $160K"), NOW)
    assert j["url"] == "https://www.ycombinator.com/companies/acme/jobs/x7-swe"
    assert (j["salary_min"], j["salary_max"]) == (120_000, 160_000)
    assert j["arrangement"] is True and j["yc_id"] == "7"
    assert j["posted"] == datetime(2026, 10, 29, tzinfo=timezone.utc)
    assert yc.parse_listing(_yc_post(8, location="San Francisco, CA, US"), NOW)["arrangement"] is None


def test_yc_detail_text_puts_visa_and_experience_first():
    j = yc.parse_listing(_yc_post(1), NOW)
    text = yc.detail_text({"job": {"description": "<p>Build the <b>core</b> platform.</p>"}}, j)
    assert text.splitlines()[0] == "Company: Acme (YC W24)"
    assert "Visa: Will sponsor" in text and "Experience: Any (new grads ok)" in text
    assert text.endswith("Build the core platform.")


def test_yc_listing_urls():
    base = "https://www.ycombinator.com/jobs/role/software-engineer"
    urls = yc.listing_urls(_search())
    assert urls[0] == base and f"{base}/backend" in urls and f"{base}/new-york" in urls and f"{base}/remote" in urls
    assert yc.listing_urls(_search(location="NYC, Remote")) == [f"{base}/new-york", f"{base}/remote"]
    # a place YC has no page for is read from the role page and filtered after
    assert yc.listing_urls(_search(location="Denver, Remote")) == [f"{base}/remote", base]
    assert yc.listing_urls(_search(direct_url="https://a/1, https://a/2\nhttps://a/1")) == ["https://a/1", "https://a/2"]


@pytest.mark.asyncio
async def test_yc_fetch_unions_pages_filters_and_reads_only_new_job_pages(monkeypatch):
    from backend.scraper._shared.dedup import make_external_id
    monkeypatch.setattr(yc, "PAGE_PAUSE", 0)
    monkeypatch.setattr(yc, "DETAIL_PAUSE", 0)
    pages = {
        f"{yc.SITE}/jobs/role/software-engineer/new-york": {"jobPostings": [
            _yc_post(1), _yc_post(2, role="sales", title="Account Executive"),
            _yc_post(3, title="Backend Engineer"), _yc_post(4, location="London, UK")]},
        f"{yc.SITE}/jobs/role/software-engineer/remote": {"jobPostings": [
            _yc_post(1), _yc_post(5, location="Remote (US)", salaryRange="")]},
    }
    asked = []

    async def fake_get(client, url):
        asked.append(url)
        if "/companies/" in url:
            return {"job": {"description": f"Posting text for {url}"}}
        return pages.get(url, {})
    monkeypatch.setattr(yc, "_get", fake_get)

    known = {make_external_id("Acme", "Backend Engineer", f"{yc.SITE}/companies/acme/jobs/x3-swe")}
    items = await yc.fetch(_search(location="New York, Remote"), known)
    assert sorted(j["yc_id"] for j in items) == ["1", "3", "5"]   # no sales role, no London, 1 once
    job_pages = [u for u in asked if "/companies/" in u]
    assert sorted(job_pages) == [f"{yc.SITE}/companies/acme/jobs/x1-swe", f"{yc.SITE}/companies/acme/jobs/x5-swe"]
    assert "Posting text" in next(j for j in items if j["yc_id"] == "1")["description"]

    asked.clear()
    items = await yc.fetch(_search(location="New York, Remote", require_salary=True, search_term="backend"), set())
    assert [j["yc_id"] for j in items] == ["3"]


@pytest.mark.asyncio
async def test_yc_fetch_defers_jobs_past_the_page_cap(monkeypatch):
    monkeypatch.setattr(yc, "PAGE_PAUSE", 0)
    monkeypatch.setattr(yc, "DETAIL_PAUSE", 0)

    async def fake_get(client, url):
        if "/companies/" in url:
            return {"job": {"description": "text"}}
        return {"jobPostings": [_yc_post(i) for i in range(5)]}
    monkeypatch.setattr(yc, "_get", fake_get)
    items = await yc.fetch(_search(location="New York"), set(), detail_cap=2)
    assert [bool(j.get("deferred")) for j in items] == [False, False, True, True, True]


# ── Wellfound ────────────────────────────────────────────────────────────────

def _wf_page(jobs, page_count=3, key='seoLandingPageJobSearchResults({"page":1,"role":"software-engineer"})'):
    data = {"ROOT_QUERY": {"talent": {key: {"pageCount": page_count, "startups": [{"__ref": "StartupResult:1"}]}}},
            "StartupResult:1": {"name": "Rocket Co", "highConcept": "Rockets.", "companySize": "SIZE_11_50",
                                "highlightedJobListings": [{"__ref": f"JobListingSearchResult:{j['id']}"} for j in jobs]}}
    for j in jobs:
        data[f"JobListingSearchResult:{j['id']}"] = j
    nd = {"page": "/seoLanding/roleSearch", "props": {"pageProps": {"apolloState": {"data": data}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'


def _wf_job(i, **kw):
    base = {"id": str(i), "slug": "software-engineer", "title": "Software Engineer", "compensation": "$150k – $190k • 0.1% – 0.5%",
            "locationNames": ["New York"], "acceptedRemoteLocationNames": [], "remote": False,
            "liveStartAt": int(NOW.timestamp()) - 86400, "jobType": "full-time", "description": "Build rockets."}
    return {**base, **kw}


def test_wf_parse_page():
    jobs, pages = wf.parse_page(_wf_page([_wf_job(1), _wf_job(2, remote=True, locationNames=[],
                                                              acceptedRemoteLocationNames=["United States"])]))
    assert pages == 3 and len(jobs) == 2
    a, b = jobs
    assert a["url"] == "https://wellfound.com/jobs/1-software-engineer" and a["company"] == "Rocket Co"
    assert (a["salary_min"], a["salary_max"]) == (150_000, 190_000) and a["arrangement"] is None
    assert a["description"].startswith("Company: Rocket Co\nAbout: Rockets.\nSize: 11-50")
    assert a["description"].endswith("Build rockets.")
    assert b["location"] == "Remote (United States)" and b["arrangement"] is True
    with pytest.raises(wf.Blocked):
        wf.parse_page("<html>captcha</html>")


def test_wf_listing_urls():
    assert wf.listing_urls(_search()) == ["https://wellfound.com/role/software-engineer"]
    assert wf.listing_urls(_search(search_term="Backend Engineer, full stack engineer", location="Remote, New York")) == [
        "https://wellfound.com/role/r/backend-engineer", "https://wellfound.com/role/l/backend-engineer/new-york",
        "https://wellfound.com/role/r/full-stack-engineer", "https://wellfound.com/role/l/full-stack-engineer/new-york"]
    assert wf.page_url("https://wellfound.com/role/x", 1) == "https://wellfound.com/role/x"
    assert wf.page_url("https://wellfound.com/role/x", 3) == "https://wellfound.com/role/x?page=3"


def _mock_client(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)
    monkeypatch.setattr(wf.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_wf_fetch_pages_age_and_stop(monkeypatch):
    monkeypatch.setattr(wf, "PAGE_PAUSE", 0)
    old = int(NOW.timestamp()) - 400 * 86400
    seen = []

    def handler(request):
        seen.append(str(request.url))
        page = int(request.url.params.get("page", 1))
        if page == 1:
            return httpx.Response(200, text=_wf_page([_wf_job(1), _wf_job(2, liveStartAt=old)], page_count=5))
        if page == 2:
            return httpx.Response(200, text=_wf_page([_wf_job(1), _wf_job(3)], page_count=5))
        return httpx.Response(403, text="blocked")
    _mock_client(monkeypatch, handler)
    monkeypatch.setattr(wf.time, "time", lambda: NOW.timestamp())

    items, stopped = await wf.fetch(_search(max_pages=3))
    assert [j["wf_id"] for j in items] == ["1", "3"]       # 2 is a year old, 1 is read once
    assert len(seen) == 3 and stopped and "HTTP 403" in stopped

    seen.clear()
    items, stopped = await wf.fetch(_search(max_pages=1))
    assert len(seen) == 1 and stopped is None


# ── shared ingest: the board's own pay and remote flag are stored ───────────

@pytest.mark.asyncio
async def test_ingest_stores_posting_salary_and_board_remote(test_db, monkeypatch):
    from backend.models.db import Job, Search
    from backend.scraper.sources import startup_feeds as feeds

    async def no_h1b(*a, **k):
        return None
    monkeypatch.setattr(feeds, "prefetch_h1b", no_h1b)
    s = Search(name="WF", search_mode="wellfound")
    test_db.add(s)
    test_db.commit()
    jobs, _ = wf.parse_page(_wf_page([_wf_job(9, remote=True, acceptedRemoteLocationNames=["United States"])]))
    out = await feeds.ingest(s, jobs, "wellfound")
    assert out["new_jobs"] == 1
    job = test_db.query(Job).filter(Job.source == "wellfound").one()
    assert (job.salary_min, job.salary_max, job.salary_source) == (150_000, 190_000, "posting")
    assert job.arr_remote is True
