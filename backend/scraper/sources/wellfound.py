"""Wellfound (formerly AngelList Talent) startup jobs (search mode `wellfound`), no account.

Wellfound's public role pages embed their results (Next.js `__NEXT_DATA__`, an Apollo cache):
20 startups a page, each with its highlighted jobs, and each job with its full description,
pay range, locations, remote flag and the time it went live. Pages are read in order with
`?page=N`, up to the search's `max_pages`.

What a search reads:
- `direct_url`: one or more wellfound.com/role/... URLs (comma or newline separated). Otherwise:
- `search_term`: the role, as Wellfound's slug ("software engineer" -> software-engineer;
  comma-separated for several roles: "backend engineer, full stack engineer").
- `location`: one place per page set; "remote" reads the remote page (/role/r/<role>), any
  other place its location page (/role/l/<role>/<place>), none the role page.
- `country` (the US unless set otherwise): a remote job whose own list of remote countries
  leaves the US out is dropped; one that takes the US is labelled "Remote: United States".
- `hours_old`: postings live longer than that are left out, with a floor of MIN_AGE_DAYS
  (Wellfound ranks by startup, not date, so most pages mix new and older jobs).
- title include/exclude keywords, company exclude and `require_salary` as elsewhere.

The pages answer an ordinary browser request; a refusal (403, or a page without data) stops
the run with the pages read so far, and says so.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from backend.models.db import Search
from backend.scraper.sources import startup_feeds as feeds

logger = logging.getLogger("jobnavigator.wellfound")

SITE = "https://wellfound.com"
DEFAULT_ROLE = "software-engineer"
DEFAULT_PAGES = 3
MAX_PAGES = 10
MIN_AGE_DAYS = 30
PAGE_PAUSE = 4.0
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/141.0 Safari/537.36",
       "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}
_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class Blocked(Exception):
    """Wellfound answered with something other than a results page."""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def listing_urls(search: Search) -> list[str]:
    """The first page of each page set one run reads."""
    direct = [u.strip() for u in re.split(r"[,\n]", search.direct_url or "") if u.strip()]
    if direct:
        return list(dict.fromkeys(direct))
    roles = [slug(r) for r in (search.search_term or "").split(",") if slug(r)] or [DEFAULT_ROLE]
    places = [slug(p) for p in (search.location or "").split(",") if slug(p)]
    urls = []
    for role in roles:
        if not places:
            urls.append(f"{SITE}/role/{role}")
        for place in places:
            urls.append(f"{SITE}/role/r/{role}" if place == "remote" else f"{SITE}/role/l/{role}/{place}")
    return list(dict.fromkeys(urls))


def page_url(url: str, page: int) -> str:
    if page <= 1:
        return url
    return f"{url}{'&' if '?' in url else '?'}page={page}"


def parse_page(text: str) -> tuple[list[dict], int]:
    """(jobs, page count) of one results page; raises Blocked when it holds no results."""
    m = _NEXT.search(text or "")
    if not m:
        raise Blocked("no page data (likely a bot check)")
    data = ((json.loads(m.group(1)).get("props") or {}).get("pageProps") or {}).get("apolloState") or {}
    data = data.get("data", data)
    talent = (data.get("ROOT_QUERY") or {}).get("talent") or {}
    key = next((k for k in talent if k.startswith("seoLandingPageJobSearchResults")), None)
    if key is None:
        raise Blocked("no search results on the page")
    results = talent[key] or {}
    jobs = []
    for ref in results.get("startups") or []:
        st = data.get(ref.get("__ref")) or {}
        for jref in st.get("highlightedJobListings") or []:
            j = data.get(jref.get("__ref")) or {}
            if j:
                jobs.append(parse_job(j, st))
    return jobs, int(results.get("pageCount") or 1)


# Remote regions that let someone in the US work a remote job; a job with no list says nothing.
US_REMOTE = {"united states", "usa", "us", "north america", "americas", "worldwide", "anywhere", "global"}


def remote_for_us(accepted: list) -> bool | None:
    """True / False when the job's remote list does / does not take the US, None without a list."""
    names = [str(a).strip().lower() for a in accepted or [] if str(a).strip()]
    if not names:
        return None
    return any(n in US_REMOTE or feeds.is_us(a) and n != "remote" for n, a in zip(names, accepted))


