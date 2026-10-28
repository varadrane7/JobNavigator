"""LinkedIn people search for the curator, through a headless logged-in session.

The mock session (Settings › LinkedIn › Mock session) does the searching; the main session is
used when the mock one is signed out, throttled or spent, and for network-filtered searches.
Each session has a daily cap and a cooldown after a 429 (curator/li_budget.py); requests are
spaced 20-60 s apart.
"""
import asyncio
import json
import logging
import os
import random
from urllib.parse import quote

from backend.models.db import SessionLocal
from backend.curator import li_budget

logger = logging.getLogger("jobnavigator.curator.linkedin")

_RESULTS_JS = """
() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('main a[href*="/in/"]')) {
    const url = (a.href || '').split('?')[0];
    if (!url || seen.has(url)) continue;
    const card = a.closest('li') || a.closest('div[data-chameleon-result-urn]') || a.parentElement;
    const text = (card?.innerText || '').trim();
    if (!text) continue;
    seen.add(url);
    out.push({url, text: text.slice(0, 600)});
  }
  return out.slice(0, 12);
}
"""

_NOISE = ("connect", "message", "follow", "view ", "status is", "mutual connection", "• 1st", "• 2nd",
          "• 3rd", "1st", "2nd", "3rd+", "3rd", "current:", "past:", "summary:", "premium")


def parse_card(text: str) -> tuple[str, str]:
    """(name, headline) from a people-search card's visible text."""
    lines = [l.strip(" •·") for l in text.splitlines() if l.strip(" •·")]
    lines = [l for l in lines if l.lower() not in _NOISE and not l.lower().startswith(("view ", "• "))]
    if not lines:
        return "", ""
    name = lines[0]
    headline = next((l for l in lines[1:] if l != name and not l.lower().startswith(_NOISE)), "")
    return name, headline


def card_details(text: str, headline: str) -> dict:
    """{location, country, current_text, past_text} from a people-search card: the location line
    under the headline, and the "Current: ..." / "Past: ..." lines LinkedIn adds when the keywords
    matched a position."""
    from backend.analyzer.location import parse as parse_location
    lines = [l.strip(" •·") for l in (text or "").splitlines() if l.strip(" •·")]
    out = {"location": None, "country": None, "current_text": None, "past_text": None}
    for l in lines:
        low = l.lower()
        if low.startswith("current:"):
            out["current_text"] = l.split(":", 1)[1].strip()
        elif low.startswith("past:"):
            out["past_text"] = l.split(":", 1)[1].strip()
    after = lines[lines.index(headline) + 1:] if headline in lines else []
    for l in after[:3]:
        if l.lower().startswith(("current:", "past:", "summary:")) or l.lower() in _NOISE:
            continue
        country = parse_location(l).get("country")
        if country:
            out["location"], out["country"] = l, country
            break
    return out


# LinkedIn geo ids for the people-search location filter
_GEO_URN = {"US": "103644278", "CA": "101174742", "GB": "101165590", "IN": "102713980", "DE": "101282230"}


_POSTS_JS = """
() => {
  const out = [], seen = new Set();
  const posts = document.querySelectorAll('div.feed-shared-update-v2, div[data-urn*="urn:li:activity"], li.reusable-search__result-container');
  for (const p of posts) {
    const a = p.querySelector('a[href*="/in/"]');
    if (!a) continue;
    const url = (a.href || '').split('?')[0];
    const urn = p.getAttribute('data-urn') || (p.querySelector('[data-urn]')?.getAttribute('data-urn')) || '';
    const key = urn || url;
    if (!url || seen.has(key)) continue;
    seen.add(key);
    const actor = (p.querySelector('.update-components-actor__meta, .update-components-actor') || a).innerText || '';
    const body = (p.querySelector('.update-components-text, .feed-shared-text, .feed-shared-inline-show-more-text') || p).innerText || '';
    out.push({url, urn, actor: actor.trim().slice(0, 300), text: body.trim().slice(0, 1200)});
  }
  return out.slice(0, 15);
}
"""


def degree_of(text: str):
    """2 or 3 from a people-search card's '• 2nd' / '• 3rd+' marker (1 for a connection), else None."""
    low = (text or "").lower()
    for marker, deg in (("1st", 1), ("2nd", 2), ("3rd", 3)):
        if f"• {marker}" in low or f"\n{marker}" in low or low.endswith(marker):
            return deg
    return None


