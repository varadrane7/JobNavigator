"""Company-first people search for the US job lane: pick the day's companies, search each one with LinkedIn's
facets, remember who was found.

One company at a time, never one job at a time (the curator's top jobs are de-duplicated by company key):
  1. Alumni      currentCompany + schoolFilter (the owner's schools, from the persona) + network F,S + US.  1 load.
  3. Posts       (runs after the role step) top-3 companies only, and only while `want` people are not in hand: LinkedIn's post search filtered to
                 authorCompany (past week, "hiring"). The author of a hiring post is a lead: audience "post" (or
                 "hiring_manager" when the headline says so), the post text kept as evidence so the draft can cite it.
                 1 load, main session. Like every faceted query, a dropped authorCompany makes the type "posts_company"
                 unusable for the rest of the day and the company stays unfinished.
  2. Role        currentCompany + title "recruiter" (a quoted single title, the form that was verified);
                 for a top-3 company, a second query with title "software engineer" when the first found people
                 but not enough.  1-2 loads.
  4. Ex-colleague pastCompany = the owner's past employers, only for a top-3 company and only when steps 1 and 2
                 found nobody.  1 load.
Each step stops the company as soon as `want` people are in hand. All of it runs on the main session (a facet is
about the owner's own network) and inside the run's share of the daily LinkedIn budget (curator/li_budget.py):
a company is started only while at least 2 loads are left (3 for a top-3 company, which also plans the posts load), and no step is attempted without one.

Why `title` and not keywords for the role step: a quoted title is the only title form tested in a real browser
(`titleFreeText` is ignored, and OR-lists in `keywords` next to a facet were not tried), it returns only people who
hold that title, and one query per title costs the same one load a keyword query would. Cards then need no
re-ranking. Engineers get the second query only where a second load is affordable (top 3).

A company whose LinkedIn id is not in `li_entity_ids` is searched by keyword exactly as before (people.find_people);
nothing here loads a page just to learn an id. The id is learned from links on the pages that keyword search loads.

Cache (`company_people`, 30 days): who was found, and a marker that the company was searched. A company searched in
the last 30 days is not searched again; people it found who are not contacts yet are offered to its other jobs and
to the day's second run for free. A company where the owner already has a connected-or-later contact (or a 1st
degree one) is not searched: that contact is surfaced.
"""
import logging
from datetime import date, timedelta

from sqlalchemy import or_

from backend.curator import entity_ids, li_budget, names
from backend.curator.linkedin_people import LinkedInPeople
from backend.models.db import CompanyPeople, Contact, SessionLocal
from backend.tz import local_today

logger = logging.getLogger("jobnavigator.curator.company_first")

CACHE_DAYS = 30
SEARCHED = "_searched"
DEFAULT_COMPANY_CAP = 5
CONNECTED_PLUS = ("connected", "messaged", "replied", "referral")
TOP_N = 3                       # companies that get the extra queries
MIN_LOADS_PER_COMPANY = 2       # a company is started only with the alumni and role loads in hand
MIN_LOADS_TOP3 = 3              # a top-3 company also plans the hiring-posts load
MAX_PER_STEP = 3                # people kept from one query (the best `want` of all are used)
QTYPES = ("alumni", "posts_company", "role", "ex_colleague")     # kinds of faceted query (li_budget.unusable)
_ROLE_TITLES = ("recruiter", "software engineer")
POST_EVIDENCE_MAX = 500         # characters of a hiring post kept as evidence
_TITLE_KIND = {"recruiter": "recruiter", "software engineer": "engineer"}


def key_of(company: str) -> str:
    return names.company_key(company)


# ── settings ─────────────────────────────────────────────────────────────────

def company_cap() -> int:
    """Companies a day may be searched (setting people_company_cap, default 5, at most 10)."""
    from backend.accounts.settings import get_setting
    db = SessionLocal()
    try:
        return max(0, min(10, int(get_setting(db, "people_company_cap") or DEFAULT_COMPANY_CAP)))
    except (TypeError, ValueError):
        return DEFAULT_COMPANY_CAP
    finally:
        db.close()


# ── the cache ────────────────────────────────────────────────────────────────

def _cutoff(today: date) -> date:
    return today - timedelta(days=CACHE_DAYS)


def searched_since(keys, today: date) -> dict:
    """{company_key: date last searched} for the keys searched within the cache window."""
    keys = [k for k in keys if k]
    if not keys:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(CompanyPeople).filter(CompanyPeople.company_key.in_(keys), CompanyPeople.kind == SEARCHED,
                                              CompanyPeople.found_on >= _cutoff(today)).all()
        return {r.company_key: r.found_on for r in rows}
    finally:
        db.close()


