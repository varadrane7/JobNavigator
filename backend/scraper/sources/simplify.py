"""SimplifyJobs new-grad list source (search mode `simplify`); fetching, parsing and the ingest live in startup_feeds.py."""
import logging
import time

from backend.models.db import Search
from backend.scraper.sources import startup_feeds as feeds

logger = logging.getLogger("jobnavigator.simplify")


async def run(search: Search) -> dict:
    start = time.time()
    try:
        items = await feeds.fetch_simplify(search)
        logger.info(f"simplify '{search.name}': {len(items)} listings match")
        return await feeds.ingest(search, items, "simplify")
    except Exception as e:
        logger.error(f"simplify scrape failed for '{search.name}': {e}")
        return {"jobs_found": 0, "new_jobs": 0, "error": str(e), "duration": time.time() - start}


async def preview(search: Search, db) -> dict:
    start = time.time()
    try:
        return await feeds.preview_items(search, db, await feeds.fetch_simplify(search), "simplify", start)
    except Exception as e:
        return {"search_name": search.name, "error": str(e), "duration": round(time.time() - start, 1),
                "config": {"mode": "simplify", "search_term": search.search_term or ""}}
