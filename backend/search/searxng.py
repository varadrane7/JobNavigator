"""SearXNG client: the curator's (and any agent's) web search.

SearXNG runs as the `searxng` compose service; `searxng_url` points at it. Results
are trimmed to {title, url, snippet, engines} and cached in-process for an hour,
since one curation run asks about the same company several times.
"""
import asyncio
import logging
import time
from collections import OrderedDict

import httpx

from backend.models.db import SessionLocal, Setting

logger = logging.getLogger("jobnavigator.search")

DEFAULT_URL = "http://searxng:8080"
_CACHE: "OrderedDict[tuple, tuple[float, list]]" = OrderedDict()
_CACHE_MAX = 512
_CACHE_TTL = 3600


class SearchError(RuntimeError):
    pass


def searxng_url() -> str:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "searxng_url").first()
        return ((row.value if row else "") or DEFAULT_URL).rstrip("/")
    finally:
        db.close()


def _trim(results: list, n: int) -> list:
    out, seen = [], set()
    for r in results:
        url = r.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({
            "title": (r.get("title") or "").strip(),
            "url": url,
            "snippet": (r.get("content") or "").strip(),
            "engines": r.get("engines") or [r.get("engine")],
            "published": r.get("publishedDate"),
        })
        if len(out) >= n:
            break
    return out


async def search(query: str, n: int = 10, time_range: str | None = None,
                 engines: str | None = None, base_url: str | None = None) -> list[dict]:
    """Run one query. `time_range` is day|week|month|year. Raises SearchError when
    SearXNG is unreachable; an empty list means it answered with nothing."""
    query = " ".join((query or "").split())
    if not query:
        return []
    key = (query, n, time_range, engines)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        _CACHE.move_to_end(key)
        return hit[1]

    url = (base_url or await asyncio.to_thread(searxng_url)) + "/search"
    params = {"q": query, "format": "json"}
    if time_range:
        params["time_range"] = time_range
    if engines:
        params["engines"] = engines
    last = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(url, params=params)
            resp.raise_for_status()
            results = _trim(resp.json().get("results") or [], n)
            _CACHE[key] = (time.monotonic(), results)
            if len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
            return results
        except (httpx.HTTPError, ValueError) as e:
            last = e
            await asyncio.sleep(1.5 * (attempt + 1))
    raise SearchError(f"SearXNG at {url} failed: {last}")


def format_results(results: list[dict], max_chars: int = 3500) -> str:
    """Numbered plain-text block for an LLM prompt."""
    lines, used = [], 0
    for i, r in enumerate(results, 1):
        line = f"[{i}] {r['title']}\n{r['url']}\n{r['snippet']}\n"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)
