"""LinkedIn people search for the curator, through a headless logged-in session.

The mock session (Settings › LinkedIn › Mock session) does the searching; the main session is
used when the mock one is signed out, throttled or spent, and for network-filtered searches.
Each session has a daily cap and a cooldown after a 429 (curator/li_budget.py); requests are
spaced 20-60 s apart.
"""
import asyncio
import json
import re
import logging
import os
import random
from urllib.parse import parse_qs, quote, urlparse

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


# "<title> at <company> since <when>": how a card states a current position without the "Current:" tag
_SINCE_LINE = re.compile(r"(?<=\S)\s+since\b", re.I)


def card_details(text: str, headline: str) -> dict:
    """{location, country, current_text, past_text} from a people-search card: the location line
    under the headline, and the line LinkedIn adds when the keywords matched a position:
    "Current: ..." / "Past: ...", or (since late 2026) an untagged "Engineering Manager at Stripe
    since March 2023, ..." for a current one."""
    from backend.analyzer.location import parse as parse_location
    lines = [l.strip(" •·") for l in (text or "").splitlines() if l.strip(" •·")]
    out = {"location": None, "country": None, "current_text": None, "past_text": None}
    for l in lines:
        low = l.lower()
        if low.startswith("current:"):
            out["current_text"] = l.split(":", 1)[1].strip()
        elif low.startswith("past:"):
            out["past_text"] = l.split(":", 1)[1].strip()
        elif out["current_text"] is None and l != headline and _SINCE_LINE.search(l):
            out["current_text"] = _SINCE_LINE.split(l, 1)[0].strip()
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
_GEO_URN = {"US": "103644278", "CA": "101174742", "GB": "101165590", "IN": "102713980", "DE": "101282230",
            "NL": "102890719", "IE": "104738515", "AE": "104305776"}


_POSTS_JS = r"""
() => {
  // LinkedIn's content search has no stable class names: each result's text starts with
  // "Feed post", so a post is the nearest ancestor of an author link whose text does.
  const main = document.querySelector('main') || document.body;
  const seen = new Set(), out = [];
  for (const a of main.querySelectorAll('a[href*="/in/"]')) {
    let el = a;
    while (el && el !== main && !(el.innerText || '').trimStart().startsWith('Feed post')) el = el.parentElement;
    if (!el || el === main || seen.has(el)) continue;
    seen.add(el);
    const text = (el.innerText || '').trim().replace(/^Feed post\s*/, '');
    const lines = text.split('\n').map((l) => l.trim()).filter(Boolean);
    const follow = lines.findIndex((l) => /^(Follow|Connect|Following)$/.test(l));
    const actor = (follow > 0 ? lines.slice(0, follow) : lines.slice(0, 4)).join('\n');
    const body = (follow > 0 ? lines.slice(follow + 1) : lines.slice(4)).join('\n');
    out.push({url: (a.href || '').split('?')[0], urn: '', actor: actor.slice(0, 400), text: body.slice(0, 1500)});
  }
  return out.slice(0, 15);
}
"""


# ── facet searches ───────────────────────────────────────────────────────────
# Verified in a real browser on 2026-11-17 (.course/linkedin-facets.md): currentCompany, pastCompany,
# schoolFilter, network, geoUrn, a quoted title="..." and, on posts, authorCompany. `titleFreeText` is ignored
# (LinkedIn strips it), so it is never sent. LinkedIn reorders the params and drops the ones it ignores.
_FACET_PARAMS = ("currentCompany", "pastCompany", "schoolFilter", "network", "geoUrn", "title", "authorCompany")


def _ids(values) -> str:
    return quote(json.dumps([str(v) for v in values], separators=(",", ":")))