def parse_job(j: dict, startup: dict) -> dict:
    from backend.scraper.sources.jobright import _parse_salary
    pay = (j.get("compensation") or "").split("•")[0].strip()
    salary_min, salary_max = _parse_salary(pay)
    places = j.get("locationNames") or []
    remote_in = j.get("acceptedRemoteLocationNames") or []
    loc = ", ".join(places)
    us_remote = remote_for_us(remote_in) if j.get("remote") else None
    if us_remote:
        remote_in = ["United States"]   # the full list (40 countries) misleads the place parser
    if j.get("remote"):
        loc = f"{loc} (Remote{': ' + ', '.join(remote_in) if remote_in else ''})" if loc \
            else f"Remote{' (' + ', '.join(remote_in) + ')' if remote_in else ''}"
    live = j.get("liveStartAt")
    try:
        posted = datetime.fromtimestamp(int(live), timezone.utc) if live else None
    except (TypeError, ValueError):
        posted = None
    company = (startup.get("name") or "").strip()
    about = (startup.get("highConcept") or "").strip()
    facts = [f"{k}: {v}" for k, v in (("Company", company), ("About", about),
                                      ("Size", (startup.get("companySize") or "").replace("SIZE_", "").replace("_", "-")),
                                      ("Compensation", j.get("compensation") or ""),
                                      ("Type", j.get("jobType") or "")) if v]
    body = (j.get("description") or "").strip()
    return {"title": (j.get("title") or "").strip(), "company": company,
            "url": f"{SITE}/jobs/{j.get('id')}-{j.get('slug') or slug(j.get('title') or '')}",
            "location": loc, "posted": posted,
            "description": ("\n".join(facts) + ("\n\n" + body if body else "")).strip() or None,
            "salary_min": salary_min, "salary_max": salary_max,
            "arrangement": True if j.get("remote") else None, "wf_id": str(j.get("id") or ""),
            "us_remote": us_remote}


async def fetch(search: Search) -> tuple[list[dict], str | None]:
    """(matching jobs, the reason reading stopped early or None)."""
    pages = max(1, min(int(getattr(search, "max_pages", None) or DEFAULT_PAGES), MAX_PAGES))
    max_age = max(int(search.hours_old or 24), MIN_AGE_DAYS * 24) * 3600
    now = time.time()
    seen, items, stopped = set(), [], None
    async with httpx.AsyncClient(timeout=30, headers=_UA, follow_redirects=True) as client:
        first = True
        for base in listing_urls(search):
            count = pages
            page = 1
            while page <= count:
                if not first:
                    await asyncio.sleep(PAGE_PAUSE)
                first = False
                url = page_url(base, page)
                r = await client.get(url)
                if r.status_code == 404:
                    logger.info(f"Wellfound page not found: {url}")
                    break
                try:
                    if r.status_code != 200:
                        raise Blocked(f"HTTP {r.status_code}")
                    jobs, page_count = parse_page(r.text)
                except Blocked as e:
                    stopped = f"Wellfound stopped answering at {url}: {e}"
                    logger.warning(stopped)
                    break
                count = min(pages, page_count)
                for j in jobs:
                    if j["wf_id"] in seen:
                        continue
                    seen.add(j["wf_id"])
                    items.append(j)
                page += 1
            if stopped:
                break
    place_filter = None if (search.direct_url or "").strip() else search.location
    us = (getattr(search, "country", None) or "usa").lower() in ("usa", "us", "united states")
    items = [j for j in items
             if (j["posted"] is None or now - j["posted"].timestamp() <= max_age)
             and (j["salary_min"] or not getattr(search, "require_salary", False))
             and _place_ok(j["location"], place_filter)
             and not (us and j["us_remote"] is False)]
    return items, stopped


def _place_ok(location: str, wanted: str | None) -> bool:
    """A page for a place already holds that place's jobs (and remote ones on remote pages):
    only "united states" is checked again, as Wellfound's US page also lists other countries."""
    places = [p.strip().lower() for p in (wanted or "").split(",") if p.strip()]
    if not any(p in ("united states", "usa", "us") for p in places):
        return True
    low = (location or "").lower()
    return feeds.is_us(location) or "united states" in low or any(
        p in low for p in places if p not in ("united states", "usa", "us"))


async def run(search: Search) -> dict:
    start = time.time()
    try:
        items, stopped = await fetch(search)
        logger.info(f"wellfound '{search.name}': {len(items)} listings match")
        if stopped and not items:
            raise RuntimeError(stopped)
        result = await feeds.ingest(search, items, "wellfound")
        if stopped:   # a partial run: recorded as a warning next to what it did store
            result["source_breakdown"] = {"wellfound": {"returned": len(items), "seen": result["jobs_found"],
                                                        "new": result["new_jobs"], "error": stopped}}
        return result
    except Exception as e:
        logger.error(f"wellfound scrape failed for '{search.name}': {e}")
        return {"jobs_found": 0, "new_jobs": 0, "error": str(e), "duration": time.time() - start}


async def preview(search: Search, db) -> dict:
    start = time.time()
    try:
        items, stopped = await fetch(search)
        out = await feeds.preview_items(search, db, items, "wellfound", start)
        if stopped:
            out["warning"] = stopped
        return out
    except Exception as e:
        return {"search_name": search.name, "error": str(e), "duration": round(time.time() - start, 1),
                "config": {"mode": "wellfound", "search_term": search.search_term or ""}}
