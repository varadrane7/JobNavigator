"""Web tools an LLM can call: OpenAI function schemas + their executors."""
import json
import logging

from backend.search.searxng import SearchError, search

logger = logging.getLogger("jobnavigator.search.tools")

WEB_SEARCH = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web (SearXNG). Returns titles, URLs and snippets. "
                       "Supports operators like site:linkedin.com/in and quoted phrases.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "time_range": {"type": "string", "enum": ["day", "week", "month", "year"],
                               "description": "Only results from this period (optional)"},
            },
            "required": ["query"],
        },
    },
}

FETCH_PAGE = {
    "type": "function",
    "function": {
        "name": "fetch_page",
        "description": "Fetch a public web page and return its readable text (first ~4000 chars).",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
}

TOOLS = [WEB_SEARCH, FETCH_PAGE]


async def fetch_page_text(url: str, limit: int = 4000) -> str:
    from backend.api.routes_applications import _extract_clean_content
    from backend.scraper._shared.bot_wall import is_bot_wall
    from backend.scraper._shared.url_safety import safe_get
    resp = await safe_get(url, timeout=15.0, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    if resp.status_code >= 400:
        return f"HTTP {resp.status_code}"
    if is_bot_wall(resp.text):
        return "Blocked by a bot wall."
    _html, text = _extract_clean_content(resp.text)
    return (text or "")[:limit]


async def run_tool(name: str, args: dict) -> str:
    try:
        if name == "web_search":
            results = await search(args.get("query", ""), n=8, time_range=args.get("time_range"))
            return json.dumps([{k: r[k] for k in ("title", "url", "snippet")} for r in results])
        if name == "fetch_page":
            return await fetch_page_text(args.get("url", ""))
        return f"Unknown tool {name}"
    except SearchError as e:
        return f"Search unavailable: {e}"
    except Exception as e:  # a tool failure is an answer for the model, not a crash
        logger.info(f"tool {name} failed: {e}")
        return f"Error: {e}"