def people_url(keywords: str = "", *, current_company=None, past_company=None, schools=None, network=None,
               country: str | None = None, title: str | None = None) -> str:
    """The people-search URL for a faceted search. Company and school arguments are numeric LinkedIn ids
    (one id or a list); `title` is one title, sent quoted; `network` is ["F", "S"] and the like."""
    url = "https://www.linkedin.com/search/results/people/?origin=FACETED_SEARCH"
    if keywords:
        url += "&keywords=" + quote(keywords)
    for param, val in (("currentCompany", current_company), ("pastCompany", past_company), ("schoolFilter", schools)):
        vals = [val] if isinstance(val, (str, int)) else list(val or [])
        if vals:
            url += f"&{param}=" + _ids(vals)
    if network:
        url += "&network=" + _ids(network)
    geo = _GEO_URN.get(country or "")
    if geo:
        url += "&geoUrn=" + _ids([geo])
    if title:
        url += "&title=" + quote(f'"{title}"')
    return url


def posts_url(keywords: str = "", *, author_company=None) -> str:
    """The content-search URL: past week, newest first, optionally only posts by people at one company."""
    url = "https://www.linkedin.com/search/results/content/?"
    if keywords:
        url += "keywords=" + quote(keywords) + "&"
    if author_company:
        url += "authorCompany=" + _ids([author_company]) + "&"
    return (url + "datePosted=" + quote('"past-week"') + "&sortBy=" + quote('"date_posted"')
            + "&origin=FACETED_SEARCH")


def facets_of(url: str) -> dict:
    """{param: value as sent} of the facet params in `url`, ids as a list of strings, a title unquoted."""
    q = parse_qs(urlparse(url).query)
    out = {}
    for p in _FACET_PARAMS:
        if p in q:
            raw = q[p][0]
            try:
                v = json.loads(raw)
            except ValueError:
                v = raw
            out[p] = [str(x) for x in v] if isinstance(v, list) else str(v)
    return out


def dropped_facets(final_url: str, requested: dict) -> list:
    """The facet params of `requested` (as facets_of reads them) that the page's final, post-redirect URL no
    longer carries with the same value: LinkedIn strips a param it ignores."""
    kept = facets_of(final_url)

    def norm(v):
        return sorted(v) if isinstance(v, list) else v        # network ["F","S"] and ["S","F"] are one facet
    return [p for p, v in requested.items() if norm(kept.get(p)) != norm(v)]


_PILL_FACETS = {"currentCompany", "pastCompany", "authorCompany", "schoolFilter", "network"}   # facets with a pill
_PILL_NETWORK = re.compile(r"\b(1st|2nd|3rd)\b", re.I)


def pill_missing(pills: list, requested: dict, hints: dict) -> list:
    """The requested facets whose filter pill is not among `pills` (button texts read from the page).
    `hints` names what the pill should say: currentCompany/pastCompany/authorCompany the company's name,
    schoolFilter a school name. A title and a geoUrn are checked by their URL only. Returns []
    when no pills could be read at all (the URL check then stands alone)."""
    texts = [" ".join(str(t).split()).lower() for t in pills or [] if str(t).strip()]
    if not texts:
        return []
    from backend.curator import names

    def says(hint):
        ws = [w for w in names.words(hint) if len(w) > 1]
        return bool(ws) and any(ws[0] in t for t in texts)      # the name's first word: "Kotak Neo" ~ "Kotak Securities"
    out = []
    for p in requested:
        if p in ("currentCompany", "pastCompany", "authorCompany"):
            ok = says(hints.get(p, ""))
        elif p == "schoolFilter":
            ok = any(re.search(r"\bschools?\b", t) for t in texts) or says(hints.get(p, ""))
        elif p == "network":
            ok = any(_PILL_NETWORK.search(t) for t in texts)
        else:
            ok = True
        if not ok:
            out.append(p)
    return out


_PILLS_JS = """
() => {
  // only the search filter bar: a result card's Connect / Message buttons are not filters. null = no bar found
  const bar = document.querySelector('.search-reusables__filters-bar, .search-reusables__primary-filters, '
    + '[data-view-name="search-filters"], [role="toolbar"]');
  if (!bar) return null;
  const out = [];
  for (const el of bar.querySelectorAll('button, [role="button"], label')) {
    const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
    if (t && t.length <= 40) out.push(t);
  }
  return out.slice(0, 60);
}
"""