def searched_count(today: date) -> int:
    """Companies searched today (all runs of the day share the daily cap)."""
    db = SessionLocal()
    try:
        return db.query(CompanyPeople).filter(CompanyPeople.kind == SEARCHED, CompanyPeople.found_on == today).count()
    finally:
        db.close()


def cached_people(key: str, today: date, known_urls=frozenset()) -> list:
    """People found for the company in the last 30 days who are not contacts yet, as candidate dicts."""
    db = SessionLocal()
    try:
        rows = (db.query(CompanyPeople).filter(CompanyPeople.company_key == key, or_(CompanyPeople.kind.is_(None), CompanyPeople.kind != SEARCHED),
                                               CompanyPeople.found_on >= _cutoff(today))
                .order_by(CompanyPeople.found_on.desc()).all())
        out = []
        for r in rows:
            if r.li_url in known_urls:
                continue
            sig = dict(r.signals or {})
            if sig.get("verified") is False or sig.get("degree") == 1:
                continue            # company guessed from a headline, or already a connection: not offered
            out.append({"name": sig.get("name") or "", "headline": sig.get("headline") or "", "url": r.li_url,
                        "location": sig.get("location"), "country": sig.get("country"), "degree": sig.get("degree"),
                        "source": sig.get("source"), "kind": r.kind, "audience": sig.get("audience"),
                        "current": True, "from_cache": True,
                        "evidence": sig.get("evidence") or "", "from_post": bool(sig.get("from_post"))})
        return out
    finally:
        db.close()


def remember(key: str, people: list, today: date, searched: bool = True) -> None:
    """Store `people` for the company (new URLs only; a person is never rewritten) and, when `searched`, the
    marker that it was searched today. Rows past the cache window are removed."""
    db = SessionLocal()
    try:
        db.query(CompanyPeople).filter(CompanyPeople.company_key == key, CompanyPeople.found_on < _cutoff(today)).delete()
        have = {u for (u,) in db.query(CompanyPeople.li_url).filter(CompanyPeople.company_key == key).all()}
        for p in people:
            url = (p.get("url") or "").split("?")[0]
            if not url or url in have:
                continue
            have.add(url)
            db.add(CompanyPeople(company_key=key, li_url=url, kind=p.get("kind"), found_on=today, signals={
                "name": p.get("name"), "headline": p.get("headline"), "location": p.get("location"),
                "country": p.get("country"), "degree": p.get("degree"), "source": p.get("source"),
                "audience": p.get("audience"), "query": p.get("query"),
                "evidence": (p.get("evidence") or "")[:POST_EVIDENCE_MAX] if p.get("from_post") else None,
                "from_post": bool(p.get("from_post")),
                "verified": bool(p.get("verified", True))}))
        if searched:
            marker = db.query(CompanyPeople).filter(CompanyPeople.company_key == key, CompanyPeople.kind == SEARCHED).first()
            if marker:
                marker.found_on = today
            else:
                db.add(CompanyPeople(company_key=key, li_url="", kind=SEARCHED, found_on=today, signals={}))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"company_people not saved for {key!r}: {e}")
    finally:
        db.close()


def remember_scouted(cands: list, today: date | None = None) -> int:
    """People the lead scout's LinkedIn searches found, filed under their company so the curator can reuse
    them (no search marker: the company itself was not searched). Only people whose company came from a card's
    current-position line are reused later; one parsed from a headline is stored but never offered. Returns how many had a company."""
    from backend.curator.people import classify
    today = today or local_today()
    by_co: dict = {}
    for c in cands:
        k = key_of(c.get("company") or "")
        if k and c.get("url") and c.get("lead_kind") != "hiring_post":
            by_co.setdefault(k, []).append({**c, "kind": classify(c.get("headline") or ""), "query": "scout",
                                   "verified": bool(c.get("company_verified"))})
    for k, people in by_co.items():
        remember(k, people, today, searched=False)
    return sum(len(v) for v in by_co.values())


# ── choosing the day's companies ─────────────────────────────────────────────

def known_urls() -> set:
    db = SessionLocal()
    try:
        return {u for (u,) in db.query(Contact.linkedin_url).filter(Contact.linkedin_url.isnot(None)).all()}
    finally:
        db.close()


def _open_lead(c, sched) -> bool:
    """A contact still worth surfacing: not skipped or closed for no reply, follow-ups not used up."""
    from backend.curator import followups
    if c.status in ("skip", "snoozed") or c.message_stage == followups.CLOSED:
        return False
    sent = c.follow_ups_sent or 0
    return not (sent > 0 and sent >= sched.count)           # every follow-up of the schedule already sent


