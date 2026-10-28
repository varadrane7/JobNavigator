"""Two free, startup-heavy job feeds and the ingest they share.

- SimplifyJobs New-Grad-Positions (`simplify` mode): the community-kept new-grad list on GitHub
  (listings.json), employer apply links, a sponsorship field. Jobs that require citizenship or
  say they do not sponsor are left out.
- Hacker News "Who is hiring?" (`hn_hiring` mode): the monthly thread's top-level comments,
  mostly startups posting themselves. A comment's first line ("Company | Role | Location | ...")
  gives the company, role and place; the comment is the description and its HN link the URL.

Both are read whole on every run (one request, or two for HN) and filtered here: the search's
title include/exclude keywords, its `search_term` (comma-separated words, any of which must
appear) and its `location` (comma-separated places, any of which must appear; "United States"
keeps US and US-remote listings only).
"""
import logging
import re
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy.exc import IntegrityError

from backend.models.db import SessionLocal, Job, Search, get_existing_external_ids
from backend.scraper._shared.analysis import analyze_inline, prefetch_h1b
from backend.scraper._shared.dedup import make_content_hash, make_external_id
from backend.scraper._shared.filters import build_search_exclude_sets, search_title_filters, title_kept
from backend.scraper._shared.html_text import html_to_text
from backend.scraper._shared.posting_links import counts_as_new

logger = logging.getLogger("jobnavigator.startup_feeds")

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
SIMPLIFY_CATEGORIES = {"Software", "Software Engineering", "AI/ML/Data", "Data Science, AI & Machine Learning"}
NO_SPONSOR = {"U.S. Citizenship is Required", "Does Not Offer Sponsorship"}
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"

_US_STATE = re.compile(r",\s*(A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b")
_US_WORDS = re.compile(r"\b(USA?|United States|U\.S\.|Remote in US|NYC|SF|Bay Area)\b", re.I)


def is_us(location: str) -> bool:
    loc = location or ""
    return bool(_US_STATE.search(loc) or _US_WORDS.search(loc) or loc.strip().lower() in ("remote", ""))


def _words(value) -> list[str]:
    return [w.strip().lower() for w in str(value or "").split(",") if w.strip()]


def place_ok(location: str, wanted: str | None) -> bool:
    places = _words(wanted)
    if not places:
        return True
    loc = (location or "").lower()
    for p in places:
        if p in ("united states", "usa", "us"):
            if is_us(location):
                return True
        elif p in loc:
            return True
    return False


def terms_ok(text: str, term: str | None) -> bool:
    words = _words(term)
    low = (text or "").lower()
    return not words or any(w in low for w in words)


# ── SimplifyJobs ─────────────────────────────────────────────────────────────

def parse_simplify(raw: list, hours: int, now: float | None = None) -> list[dict]:
    now = now or time.time()
    out = []
    for x in raw:
        if not (x.get("active") and x.get("is_visible", True)):
            continue
        if x.get("category") and x["category"] not in SIMPLIFY_CATEGORIES:
            continue
        if x.get("sponsorship") in NO_SPONSOR:
            continue
        posted = x.get("date_posted") or x.get("date_updated")
        if posted and now - posted > hours * 3600:
            continue
        locs = x.get("locations") or []
        out.append({"title": x.get("title") or "", "company": x.get("company_name") or "",
                    "url": x.get("url") or "", "location": "; ".join(locs[:3]),
                    "locations": locs, "description": None,
                    "posted": datetime.fromtimestamp(posted, timezone.utc) if posted else None,
                    "sponsorship": x.get("sponsorship")})
    return out


async def fetch_simplify(search: Search) -> list[dict]:
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "JobNavigator"}) as client:
        r = await client.get(SIMPLIFY_URL)
        r.raise_for_status()
        raw = r.json()
    items = parse_simplify(raw, max(int(search.hours_old or 24), 24))
    return [j for j in items
            if any(place_ok(l, search.location) for l in (j["locations"] or [""]))
            and terms_ok(j["title"], search.search_term)]


# ── Hacker News "Who is hiring?" ──────────────────────────────────────────────

_ROLE = re.compile(r"engineer|developer|programmer|swe\b|sde\b|architect|scientist|devops|sre\b|full[- ]?stack|backend|frontend", re.I)
_PLACE = re.compile(r"remote|onsite|on-site|hybrid|\b[A-Z][a-z]+,\s*[A-Z]{2}\b|new york|nyc|san francisco|\bsf\b|london|berlin|toronto|boston|seattle|austin", re.I)