_LINKS_JS = """
() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href*="/company/"], a[href*="/school/"], a[href*="currentCompany="], a[href*="pastCompany="], a[href*="schoolFilter="]')) {
    out.push({href: a.href || '', text: (a.innerText || '').trim().slice(0, 80)});
  }
  return out.slice(0, 60);
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
        self.walled: set = set()     # sessions LinkedIn shows no out-of-network people to
        self.notes: list = []        # what went wrong, in words for the run summary
        self.bad_pages = 0           # searches whose page was not usable (blocked, walled, facet dropped, failed)
        self.loads = 0               # pages loaded by this object (each cost one budget slot)
        self.wanted_ids: set = set() # company keys whose LinkedIn id a loaded page may teach us (entity_ids.learn)

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

    async def _walled(self) -> bool:
        """LinkedIn answered a people search with its "limited visibility" page: this account has
        used up its searches or has too small a network, and every result is hidden. Not a block:
        the page loads, with nobody on it."""
        try:
            text = await self.page.evaluate("document.body ? document.body.innerText.slice(0, 3000) : ''")
        except Exception:
            return False
        return "limited visibility" in text or "Unlock these profiles" in text

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
            self.loads += 1
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(3, 5))
            if not await self._blocked():
                return True
            li_budget.cool(self.account, "blocked or rate-limited")
            self.tried.add(self.account)
            self.account = None
        return False

    async def search(self, keywords: str = "", country: str | None = None, network: list[str] | None = None, *,
                     current_company=None, past_company=None, schools=None, title: str | None = None,
                     qtype: str | None = None, names: dict | None = None) -> list[dict]:
        """[{name, headline, url, source, location, country, current, past_text, degree}] for one
        people search, filtered to `country` when LinkedIn has a geo id for it; [] when no session can.
        `network` (["S", "O"] = 2nd and 3rd+) filters by the user's own network, so it runs on the
        main session only.

        With a facet (`current_company`, `past_company`, `schools` as numeric ids, `title`) the search is a
        faceted one, on the main session only. `qtype` names the kind of query ("alumni", "role", ...); once
        LinkedIn drops one of its facets that kind is unusable for the rest of the day and costs nothing
        more. `names` gives the pill text to expect ({"currentCompany": "Stripe"})."""
        faceted = bool(current_company or past_company or schools or title)
        if faceted:
            if qtype and li_budget.unusable(qtype):
                return []
            url = people_url(keywords, current_company=current_company, past_company=past_company, schools=schools,
                             network=network, country=country, title=title)
        else:
            url = f"https://www.linkedin.com/search/results/people/?keywords={quote(keywords)}&origin=GLOBAL_SEARCH_HEADER"
            geo = _GEO_URN.get(country or "")
            if geo:
                url += "&geoUrn=" + quote(f'["{geo}"]')
            if network:
                url += "&network=" + quote(json.dumps(network))
        geo = _GEO_URN.get(country or "")
        try:
            # a session that hides everyone is not asked again this run
            if not await self._open(url, main_only=faceted or bool(network) or li_budget.MOCK in self.walled):
                self.bad_pages += 1
                return []
            if faceted and await self._facets_dropped(url, qtype, names or {}):
                self.bad_pages += 1
                return []
            cards = await self.page.evaluate(_RESULTS_JS)
            await self._learn_ids()
            if not cards and await self._walled():
                who = self.account
                self.walled.add(who)
                note = (f"LinkedIn {who} session shows no people outside its network (its search limit, or too "
                        "few connections): people search on it finds nobody")
                if note not in self.notes:
                    self.notes.append(note)
                    logger.warning(note)
                if who != li_budget.MAIN and await self._open(url, main_only=True):
                    cards = await self.page.evaluate(_RESULTS_JS)
                    if not cards and await self._walled():
                        self.walled.add(li_budget.MAIN)
                if not cards:
                    self.bad_pages += 1          # a "limited visibility" page is not an answer
        except Exception as e:
            logger.warning(f"LinkedIn people search failed ({keywords!r}): {e}")
            self.bad_pages += 1
            return []
        out = []
        for c in cards:
            name, headline = parse_card(c.get("text", ""))
            # "Jane Doe is a mutual connection" is a caption under a result, not a result
            if name and "linkedin member" not in name.lower() and "mutual connection" not in name.lower():
                d = card_details(c.get("text", ""), headline)
                out.append({"name": name, "headline": headline, "url": c["url"],
                            "source": f"linkedin_{self.account}", "location": d["location"],
                            "country": d["country"] or (country if geo else None),
                            "current_text": d["current_text"], "past_text": d["past_text"],
                            "degree": degree_of(c.get("text", ""))})
        return out

    async def _read_pills(self) -> list:
        try:
            return await self.page.evaluate(_PILLS_JS) or []
        except Exception:
            return []

    async def _facets_dropped(self, sent_url: str, qtype: str | None, names: dict) -> bool:
        """After a faceted page has loaded (and `_open` waited 3-5 s): True when LinkedIn dropped a facet we
        sent. The evidence is the final, post-redirect URL: LinkedIn strips a param it ignores. The filter
        pills only confirm: the pills are read from the filter bar alone (never from result cards), and when the
        bar cannot be found, or the page looks stale, they are read once more after a short wait, without a
        new load. A facet whose URL param is missing is dropped unless a pill for it is plainly there. Cards
        cannot tell: a faceted card carries no current / past / school line and only 40-50% of headlines name
        the company even when the filter applied. A dropped facet is logged, its query type is unusable for
        the rest of the day, and the page's results are not used."""
        try:
            requested = facets_of(sent_url)
            lost = dropped_facets(self.page.url, requested)
            pills = []
            if lost:
                await asyncio.sleep(random.uniform(2, 3))          # the page may still be settling: look once more
                lost = dropped_facets(self.page.url, requested)
            if lost:
                pills = await self._read_pills()
                if not pills:
                    await asyncio.sleep(2)
                    pills = await self._read_pills()
                if pills:        # readable filter bar: a facet with its pill showing was applied after all
                    checkable = {k: requested[k] for k in lost if k in _PILL_FACETS}
                    shown = set(checkable) - set(pill_missing(pills, checkable, names))
                    lost = [p for p in lost if p not in shown]
        except Exception as e:
            logger.warning(f"could not check the facets of a LinkedIn search ({e}); using its results")
            return False
        if not lost:
            return False
        note = (f"LinkedIn ignored the {', '.join(lost)} facet of a {qtype or 'faceted'} search: "
                f"that kind of query is not used again today")
        if note not in self.notes:
            self.notes.append(note)
        logger.warning(note)
        if qtype:
            li_budget.mark_unusable(qtype)
        return True

    async def _learn_ids(self):
        """Store the company ids that links on the loaded page carry, for the companies we want one for.
        Reads the page that is already open; no extra load."""
        if not self.wanted_ids:
            return
        try:
            from backend.curator import entity_ids
            items = await self.page.evaluate(_LINKS_JS)
            entity_ids.learn(items, self.wanted_ids)
        except Exception as e:
            logger.debug(f"LinkedIn id harvest skipped: {e}")

    async def search_posts(self, keywords: str = "", *, author_company=None, qtype: str | None = None,
                           name: str | None = None) -> list[dict]:
        """Recent posts (past week, newest first) matching `keywords`:
        [{author_url, actor, text, urn, source}]; [] when no session can. `author_company` (a numeric id)
        keeps only posts by people at that company; it is checked like a people facet (see search())."""
        if author_company and qtype and li_budget.unusable(qtype):
            return []
        url = posts_url(keywords, author_company=author_company)
        try:
            if not await self._open(url, main_only=bool(author_company)):
                return []
            if author_company and await self._facets_dropped(url, qtype, {"authorCompany": name or ""}):
                return []
            await self.page.mouse.wheel(0, 2500)
            await asyncio.sleep(2)
            posts = await self.page.evaluate(_POSTS_JS)
        except Exception as e:
            logger.warning(f"LinkedIn post search failed ({keywords!r}): {e}")
            return []
        return [{"author_url": p["url"], "actor": p.get("actor", ""), "text": p.get("text", ""),
                 "urn": p.get("urn", ""), "source": f"linkedin_{self.account}"} for p in posts]
