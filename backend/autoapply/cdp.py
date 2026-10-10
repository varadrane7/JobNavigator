"""Drive the user's own logged-in Chrome over the DevTools protocol (LinkedIn, ZipRecruiter).

Chrome only answers DevTools requests whose Host header is localhost or an IP, and its
/json/version hands back a ws://localhost:<port>/... URL that is useless from inside a container.
So the endpoint is resolved here: ask with Host: localhost, then point the websocket at the
address we actually reached and send the same Host header on the upgrade.

We open one new tab per item and close only that tab; the user's other tabs and the browser
itself are never touched."""
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("jobnavigator.autoapply.cdp")


def _host_header(cdp_url: str) -> str:
    p = urlparse(cdp_url)
    return f"localhost:{p.port or 9222}"


async def resolve_ws(cdp_url: str, timeout: float = 5.0) -> str:
    base = cdp_url.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(f"{base}/json/version", headers={"Host": _host_header(cdp_url)})
        r.raise_for_status()
        ws = r.json()["webSocketDebuggerUrl"]
    reached = urlparse(base)
    return urlparse(ws)._replace(netloc=reached.netloc).geturl()


class CdpBrowser:
    """async with CdpBrowser(url) as page: ... — a fresh tab in the user's default profile."""

    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self.page = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        from backend.autoapply.submitters.base import page_script
        ws = await resolve_ws(self.cdp_url)
        self.pw = await async_playwright().start()
        try:
            browser = await self.pw.chromium.connect_over_cdp(ws, headers={"Host": _host_header(self.cdp_url)})
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            self.page = await ctx.new_page()
            # Page-level, so the user's own tabs never get our script.
            await self.page.add_init_script(page_script())
            return self.page
        except BaseException:
            await self.pw.stop()
            raise

    async def __aexit__(self, *exc):
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        # Stopping the driver only drops our connection; the user's Chrome keeps running.
        await self.pw.stop()


async def status(cdp_url: str) -> dict:
    """Reachable? Which browser? Logged in to the sites we use (cookie names only, never values)?"""
    out = {"cdp_url": cdp_url, "reachable": False}
    try:
        ws = await resolve_ws(cdp_url)
    except Exception as e:
        out["error"] = f"cannot reach DevTools: {e}"
        return out
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(ws, headers={"Host": _host_header(cdp_url)}, timeout=10000)
        out["reachable"] = True
        out["version"] = browser.version
        ctx = browser.contexts[0] if browser.contexts else None
        cookies = await ctx.cookies(["https://www.linkedin.com", "https://www.ziprecruiter.com"]) if ctx else []
        names = {(c["domain"].lstrip("."), c["name"]) for c in cookies}
        out["linkedin_logged_in"] = any(d.endswith("linkedin.com") and n == "li_at" for d, n in names)
        out["ziprecruiter_cookies"] = sum(1 for d, _ in names if d.endswith("ziprecruiter.com"))
        out["tabs"] = len(ctx.pages) if ctx else 0
    except Exception as e:
        out["error"] = str(e)
    finally:
        await pw.stop()
    return out
