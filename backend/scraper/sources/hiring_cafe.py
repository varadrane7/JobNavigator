"""Hiring Cafe (search mode `hiring_cafe`): jobs captured by the browser extension.

hiring.cafe answers automated clients with a Cloudflare check, so nothing here fetches it. The
extension (content_hiringcafe_main.js) reads the search results the page itself loaded in the
user's browser and posts them to POST /api/jobs/hiring-cafe-import; `import_jobs` parses them
and stores them through the feed ingest (startup_feeds.ingest) under the account's Hiring
Cafe search, with its title and company filters, the body-exclusion scan, salary, location
and work-arrangement analysis, and its auto-scoring depth.

A result is read defensively (`parse_result`): Hiring Cafe's own fields are
`job_information` (title, HTML description), `v5_processed_job_data` (company, place,
workplace type, yearly pay, publish date) and `apply_url` (the employer's posting, which
becomes the job's URL so a company scrape of the same posting is recognised). The keys of
the first result each process receives are logged, so a changed shape shows in the log.
"""
import logging
import time
from datetime import datetime, timezone

from backend.models.db import Search
from backend.scraper._shared.html_text import html_to_text
from backend.scraper.sources import startup_feeds as feeds

logger = logging.getLogger("jobnavigator.hiring_cafe")

SITE = "https://hiring.cafe"
MAX_BATCH = 500
_shape_logged = False


def _first(*values):
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
        if v not in (None, "", [], {}) and not isinstance(v, str):
            return v
    return None


def _num(v) -> int | None:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if n >= 1000 else None


def _date(v) -> datetime | None:
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def parse_result(raw: dict) -> dict | None:
    """One Hiring Cafe search result as a feed job, or None when it names no title or company."""
    if not isinstance(raw, dict):
        return None
    ji = raw.get("job_information") or {}
    v5 = raw.get("v5_processed_job_data") or raw.get("processed_job_data") or {}
    co = raw.get("v5_processed_company_data") or raw.get("enriched_company_data") or {}
    title = _first(ji.get("title"), ji.get("job_title_raw"), v5.get("core_job_title"), raw.get("title"))
    company = _first(v5.get("company_name"), co.get("name"), raw.get("company_name"), raw.get("company"))
    if not title or not company:
        return None
    jid = _first(raw.get("id"), raw.get("objectID"), raw.get("requisition_id"))
    apply_url = _first(raw.get("apply_url"), ji.get("apply_url"), raw.get("url"))
    view_url = f"{SITE}/viewjob/{jid}" if jid else None
    url = apply_url if isinstance(apply_url, str) and apply_url.startswith("http") else view_url
    if not url:
        return None

    places = v5.get("workplace_cities") or v5.get("workplace_locations") or []
    location = _first(v5.get("formatted_workplace_location"), raw.get("location"),
                      ", ".join(p for p in places if isinstance(p, str)) if isinstance(places, list) else None) or ""
    workplace = _first(v5.get("workplace_type"), raw.get("workplace_type"))
    if isinstance(workplace, str) and workplace.lower() == "remote" and "remote" not in location.lower():
        location = f"{location} (Remote)" if location else "Remote"

    # yearly_* are Hiring Cafe's own annualised figures; one in another currency is not
    # stored as a number (salaries here are USD), the description keeps the posting's text.
    currency = str(_first(v5.get("listed_compensation_currency"), "USD")).upper()
    lo, hi = _num(v5.get("yearly_min_compensation")), _num(v5.get("yearly_max_compensation"))
    if currency != "USD":
        lo = hi = None

    body = ji.get("description") or raw.get("description") or ""
    text = html_to_text(body) if "<" in body else body
    facts = [f"{k}: {v}" for k, v in (
        ("Seniority", v5.get("seniority_level")), ("Commitment", ", ".join(v5.get("commitment") or [])
                                                    if isinstance(v5.get("commitment"), list) else v5.get("commitment")),
        ("Workplace", workplace), ("Visa sponsorship", v5.get("visa_sponsorship")),
        ("Experience", (f"{v5.get('min_industry_and_role_yoe')}+ years"
                        if v5.get("min_industry_and_role_yoe") not in (None, "") else None)),
    ) if v not in (None, "", [])]
    description = ("\n".join(facts) + ("\n\n" + text.strip() if text.strip() else "")).strip() or None

    return {"title": title, "company": company, "url": url,
            "apply_url": url if url == apply_url else None, "location": location,
            "description": description, "posted": _date(_first(v5.get("estimated_publish_date"),
                                                                raw.get("estimated_publish_date"),
                                                                raw.get("created_at"))),
            "salary_min": lo, "salary_max": hi or lo,
            "arrangement": workplace if isinstance(workplace, str) else None,
            "hc_id": str(jid) if jid else None}


def _log_shape(raw_list: list) -> None:
    global _shape_logged
    if _shape_logged or not raw_list or not isinstance(raw_list[0], dict):
        return
    _shape_logged = True
    r = raw_list[0]
    nested = {k: sorted(v)[:40] for k, v in r.items() if isinstance(v, dict)}
    logger.info(f"Hiring Cafe result shape: keys={sorted(r)[:60]} nested={nested}")


def parse_batch(raw_list: list) -> list[dict]:
    _log_shape(raw_list)
    out, seen = [], set()
    for raw in raw_list[:MAX_BATCH]:
        j = parse_result(raw)
        if not j:
            continue
        key = j["hc_id"] or j["url"]
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out


async def import_jobs(search: Search, raw_list: list) -> dict:
    """Parse and store one batch from the extension under `search`; the ingest result plus
    `received` and `parsed`."""
    start = time.time()
    items = parse_batch(raw_list)
    items = [j for j in items if j["salary_min"] or not getattr(search, "require_salary", False)]
    result = await feeds.ingest(search, items, "hiring_cafe")
    result.update({"received": len(raw_list), "parsed": len(items), "duration": time.time() - start})
    logger.info(f"Hiring Cafe import: {len(raw_list)} received, {len(items)} parsed, "
                f"{result['jobs_found']} kept, {result['new_jobs']} new")
    return result
