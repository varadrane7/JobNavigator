"""The container's one Chromium, reached over its DevTools port: health, tab reset, screenshots."""
import asyncio
import base64
import json
import subprocess

import httpx
import websockets

CDP = "http://127.0.0.1:9222"


async def version() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{CDP}/json/version")
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def ensure_running() -> bool:
    """Relaunch Chromium if it died (a crashed tab can take the browser with it)."""
    if await version():
        return True
    subprocess.Popen(["/opt/applier/app/start-chrome.sh"])
    for _ in range(20):
        await asyncio.sleep(0.5)
        if await version():
            return True
    return False


async def _targets() -> list:
    async with httpx.AsyncClient(timeout=5) as c:
        return [t for t in (await c.get(f"{CDP}/json/list")).json() if t.get("type") == "page"]


async def reset_tabs() -> None:
    """Start every run from one blank tab, so the agent never picks up the last job's form."""
    async with httpx.AsyncClient(timeout=5) as c:
        await c.put(f"{CDP}/json/new?about:blank")
        for t in await _targets():
            if t.get("url") != "about:blank":
                await c.get(f"{CDP}/json/close/{t['id']}")


async def screenshot() -> tuple[str | None, str]:
    """(base64 PNG of the tab the agent last used, its URL); (None, "") if there is no tab."""
    try:
        pages = await _targets()
    except Exception:
        return None, ""
    # /json/list orders most recently focused first; skip leftover blank tabs.
    pages = [p for p in pages if p.get("url") != "about:blank"] or pages
    if not pages:
        return None, ""
    page = pages[0]
    try:
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 2**20) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot",
                                      "params": {"format": "png", "captureBeyondViewport": True}}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if msg.get("id") == 1:
                    data = (msg.get("result") or {}).get("data")
                    return data, page.get("url", "")
    except Exception:
        return None, page.get("url", "")


def b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()
