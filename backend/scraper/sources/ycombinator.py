"""Y Combinator's job board (search mode `ycombinator`): ycombinator.com/jobs, no account.

Each listing page embeds its jobs as JSON (the Inertia `data-page` attribute): title, company,
location, salary, visa, minimum experience and the YC batch. A page holds about 40-50 of the
most active jobs and has no further pages, so a run reads several: the role page, its
sub-roles and its city pages, and keeps the union (about 340 engineering jobs).

What a search reads:
- `direct_url`: one or more ycombinator.com/jobs URLs (comma or newline separated), read as
  they are. Otherwise:
- `sources`: the YC roles (default software-engineer) and `location`: comma-separated places,
  each read from its city page when YC has one ("remote" included); with no location, the role
  page, its sub-roles and every city page.
- `search_term`: comma-separated words, any of which must appear in the title.
- `title_include/exclude_keywords`, company exclude and the location filter as in the other
  feeds; `require_salary` drops jobs without a pay range.

New jobs get their description from the job's own page (the posting text, with YC's visa,
experience and batch lines on top), at most DETAIL_CAP per run (the rest wait for the next
run); already stored ones are skipped before any page is read.
"""
import asyncio
import html as html_lib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from backend.models.db import Search
from backend.scraper.sources import startup_feeds as feeds

logger = logging.getLogger("jobnavigator.ycombinator")

SITE = "https://www.ycombinator.com"
DEFAULT_ROLES = ["software-engineer"]
# YC's own slugs. A role page links its sub-roles; a sub-role has no city pages.
SUBROLES = {"software-engineer": ["backend", "frontend", "full-stack", "devops", "machine-learning",
                                  "android", "ios", "embedded-systems", "data-science", "data-engineer"]}
CITIES = {"san francisco": "san-francisco", "sf": "san-francisco", "bay area": "san-francisco",
          "new york": "new-york", "nyc": "new-york", "los angeles": "los-angeles", "la": "los-angeles",
          "seattle": "seattle", "boston": "boston", "austin": "austin", "chicago": "chicago",
          "remote": "remote", "india": "india"}
# The `role` code YC gives each posting, per role page: a city page lists every role.
ROLE_CODES = {"software-engineer": "eng", "designer": "design", "product-manager": "product",
              "recruiting-hr": "recruiting", "sales-manager": "sales", "marketing": "marketing",
              "science": "science", "operations": "operations", "support": "support"}
US_CITY_SLUGS = ["san-francisco", "new-york", "remote", "seattle", "boston", "austin", "los-angeles", "chicago"]
PAGE_PAUSE = 1.0
DETAIL_PAUSE = 1.0
DETAIL_CAP = 60
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/141.0 Safari/537.36",
       "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}
_DATA_PAGE = re.compile(r'data-page="([^"]+)"')


def page_props(text: str) -> dict:
    """The props of the Inertia page a ycombinator.com response embeds, or {}."""
    m = _DATA_PAGE.search(text or "")
    if not m:
        return {}
    try:
        return json.loads(html_lib.unescape(m.group(1))).get("props") or {}
    except (ValueError, AttributeError):
        return {}


def listing_urls(search: Search) -> list[str]:
    """The listing pages one run reads, in order."""
    direct = [u.strip() for u in re.split(r"[,\n]", search.direct_url or "") if u.strip()]
    if direct:
        return list(dict.fromkeys(direct))
    roles = [r for r in (search.sources or []) if isinstance(r, str) and r] or DEFAULT_ROLES
    places = [p.strip().lower() for p in (search.location or "").split(",") if p.strip()]
    slugs = []
    for p in places:
        if p in ("united states", "usa", "us"):
            slugs += US_CITY_SLUGS
        elif p in CITIES:
            slugs.append(CITIES[p])
    slugs = list(dict.fromkeys(slugs))
    urls = []
    for role in roles:
        base = f"{SITE}/jobs/role/{role}"
        if places:
            # a place with no city page (Denver) is read from the role page and filtered there
            urls += [f"{base}/{s}" for s in slugs] or [base]
            if len(slugs) < len(places):
                urls.append(base)
        else:
            urls += [base] + [f"{base}/{sub}" for sub in SUBROLES.get(role, [])] \
                + [f"{base}/{s}" for s in US_CITY_SLUGS]
    return list(dict.fromkeys(urls))


def _ago(text: str | None, now: datetime) -> datetime | None:
    """YC's "3 months" / "4 days" / "about 1 hour" as a date that long before `now`."""
    m = re.search(r"(\d+|an?|about an?)\s*(minute|hour|day|week|month|year)", (text or "").lower())
    if not m:
        return None
    n = 1 if not m.group(1)[0].isdigit() else int(m.group(1))
    days = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]
    return now - timedelta(days=n * days)


