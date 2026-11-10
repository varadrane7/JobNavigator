"""Staffing boards that draw their listings from a JSON call: Insight Global, Kforce, Yoh, United Software.

The generic scraper finds no job links on these pages. So each page is loaded in the browser and
the response it receives is read; the adapter holds no API keys, and one board changing its
response shape costs only that board."""
import asyncio
import html
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.scraper._shared.browser import _close_page, _new_page
from backend.scraper._shared.filters import _validate_job

logger = logging.getLogger("jobnavigator.scraper.ats.staffing_boards")

WAIT_SECONDS = 20
SETTLE_SECONDS = 4          # Kforce answers twice: unfiltered first, then the search
INSIGHT_PAGES = 5
CEIPAL_PAGES = 10


def _text(value) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def _place(*parts) -> str | None:
    return ", ".join(str(p) for p in parts if p) or None


def _insight(body: dict) -> list[dict]:
    jobs = []
    for j in body.get("jobs") or []:
        where = j.get("workAddress") or {}
        remote = str(j.get("workRemote")).lower() == "true"
        jobs.append({"title": (j.get("jobTitle") or "").strip(),
                     "url": f"https://insightglobal.com/jobs/{j.get('requisitionId')}",
                     "location": _place(where.get("locality"), where.get("administrativeArea")),
                     "arrangement": "remote" if remote else None})
    return jobs


def _kforce(body: dict) -> list[dict]:
    jobs = []
    for j in body.get("value") or []:
        apply_url = j.get("ApplyUrl") or ""
        page = apply_url.split("/ApplyOnline", 1)[0].replace("http://", "https://", 1)
        jobs.append({"title": (j.get("Title") or "").strip(), "url": f"{page}/" if page else "",
                     "location": _place(j.get("City"), j.get("State")),
                     "description": _text(f"{j.get('Responsibilities') or ''} {j.get('Skills') or ''}")})
    return jobs


def _yoh(body: list) -> list[dict]:
    jobs = []
    for row in body or []:
        j = row.get("data") or {}
        jobs.append({"title": (j.get("jobName") or "").strip(), "url": j.get("jobURL") or "",
                     "location": _place(j.get("city"), j.get("state")), "description": _text(j.get("fullDescription"))})
    return jobs


def _ceipal(body: dict) -> list[dict]:
    jobs = []
    for j in body.get("results") or []:
        jobs.append({"title": (j.get("public_job_title") or "").strip(), "url": j.get("apply_job_login") or "",
                     "location": _place(j.get("state"), j.get("country")),
                     "arrangement": "remote" if str(j.get("remote_opportunities")) == "1" else None,
                     "description": _text(j.get("public_job_desc"))})
    return jobs


_BOARDS = {"insightglobal.com": ("/all/jobs?keyword", _insight), "kforce.com": ("/docs/search", _kforce),
           "jobs.yoh.com": ("shazamme.io/job-results", _yoh), "usgrpinc.com": ("CareerPortalJobPostings", _ceipal)}


def _board(url: str):
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host, _BOARDS.get(host)


def is_staffing_board(url: str) -> bool:
    return _board(url)[1] is not None


def _page_url(url: str, number: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update(page=str(number), size="10")
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _load(browser, url: str, needle: str):
    """(page, the last matching response's url, its JSON). The caller closes the page."""
    page = await _new_page(browser)
    got, tasks = [], []

    async def read(resp):
        try:
            got.append((resp.url, await resp.json()))
        except Exception as e:
            logger.info(f"Unreadable board response {resp.url[:120]}: {type(e).__name__}")

    page.on("response", lambda r: tasks.append(asyncio.ensure_future(read(r))) if needle in r.url else None)
    await page.goto(url, wait_until="domcontentloaded", timeout=35000)
    for _ in range(WAIT_SECONDS * 2):
        if got:
            break
        await page.wait_for_timeout(500)
    await page.wait_for_timeout(SETTLE_SECONDS * 1000)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return page, *(got[-1] if got else (None, None))


async def _fetch(browser, url: str, host: str, needle: str, parse) -> list[dict]:
    jobs = []
    if host == "insightglobal.com":
        for number in range(1, INSIGHT_PAGES + 1):
            page, _, body = await _load(browser, _page_url(url, number), needle)
            await _close_page(page)
            found = parse(body) if body else []
            jobs += found
            if not found or number >= int(((body or {}).get("pageMetadata") or {}).get("totalPages") or 1):
                break
        return jobs
    page, api_url, body = await _load(browser, url, needle)
    try:
        jobs = parse(body) if body is not None else []
        if host == "usgrpinc.com" and api_url:
            for number in range(2, min(CEIPAL_PAGES, int((body or {}).get("num_pages") or 1)) + 1):
                next_url = re.sub(r"page=\d+", f"page={number}", api_url)
                resp = await page.request.post(next_url, multipart={"searchkey": ""})
                if not resp.ok:
                    break
                jobs += parse(await resp.json())
    finally:
        await _close_page(page)
    return jobs


async def scrape(url: str, browser=None, debug: bool = False) -> list[dict] | tuple:
    host, board = _board(url)
    needle, parse = board
    found = await _fetch(browser, url, host, needle, parse)
    jobs, rejected, seen = [], [], set()
    for j in found:
        reason = _validate_job(j["title"], j["url"])
        if reason:
            rejected.append({"title": j["title"], "url": j["url"], "selector": "staffing_json", "reason": reason})
        elif j["url"] not in seen:
            seen.add(j["url"])
            jobs.append(j)
    logger.info(f"Staffing board {host}: {len(jobs)} jobs from {url[:80]}")
    return (jobs, rejected) if debug else jobs
