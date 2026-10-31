"""Arbeitnow (Germany) source (search mode `arbeitnow`); fetching and parsing live in europe_feeds.py."""
from backend.models.db import Search
from backend.scraper.sources import europe_feeds


async def run(search: Search) -> dict:
    return await europe_feeds.run_mode(search, "arbeitnow")


async def preview(search: Search, db) -> dict:
    return await europe_feeds.preview_mode(search, db, "arbeitnow")