def parse_listing(p: dict, now: datetime | None = None) -> dict:
    """One job of a listing page in the shape the feed ingest takes."""
    from backend.scraper.sources.jobright import _parse_salary
    now = now or datetime.now(timezone.utc)
    salary_min, salary_max = _parse_salary(p.get("salaryRange") or "")
    loc = (p.get("location") or "").strip()
    remote = "remote" in loc.lower()
    return {"title": (p.get("title") or "").strip(), "company": (p.get("companyName") or "").strip(),
            "url": SITE + p["url"] if (p.get("url") or "").startswith("/") else (p.get("url") or ""),
            "location": loc, "description": None, "posted": _ago(p.get("createdAt"), now),
            "salary_min": salary_min, "salary_max": salary_max, "salary_text": p.get("salaryRange") or "",
            "arrangement": True if remote else None, "yc_id": str(p.get("id") or ""),
            "visa": p.get("visa") or "", "experience": p.get("minExperience") or "",
            "batch": p.get("companyBatchName") or "", "one_liner": p.get("companyOneLiner") or "",
            "job_type": p.get("type") or ""}


def detail_text(props: dict, job: dict) -> str | None:
    """The description stored for one job: YC's facts first, then the posting itself."""
    from backend.scraper._shared.html_text import html_to_text
    d = (props.get("job") or {}).get("description") or ""
    body = html_to_text(d) if "<" in d else d
    facts = [f"{k}: {v}" for k, v in (("Company", f"{job['company']} (YC {job['batch']})" if job["batch"] else ""),
                                      ("About", job["one_liner"]), ("Salary", job["salary_text"]),
                                      ("Visa", job["visa"]), ("Experience", job["experience"]),
                                      ("Type", job["job_type"])) if v]
    text = "\n".join(facts) + ("\n\n" + body.strip() if body.strip() else "")
    return text.strip() or None


async def _get(client, url: str) -> dict:
    r = await client.get(url)
    if r.status_code == 404:
        logger.info(f"YC page not found: {url}")
        return {}
    r.raise_for_status()
    return page_props(r.text)


async def fetch(search: Search, existing_ids: set | None = None, detail_cap: int = DETAIL_CAP) -> list[dict]:
    """Every matching job of the run's listing pages; new ones (not in `existing_ids`) with
    their description."""
    from backend.scraper._shared.dedup import make_external_id
    now = datetime.now(timezone.utc)
    seen, items = set(), []
    direct = bool((search.direct_url or "").strip())
    roles = [r for r in (search.sources or []) if isinstance(r, str) and r] or DEFAULT_ROLES
    codes = {ROLE_CODES.get(r, r) for r in roles}
    async with httpx.AsyncClient(timeout=30, headers=_UA, follow_redirects=True) as client:
        for i, url in enumerate(listing_urls(search)):
            if i:
                await asyncio.sleep(PAGE_PAUSE)
            props = await _get(client, url)
            for p in props.get("jobPostings") or []:
                if not direct and p.get("role") and p["role"] not in codes:
                    continue
                j = parse_listing(p, now)
                if not j["url"] or j["yc_id"] in seen:
                    continue
                seen.add(j["yc_id"])
                items.append(j)
        items = [j for j in items
                 if feeds.place_ok(j["location"], search.location)
                 and feeds.terms_ok(j["title"], search.search_term)
                 and (j["salary_min"] or not getattr(search, "require_salary", False))]
        todo = [j for j in items
                if existing_ids is None or make_external_id(j["company"], j["title"], j["url"]) not in existing_ids]
        for i, j in enumerate(todo[:detail_cap]):
            if i:
                await asyncio.sleep(DETAIL_PAUSE)
            try:
                j["description"] = detail_text(await _get(client, j["url"]), j)
            except Exception as e:  # noqa: BLE001 - the job keeps the listing facts only
                logger.info(f"YC job page {j['url']}: {e}")
            if not j["description"]:
                j["description"] = detail_text({}, j)
        # past the cap: left for the next run, so none is stored without its page
        for j in todo[detail_cap:]:
            j["deferred"] = True
    return items


async def run(search: Search) -> dict:
    start = time.time()
    try:
        from backend.models.db import SessionLocal, get_existing_external_ids
        db = SessionLocal()
        try:
            existing = get_existing_external_ids(db)
        finally:
            db.close()
        items = await fetch(search, existing)
        logger.info(f"ycombinator '{search.name}': {len(items)} listings match")
        items = [j for j in items if not j.get("deferred")]
        return await feeds.ingest(search, items, "ycombinator")
    except Exception as e:
        logger.error(f"ycombinator scrape failed for '{search.name}': {e}")
        return {"jobs_found": 0, "new_jobs": 0, "error": str(e), "duration": time.time() - start}


async def preview(search: Search, db) -> dict:
    start = time.time()
    try:
        from backend.models.db import get_existing_external_ids
        # Test reads a few job pages only: enough to show the descriptions come through
        items = await fetch(search, get_existing_external_ids(db), detail_cap=5)
        return await feeds.preview_items(search, db, items, "ycombinator", start)
    except Exception as e:
        return {"search_name": search.name, "error": str(e), "duration": round(time.time() - start, 1),
                "config": {"mode": "ycombinator", "search_term": search.search_term or ""}}
