"""LinkedIn people search for the curator, through a headless logged-in session.

The mock account's session (the one Settings → LinkedIn refreshes, saved at
linkedin_extension._SESSION_PATH) does the searching; the personal account is used
only when the mock session is dead or rate-limited. Searches are capped per day
(`linkedin_people_daily_cap`) and spaced 20-60 s apart.
"""
import asyncio
import json
import logging
import os
import random
from urllib.parse import quote

from backend.analyzer.llm_client import _get_setting
from backend.models.db import SessionLocal
from backend.tz import local_today

logger = logging.getLogger("jobnavigator.curator.linkedin")

_USED: dict = {}          # {date: searches made}, process-local

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


def _cap() -> int:
    db = SessionLocal()
    try:
        try:
            return int(_get_setting(db, "linkedin_people_daily_cap", "25"))
        except ValueError:
            return 25
    finally:
        db.close()


def _creds(kind: str) -> tuple[str, str]:
    db = SessionLocal()
    try:
        if kind == "mock":
            return _get_setting(db, "linkedin_mock_email", ""), _get_setting(db, "linkedin_mock_password", "")
        return _get_setting(db, "linkedin_email", ""), _get_setting(db, "linkedin_password", "")
    finally:
        db.close()


class LinkedInPeople:
    """One browser for a whole curation run: `async with LinkedInPeople() as li: await li.search(...)`."""

    def __init__(self):
        self.pw = self.browser = self.context = self.page = None
        self.account = None          # "mock" | "personal" | None (unusable)
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

    async def _start(self) -> bool:
        from backend.scraper.sources import linkedin_personal as lp
        from backend.scraper.sources.linkedin_extension import _SESSION_PATH
        if self.page or self.dead:
            return not self.dead
        try:
            self.pw, self.browser, self.context, self.page = await lp._get_linkedin_browser()
        except Exception as e:
            logger.warning(f"LinkedIn browser failed to start: {e}")
            self.dead = True
            return False
        # mock session first
        if await self._load_cookies(_SESSION_PATH):
            await self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            if await lp._voyager_me_status(self.page) == 200:
                self.account = "mock"
                return True
        return await self._switch_to_personal()

    async def _switch_to_personal(self) -> bool:
        from backend.scraper.sources import linkedin_personal as lp
        if self.account == "personal":
            self.dead = True
            return False
        try:
            await self.context.clear_cookies()
            await self._load_cookies(lp.COOKIE_PATH)
            email, password = _creds("personal")
            await lp._ensure_logged_in(self.page, self.context, email, password)
            self.account = "personal"
            return True
        except Exception as e:
            logger.warning(f"LinkedIn personal session unavailable: {e}")
            self.dead = True
            return False

    async def search(self, keywords: str, country: str | None = None) -> list[dict]:
        """[{name, headline, url, source, location, country, current, past_text}] for one people
        search, filtered to `country` when LinkedIn has a geo id for it; [] when capped or unusable.
        `current` is True/False when the card's Current:/Past: line settles it, else None."""
        from backend.scraper.sources import linkedin_personal as lp
        today = local_today()
        if _USED.get(today, 0) >= _cap():
            return []
        if not await self._start():
            return []
        if _USED.get(today):
            await asyncio.sleep(random.uniform(20, 60))
        _USED[today] = _USED.get(today, 0) + 1
        url = f"https://www.linkedin.com/search/results/people/?keywords={quote(keywords)}&origin=GLOBAL_SEARCH_HEADER"
        geo = _GEO_URN.get(country or "")
        if geo:
            url += "&geoUrn=" + quote(f'["{geo}"]')
        for attempt in range(2):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(random.uniform(3, 5))
                if "/checkpoint" in self.page.url or "/login" in self.page.url or await lp._check_rate_limit(self.page):
                    logger.warning(f"LinkedIn {self.account} account blocked or rate-limited")
                    if attempt == 0 and await self._switch_to_personal():
                        continue
                    return []
                cards = await self.page.evaluate(_RESULTS_JS)
                out = []
                for c in cards:
                    name, headline = parse_card(c.get("text", ""))
                    if name and "linkedin member" not in name.lower():
                        d = card_details(c.get("text", ""), headline)
                        out.append({"name": name, "headline": headline, "url": c["url"],
                                    "source": f"linkedin_{self.account}", "location": d["location"],
                                    "country": d["country"] or (country if geo else None),
                                    "current_text": d["current_text"], "past_text": d["past_text"]})
                return out
            except Exception as e:
                logger.warning(f"LinkedIn people search failed ({keywords!r}): {e}")
                return []
        return []