def connected_contacts(keys: set) -> dict:
    """{company_key: Contact id} of an open contact at one of `keys` who is connected or further along, or a
    1st-degree connection: he can write to them directly, so the company is not searched. Closed leads (skipped,
    closed for no reply, follow-ups used up) do not count, so their company can be searched."""
    from backend.curator import followups
    db = SessionLocal()
    try:
        sched = followups.schedule(db)
        out = {}
        for c in db.query(Contact).filter(Contact.company.isnot(None)).order_by(Contact.found_on.desc()).all():
            k = key_of(c.company)
            if k in keys and k not in out and (c.status in CONNECTED_PLUS or c.degree == 1) and _open_lead(c, sched):
                out[k] = c.id
        return out
    finally:
        db.close()


SURFACE_MAX = 3         # contacts put back on the owner's list per run


def surface_contacts(ids) -> int:
    """Put connected contacts back in front of the owner (due today when nothing is scheduled), at most
    SURFACE_MAX per run. Called when a run has finished searching, never while planning: `plan` writes nothing."""
    today = local_today()
    n = 0
    db = SessionLocal()
    try:
        for cid in list(ids)[:SURFACE_MAX]:
            c = db.get(Contact, cid)
            if c is not None and c.next_action_at is None:
                c.next_action_at = today
                n += 1
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"could not surface connected contacts: {e}")
    finally:
        db.close()
    return n


def plan(top: list, today: date, day_cap: int | None = None) -> dict:
    """{company_key: {"action", "rank", "contact_id"}} for the US jobs of `top` (best score first), one entry per
    company: action is "connected" (surface the contact), "search" (a search slot is free), "cache" (searched in
    the last 30 days: only cached people are used) or "skip" (no slot left today).

    The slots are the day's cap (setting people_company_cap, about 5) less the companies already searched today,
    given to companies in score order. `rank` counts the distinct companies in score order from 1."""
    day_cap = company_cap() if day_cap is None else day_cap
    order = []
    for j in top:
        if (j.get("loc_country") or "US") != "US":
            continue
        k = key_of(j.get("company") or "")
        if k and k not in order:
            order.append(k)
    recent = searched_since(order, today)
    connected = connected_contacts(set(order))
    slots = max(0, day_cap - searched_count(today))
    out = {}
    for rank, k in enumerate(order, 1):
        if k in connected:
            out[k] = {"action": "connected", "rank": rank, "contact_id": connected[k]}
        elif k in recent:
            out[k] = {"action": "cache", "rank": rank}
        elif slots > 0:
            slots -= 1
            out[k] = {"action": "search", "rank": rank}
        else:
            out[k] = {"action": "skip", "rank": rank}
    return out


# ── searching one company ────────────────────────────────────────────────────

def _card(h: dict, kind, audience, query) -> dict:
    card = {**{k: h.get(k) for k in ("name", "headline", "url", "location", "country", "degree", "source")},
            "kind": kind, "audience": audience, "query": query, "current": True}
    if h.get("from_post"):
        card.update(from_post=True, evidence=h.get("evidence") or "")
    return card


def _order(people: list) -> list:
    """Best first, in the owner's order of audiences: alumni, ex-colleagues, engineers, recruiters, the rest."""
    from backend.curator import audience as aud
    return sorted(people, key=lambda p: aud.rank(p.get("audience") or p.get("kind")))


class FacetResult(list):
    """The people a facet search found, plus `complete`: every step planned for the company ran on a usable
    page (not blocked, walled, facet-dropped, or cut off by the budget). Only a complete search marks the
    company searched."""
    complete = True


