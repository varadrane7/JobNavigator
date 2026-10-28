"""Hacker News Who is hiring source (search mode `hn_hiring`); fetching, parsing and the ingest live in startup_feeds.py."""
import logging
import time

from backend.models.db import Search
from backend.scraper.sources import startup_feeds as feeds

logger = logging.getLogger("jobnavigator.hn_hiring")


async def run(search: Search) -> dict:
    start = time.time()
    try:
        items = await feeds.fetch_hn(search)
        logger.info(f"hn_hiring '{search.name}': {len(items)} listings match")
        return await feeds.ingest(search, items, "hn_hiring")
    except Exception as e:
        logger.error(f"hn_hiring scrape failed for '{search.name}': {e}")
        return {"jobs_found": 0, "new_jobs": 0, "error": str(e), "duration": time.time() - start}


async def preview(search: Search, db) -> dict:
    start = time.time()
    try:
        return await feeds.preview_items(search, db, await feeds.fetch_hn(search), "hn_hiring", start)
    except Exception as e:
        return {"search_name": search.name, "error": str(e), "duration": round(time.time() - start, 1),
                "config": {"mode": "hn_hiring", "search_term": search.search_term or ""}}