def parse_hn_comment(c: dict) -> dict | None:
    """A job from one top-level comment, or None when its first line does not read as a posting."""
    html = c.get("text") or ""
    if not html or c.get("author") in (None, "whoishiring"):
        return None
    first = html_to_text(re.split(r"<p>", html, maxsplit=1)[0]).strip()
    parts = [p.strip() for p in re.split(r"\s+\|\s+|\s+\|\s*|\s*\|\s+", first) if p.strip()]
    if len(parts) < 2:
        return None
    company = re.sub(r"https?://\S+|\s*\(.*?\)\s*$", "", parts[0]).strip(" -–:")[:80]
    role = next((p for p in parts[1:] if _ROLE.search(p)), "")
    place = next((p for p in parts[1:] if _PLACE.search(p) and p != role), "")
    if not company or not role:
        return None
    posted = c.get("created_at")
    return {"title": role[:140], "company": company, "url": f"https://news.ycombinator.com/item?id={c['id']}",
            "location": place[:120], "description": html_to_text(html)[:20000],
            "posted": datetime.fromisoformat(posted.replace("Z", "+00:00")) if posted else None}


async def fetch_hn(search: Search) -> list[dict]:
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "JobNavigator"}) as client:
        r = await client.get(HN_SEARCH, params={"tags": "story,author_whoishiring", "query": "who is hiring",
                                                "hitsPerPage": 5})
        r.raise_for_status()
        story = next((h for h in r.json().get("hits", []) if "who is hiring" in (h.get("title") or "").lower()), None)
        if not story:
            return []
        r = await client.get(HN_ITEM.format(id=story["objectID"]))
        r.raise_for_status()
        children = r.json().get("children") or []
    out = []
    for c in children:
        j = parse_hn_comment(c)
        if j and place_ok(f"{j['location']} {j['description'][:400]}", search.location) \
                and terms_ok(f"{j['title']} {j['description']}", search.search_term):
            out.append(j)
    return out


# ── shared ingest ────────────────────────────────────────────────────────────

async def ingest(search: Search, items: list[dict], source: str) -> dict:
    """Save `items` as new jobs of `search` after its title and company filters; the
    {jobs_found, new_jobs, error, duration} every source module returns."""
    start = time.time()
    db = SessionLocal()
    new_jobs = kept = 0
    try:
        include_kw, exclude_kw = search_title_filters(search, db)
        global_ex, search_ex = build_search_exclude_sets(db, search)
        existing = get_existing_external_ids(db)
        keep = [j for j in items if j["url"] and title_kept(j["title"], include_kw, exclude_kw)[0]
                and (j["company"] or "").lower() not in global_ex | search_ex]
        kept = len(keep)
        await prefetch_h1b([j["company"] for j in keep
                            if make_external_id(j["company"], j["title"], j["url"]) not in existing])
        for j in keep:
            ext_id = make_external_id(j["company"], j["title"], j["url"])
            if ext_id in existing:
                continue
            job = Job(external_id=ext_id, content_hash=make_content_hash(j["company"], j["title"]),
                      company=j["company"], title=j["title"], url=j["url"], source=source,
                      search_id=search.id, location=j.get("location") or None,
                      description=j.get("description") or None, status="new", seen=False, saved=False,
                      posted_at=j.get("posted"))
            try:
                await analyze_inline(job, db=db)
            except Exception as e:
                logger.warning(f"{source} inline analysis failed for {j['title']}: {e}")
            if job.h1b_jd_flag:
                continue
            try:
                with db.begin_nested():
                    db.add(job)
                    db.flush()
                new_jobs += counts_as_new(job)
                existing.add(ext_id)
            except IntegrityError:
                continue
        s = db.query(Search).filter(Search.id == search.id).first()
        if s:
            s.last_run_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    from backend.activity import log_activity
    log_activity("scrape", f"{source} '{search.name}': {new_jobs} new / {kept} kept")
    return {"jobs_found": kept, "new_jobs": new_jobs, "error": None, "duration": time.time() - start}


async def preview_items(search: Search, db, items: list[dict], source: str, start: float) -> dict:
    include_kw, exclude_kw = search_title_filters(search, db)
    global_ex, search_ex = build_search_exclude_sets(db, search)
    jobs = []
    for j in items:
        ok, reason = title_kept(j["title"], include_kw, exclude_kw)
        if ok and (j["company"] or "").lower() in global_ex | search_ex:
            ok, reason = False, "Company excluded"
        jobs.append({"title": j["title"], "company": j["company"], "url": j["url"], "source": source,
                     "location": j.get("location") or "", "salary": None,
                     "has_description": bool(j.get("description")), "desc_length": len(j.get("description") or ""),
                     "kept": ok, "reason": None if ok else reason,
                     "posted": j["posted"].isoformat() if j.get("posted") else None})
    return {"search_name": search.name, "duration": round(time.time() - start, 1), "raw_count": len(items),
            "after_filter": sum(1 for x in jobs if x["kept"]), "source_breakdown": {source: len(items)},
            "include_keywords": include_kw, "exclude_keywords": exclude_kw, "jobs": jobs,
            "config": {"mode": source, "search_term": search.search_term or "", "location": search.location or ""}}