async def facet_search(li: LinkedInPeople, company: str, *, want: int, top3: bool, aff, known=frozenset(),
                       country: str = "US") -> "FacetResult | None":
    """Alumni, then role, then (top 3) authors of the company's hiring posts, then (top 3, nothing found yet)
    ex-colleague people at `company`, best first, up to
    `want`. None when the company's LinkedIn id is not known (the caller searches by keyword instead). Up to
    MAX_PER_STEP people per query are returned, more than `want`, so the cache holds the rest for later."""
    from backend.curator.people import classify
    key = key_of(company)
    cid = entity_ids.lookup(entity_ids.COMPANY, company)
    if not cid:
        li.wanted_ids.add(key)
        return None
    schools = entity_ids.ids_for(entity_ids.SCHOOL, getattr(aff, "schools", []))
    past = entity_ids.ids_for(entity_ids.COMPANY, getattr(aff, "employers", []))
    found: dict = {}
    state = {"complete": True}
    bad_before = li.bad_pages

    def room(qtype=None) -> bool:
        """A load is available and the query type is usable; otherwise the company is left unfinished."""
        ok = li_budget.remaining(li_budget.MAIN) >= 1 and not (qtype and li_budget.unusable(qtype))
        if not ok:
            state["complete"] = False
        return ok

    def take(hits, kind_for, audience, query, limit=MAX_PER_STEP):
        n = 0
        for h in hits:
            url = (h.get("url") or "").split("?")[0]
            if not url or url in found or url in known:
                continue
            if h.get("degree") == 1:            # already a connection: write to him, do not "find" him
                continue
            found[url] = _card({**h, "url": url}, kind_for(h), audience, query)
            n += 1
            if n >= limit:
                break

    def enough() -> bool:
        return len(found) >= want

    if schools and room("alumni"):
        hits = await li.search("", country, ["F", "S"], current_company=cid, schools=[i for _, i in schools],
                               qtype="alumni", names={"currentCompany": company, "schoolFilter": schools[0][0]})
        take(hits, lambda h: classify(h.get("headline") or ""), "alum", "alumni", limit=2)
    role_hits = 0
    titles = _ROLE_TITLES if top3 else _ROLE_TITLES[:1]
    for i, title in enumerate(titles):
        if enough():
            break
        if not room("role"):
            break
        if i and not role_hits:                 # the second title only follows a first that found people
            break
        hits = await li.search("", country, ["F", "S"], current_company=cid, title=title, qtype="role",
                               names={"currentCompany": company})
        before = len(found)
        take(hits, lambda h, t=title: classify(h.get("headline") or "") or _TITLE_KIND[t], None, f"role:{title}")
        role_hits += len(found) - before
    if top3 and not enough() and room("posts_company"):
        from backend.curator import audience as aud
        from backend.curator.scout import parse_post
        posts = await li.search_posts("hiring", author_company=cid, qtype="posts_company", name=company)
        leads = [lead for lead in (parse_post(p) for p in posts) if lead]
        hits = [{**lead, "from_post": True, "evidence": lead["evidence"][:POST_EVIDENCE_MAX]} for lead in leads]
        take(hits, lambda h: classify(h.get("headline") or ""), None, "posts_company", limit=2)
        for card in found.values():        # an author's audience: hiring_manager when the headline says so, else post
            if card.get("from_post"):
                card["audience"] = aud.detect(card.get("headline") or "", "", None, from_post=True)
    if not found and top3 and past and room("ex_colleague"):
        hits = await li.search("", country, ["F", "S"], current_company=cid, past_company=[i for _, i in past],
                               qtype="ex_colleague", names={"currentCompany": company, "pastCompany": past[0][0]})
        take(hits, lambda h: classify(h.get("headline") or ""), "ex_colleague", "ex_colleague", limit=1)
    out = FacetResult(found.values())
    out.complete = state["complete"] and li.bad_pages == bad_before
    return out


async def people_for(li: LinkedInPeople, job: dict, decision: dict, *, want: int, aff, known=frozenset(),
                     city: str | None = None, today: date | None = None) -> list:
    """Up to `want` people for the company of `job`, from the cache first, then (when the plan gave it a slot and
    the run's LinkedIn share has loads left) from a search. Never raises on an empty day: [] means nobody."""
    today = today or local_today()
    company, key = job["company"], key_of(job["company"])
    action = (decision or {}).get("action", "skip")
    if action == "connected":
        return []
    cached = cached_people(key, today, known)
    if cached or action != "search":
        return _order(cached)[:want]
    top3 = (decision or {}).get("rank", 99) <= TOP_N
    if li_budget.remaining(li_budget.MAIN) < (MIN_LOADS_TOP3 if top3 else MIN_LOADS_PER_COMPANY):
        return []                               # not room for its planned steps: it keeps its slot for the next run
    loads_before, bad_before = li.loads, li.bad_pages
    people = await facet_search(li, company, want=want, top3=top3, aff=aff, known=known)
    if people is None:                          # id unknown: keyword search, as before
        from backend.curator.people import find_people
        keyword = await find_people(company, job.get("title") or "", li, want=want,
                                    country=job.get("loc_country") or "US", region=job.get("loc_region"), city=city)
        people = [{**p, "query": "keyword"} for p in keyword if p.get("url") not in known]
        complete = li.bad_pages == bad_before
    else:
        complete = getattr(people, "complete", False)
    # searched = LinkedIn was asked and every planned step ran on a usable page; a block, a walled page, a dropped
    # facet or the budget running out between steps leaves the company eligible for the next run
    searched = li.loads > loads_before and complete
    remember(key, people, today, searched=searched)
    return _order(people)[:want]