class LinkedInPeople:
    """One browser for a whole run: `async with LinkedInPeople() as li: await li.search(...)`.

    The mock session searches; the main (personal) session is used when the mock one is unusable,
    throttled or spent for the day, and for searches that need the user's own network (`network=`).
    Both are only reused from their saved cookies: a background run never types a password, so the
    personal account never meets a sign-in challenge it did not ask for (Settings › LinkedIn
    refreshes either session)."""

    def __init__(self):
        self.pw = self.browser = self.context = self.page = None
        self.account = None          # "mock" | "main" | None
        self.tried: set = set()      # sessions that failed this run
        self.dead = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass
        self.pw = self.browser = None

    async def _load_cookies(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                cookies = json.load(f)
            await self.context.clear_cookies()
            await self.context.add_cookies(cookies)
            return True
        except Exception as e:
            logger.warning(f"could not load LinkedIn cookies {path}: {e}")
            return False

    def _usable(self, which: str) -> bool:
        if which in self.tried or not li_budget.available(which):
            return False
        if which == li_budget.MOCK:
            from backend.scraper._shared.linkedin_lookup import mock_session_problem
            db = SessionLocal()
            try:
                why = mock_session_problem(db)
            finally:
                db.close()
            if why:
                logger.info(f"LinkedIn mock session not used: {why}")
                return False
        return True

    async def _use(self, which: str) -> bool:
        """Switch the browser to one session's saved cookies; True when LinkedIn answers as signed in."""
        from backend import refresh_linkedin_session as rls
        from backend.scraper.sources import linkedin_personal as lp
        if self.account == which:
            return True
        if not self._usable(which):
            return False
        if self.page is None:
            try:
                self.pw, self.browser, self.context, self.page = await lp._get_linkedin_browser()
            except Exception as e:
                logger.warning(f"LinkedIn browser failed to start: {e}")
                self.dead = True
                return False
        try:
            if await self._load_cookies(rls.jar(which)):
                await self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
                status = await lp._voyager_me_status(self.page)
                if status == 200:
                    self.account = which
                    return True
                if status == 429:
                    li_budget.cool(which)
                else:
                    logger.warning(f"LinkedIn {which} session signed out (HTTP {status}): refresh it in Settings › LinkedIn")
        except Exception as e:
            logger.warning(f"LinkedIn {which} session unusable: {e}")
        self.tried.add(which)
        return False

    async def _ready(self, main_only: bool = False) -> bool:
        if self.dead:
            return False
        if self.account and li_budget.available(self.account) and not (main_only and self.account != li_budget.MAIN):
            return True
        order = (li_budget.MAIN,) if main_only else (li_budget.MOCK, li_budget.MAIN)
        for which in order:
            self.account = None if self.account != which else self.account
            if await self._use(which):
                return True
        return False

    async def _blocked(self) -> bool:
        from backend.scraper.sources import linkedin_personal as lp
        return ("/checkpoint" in self.page.url or "/login" in self.page.url or "/authwall" in self.page.url
                or await lp._check_rate_limit(self.page))

    async def _open(self, url: str, main_only: bool = False) -> bool:
        """Load one LinkedIn page on a usable session, moving to the other session once when this one
        is throttled; False when neither can."""
        for _ in range(2):
            if not await self._ready(main_only):
                return False
            if li_budget.used(self.account):
                await asyncio.sleep(random.uniform(20, 60))
            if not li_budget.take(self.account):
                continue
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(3, 5))
            if not await self._blocked():
                return True
            li_budget.cool(self.account, "blocked or rate-limited")
            self.tried.add(self.account)
            self.account = None
        return False

    async def search(self, keywords: str, country: str | None = None, network: list[str] | None = None) -> list[dict]:
        """[{name, headline, url, source, location, country, current, past_text, degree}] for one
        people search, filtered to `country` when LinkedIn has a geo id for it; [] when no session can.
        `network` (["S", "O"] = 2nd and 3rd+) filters by the user's own network, so it runs on the
        main session only."""
        url = f"https://www.linkedin.com/search/results/people/?keywords={quote(keywords)}&origin=GLOBAL_SEARCH_HEADER"
        geo = _GEO_URN.get(country or "")
        if geo:
            url += "&geoUrn=" + quote(f'["{geo}"]')
        if network:
            url += "&network=" + quote(json.dumps(network))
        try:
            if not await self._open(url, main_only=bool(network)):
                return []
            cards = await self.page.evaluate(_RESULTS_JS)
        except Exception as e:
            logger.warning(f"LinkedIn people search failed ({keywords!r}): {e}")
            return []
        out = []
        for c in cards:
            name, headline = parse_card(c.get("text", ""))
            if name and "linkedin member" not in name.lower():
                d = card_details(c.get("text", ""), headline)
                out.append({"name": name, "headline": headline, "url": c["url"],
                            "source": f"linkedin_{self.account}", "location": d["location"],
                            "country": d["country"] or (country if geo else None),
                            "current_text": d["current_text"], "past_text": d["past_text"],
                            "degree": degree_of(c.get("text", ""))})
        return out

    async def search_posts(self, keywords: str) -> list[dict]:
        """Recent posts (past week, newest first) matching `keywords`:
        [{author_url, actor, text, urn, source}]; [] when no session can."""
        url = ("https://www.linkedin.com/search/results/content/?keywords=" + quote(keywords)
               + "&datePosted=" + quote('"past-week"') + "&sortBy=" + quote('"date_posted"')
               + "&origin=FACETED_SEARCH")
        try:
            if not await self._open(url):
                return []
            await self.page.mouse.wheel(0, 2500)
            await asyncio.sleep(2)
            posts = await self.page.evaluate(_POSTS_JS)
        except Exception as e:
            logger.warning(f"LinkedIn post search failed ({keywords!r}): {e}")
            return []
        return [{"author_url": p["url"], "actor": p.get("actor", ""), "text": p.get("text", ""),
                 "urn": p.get("urn", ""), "source": f"linkedin_{self.account}"} for p in posts]
