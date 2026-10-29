"""Two free, English-friendly German job feeds for a relocation lane (curate_countries without US).

- Arbeitnow (`arbeitnow` mode): the public job-board API, 300 jobs a page, newest first, about a
  day per page. Pages are read until they pass the search's `hours_old` (at most MAX_PAGES).
- Berlin Startup Jobs (`berlinstartupjobs` mode): the engineering category's RSS feed, Berlin
  startups that mostly hire in English. It posts a few times a week, so it is read a week back.

Both are filtered like the startup feeds (startup_feeds.py): title include/exclude keywords, the
search term (comma-separated words, any of which must appear in title or description) and the
location (comma-separated places, any of which must appear). Ingest and preview are shared.
"""
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from backend.models.db import Search
from backend.scraper._shared.html_text import html_to_text
from backend.scraper.sources import startup_feeds as feeds

logger = logging.getLogger("jobnavigator.europe_feeds")

ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
BSJ_URL = "https://berlinstartupjobs.com/engineering/feed/"
MAX_PAGES = 5
_UA = {"User-Agent": "JobNavigator (personal job search; links back to the source)"}
_COUNTRY = re.compile(r"\b(germany|deutschland|austria|österreich|switzerland|schweiz|netherlands|"
                      r"ireland|united kingdom|france|spain|poland|remote)\b", re.I)


def _ts(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (TypeError, ValueError):
        return None


def _german_place(location: str) -> str:
    """Arbeitnow is a German board and often names only the city ("Dresden"): say the country."""
    loc = (location or "").strip()
    return loc if not loc or _COUNTRY.search(loc) else f"{loc}, Germany"


def parse_arbeitnow(raw: list, since: float) -> tuple[list[dict], bool]:
    """(jobs posted after `since`, whether the page ran past it)."""
    out, older = [], False
    for x in raw:
        posted = _ts(x.get("created_at"))
        if posted and posted.timestamp() < since:
            older = True
            continue
        loc = _german_place(x.get("location") or "")
        if str(x.get("remote")).lower() == "true":
            loc = f"{loc} (Remote)" if loc else "Remote, Germany"
        out.append({"title": (x.get("title") or "").strip(), "company": (x.get("company_name") or "").strip(),
                    "url": x.get("url") or "", "location": loc,
                    "description": html_to_text(x.get("description") or "")[:20000] or None, "posted": posted})
    return out, older


async def fetch_arbeitnow(search: Search) -> list[dict]:
    since = time.time() - max(int(search.hours_old or 24), 24) * 3600
    items = []
    async with httpx.AsyncClient(timeout=60, headers=_UA) as client:
        for page in range(1, MAX_PAGES + 1):
            r = await client.get(ARBEITNOW_URL, params={"page": page})
            r.raise_for_status()
            jobs, older = parse_arbeitnow(r.json().get("data") or [], since)
            items.extend(jobs)
            if older or not jobs:
                break
    return _filter(items, search)


_NS = {"content": "http://purl.org/rss/1.0/modules/content/"}


def parse_bsj(xml_text: str, since: float) -> list[dict]:
    out = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        role, _, company = title.rpartition(" // ")
        if not role:
            role, company = title, ""
        try:
            posted = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            posted = None
        if posted and posted.timestamp() < since:
            continue
        body = item.findtext("content:encoded", namespaces=_NS) or item.findtext("description") or ""
        out.append({"title": role.strip(), "company": company.strip(), "url": (item.findtext("link") or "").strip(),
                    "location": "Berlin, Germany", "description": html_to_text(body)[:20000] or None,
                    "posted": posted})
    return out


async def fetch_bsj(search: Search) -> list[dict]:
    # a small board that posts a few times a week: read a week back (seen postings are skipped)
    since = time.time() - max(int(search.hours_old or 24), 7 * 24) * 3600
    async with httpx.AsyncClient(timeout=60, headers=_UA, follow_redirects=True) as client:
        r = await client.get(BSJ_URL)
        r.raise_for_status()
    return _filter(parse_bsj(r.text, since), search)


def _filter(items: list[dict], search: Search) -> list[dict]:
    return [j for j in items
            if feeds.place_ok(j["location"], search.location)
            and feeds.terms_ok(f"{j['title']} {j['description'] or ''}", search.search_term)]


_FETCH = {"arbeitnow": fetch_arbeitnow, "berlinstartupjobs": fetch_bsj}


async def run_mode(search: Search, mode: str) -> dict:
    start = time.time()
    try:
        items = await _FETCH[mode](search)
        logger.info(f"{mode} '{search.name}': {len(items)} listings match")
        return await feeds.ingest(search, items, mode)
    except Exception as e:
        logger.error(f"{mode} scrape failed for '{search.name}': {e}")
        return {"jobs_found": 0, "new_jobs": 0, "error": str(e), "duration": time.time() - start}


async def preview_mode(search: Search, db, mode: str) -> dict:
    start = time.time()
    try:
        return await feeds.preview_items(search, db, await _FETCH[mode](search), mode, start)
    except Exception as e:
        return {"search_name": search.name, "error": str(e), "duration": round(time.time() - start, 1),
                "config": {"mode": mode, "search_term": search.search_term or ""}}
