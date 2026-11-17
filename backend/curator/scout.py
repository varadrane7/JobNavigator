"""The lead scout: people worth knowing, found from who the user is rather than from today's jobs.

Runs once a day per account (scheduler `network_scout`, after the curator) and on demand
(POST /scout/run). Sources, cheapest first:

1. Hiring posts: LinkedIn posts from the past week that say someone is hiring for the user's
   roles or domains (mock session). The author is the lead.
2. Alumni: one faceted people search on the main session for the user's schools (schoolFilter ids, network
   1st/2nd, US) when their LinkedIn ids are known; else a keyword search per school.
3. Project fit: founders, CTOs and engineering managers whose profile matches a domain the user's projects
   show (LinkedIn keyword search).
The scout shares the LinkedIn allocator (curator/li_budget.py): with two or more curator runs a day it draws only on
the reserve the runs leave over, never on their shares. People it finds are filed per company
(curator/company_first.py) so the curator's company-first search can reuse them.
4. Network distance: read off the result cards (• 2nd / • 3rd+); no separate lookup is made.

Every candidate is scored against the persona's networking profile by the curator model, which
writes why the person matters, two or three talking points, the project to show them and a
connection note; the best `scout_daily_leads` are saved as contacts (purpose 'network').
Follow-ups (curator/followups.py): a request or message with no answer becomes due again at day 4 and
day 9 (settings followup_days, followup_max), then the lead is closed as no reply.

The scorer does not write notes. The best leads are saved first, then each one's connection note is drafted
for its audience (curator/audience.py, curator/drafting.py) and validated against exactly what the model saw.
"""
import asyncio
import logging
import re
from datetime import timedelta

from backend.models.db import Contact, SessionLocal
from backend.tz import local_today

logger = logging.getLogger("jobnavigator.curator.scout")

MAX_SCORED = 40          # candidates the model scores per run
MIN_SCORE = 50

_DEFAULT_ROLES = ["backend engineer", "software engineer"]

_SYSTEM = ("You help a job seeker decide who is worth contacting on LinkedIn. Be blunt: most "
           "people are not worth it. Never invent facts about the sender or the person beyond what you "
           "are given. No flattery, no buzzwords, no em dashes.")
# P02. Scoring only: the connection note is written afterwards for the leads that are saved, by the shared
# drafting prompt (curator/drafting.py), so a note is never written for a candidate that is dropped.
_PROMPT = """Sender profile:
{brief}

Candidate (found via {kind}):
Name: {name}
Headline: {headline}
Company: {company}
Location: {location}
Evidence: {evidence}

Score how likely this person is to help the sender get hired (hire them, refer them, introduce them,
or give useful feedback on a project) within the next month. Consider: are they hiring or able to
refer for roles the sender fits (early-career backend / AI engineering), is the company likely to
support the sender's work authorization as stated under "Work authorization" in the sender profile
above (sponsorship, or a work visa when the role is abroad; if none is stated, do not assume one),
does a sender project match their work, is there a shared school or employer.

Return JSON:
{{"score": <0-100>,
  "kind": "hiring_manager|founder|recruiter|engineer|other",
  "why": "<one sentence, concrete>",
  "talking_points": ["<2-3 specific openers tied to their work or post and the sender's projects>"],
  "project": "<name of the one sender project to show them, or empty>"}}"""

# With outreach drafts off for the profile the model is not asked for talking points.
_PROMPT_NO_DRAFTS = _PROMPT.replace(
    """  "talking_points": ["<2-3 specific openers tied to their work or post and the sender's projects>"],\n""", "")


# ── candidate discovery ──────────────────────────────────────────────────────

def company_from_headline(headline: str) -> str:
    m = re.search(r"(?:\bat|@)\s+([A-Z0-9][\w&.\- ]{1,40}?)(?:\s*[|,·•(]|$)", headline or "")
    return m.group(1).strip() if m else ""


def parse_post(p: dict) -> dict | None:
    """A lead from a LinkedIn post card: the actor block reads 'Name\\nHeadline\\n...'."""
    lines = [l.strip(" •·") for l in (p.get("actor") or "").splitlines() if l.strip(" •·")]
    lines = [l for l in lines if not re.match(r"^(\d+[smhdw]\b|follow|.*followers|promoted|edited|\d(st|nd|rd)|view my |"
                                                r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}$)", l, re.I)]
    if not lines or not p.get("author_url") or "/in/" not in p["author_url"]:
        return None
    name = re.sub(r"\s*(View .*|’s profile|'s profile).*$", "", lines[0]).strip()
    headline = lines[1] if len(lines) > 1 else ""
    text = p.get("text") or ""
    if not re.search(r"\bhiring\b|\bjoin (us|our team)\b|\bwe'?re looking\b|\bopen (role|position)s?\b", text, re.I):
        return None
    return {"name": name, "headline": headline, "url": p["author_url"].split("?")[0],
            "company": company_from_headline(headline), "location": None, "country": None,
            "evidence": text[:500], "source": p.get("source")}


def plan_queries(net: dict, contact: dict) -> dict:
    """{posts: [...], li_people: [(kind, keywords)]} search strings from the networking profile."""
    roles = [r for r in net.get("target_roles") or [] if r][:3] or _DEFAULT_ROLES
    domains = [d for d in net.get("target_domains") or [] if d]
    if not domains:
        seen = []
        for p in net.get("projects") or []:
            if isinstance(p, dict) and not p.get("hidden"):
                for d in p.get("domains") or []:
                    if d not in seen:
                        seen.append(d)
        domains = seen
    domains = domains[:4]
    city = (contact.get("city") or "New York").strip()
    # posts are worldwide unless a place is named: the user's city, and US remote
    posts = [f'hiring "{r}" {city}' for r in roles[:2]] + [f'hiring "{roles[0]}" remote US'] \
        + [f"hiring {_DOMAIN_WORDS.get(d, d.replace('-', ' '))} engineer {city}" for d in domains[:2]]
    # LinkedIn's own people search (mock session) takes plain keywords, one title at a time
    words = [_DOMAIN_WORDS.get(d, d.replace("-", " ")) for d in domains[:3]]
    li_people = [("project_fit", f"{t} {w}") for w in words for t in ("engineering manager", "founder")][:4]
    li_people += [("alumni", f"{a} engineering manager") for a in (net.get("affinities") or [])[:2] if a]
    return {"posts": posts, "li_people": li_people}


_DOMAIN_WORDS = {"llm-infra": "LLM", "devtools": "developer tools", "healthtech": "health tech",
                 "fintech": "fintech", "homelab": "self-hosted"}


# ── scoring and saving ───────────────────────────────────────────────────────

def _prompt_for(c: dict, brief: str, drafts: bool = True) -> str:
    return (_PROMPT if drafts else _PROMPT_NO_DRAFTS).format(
        brief=brief, kind=c.get("lead_kind"), name=c.get("name"), headline=c.get("headline") or "",
        company=c.get("company") or "unknown", location=c.get("location") or "unknown",
        evidence=(c.get("evidence") or "")[:600])


def with_audience(c: dict, aff) -> dict:
    """`c` with its audience, read from what the person's own card says (their headline and, for a profile
    found by search, the snippet). A hiring post's text is never read for schools or employers."""
    from backend.curator import audience as aud
    post = c.get("lead_kind") == "hiring_post"
    text = "" if post else (c.get("evidence") or "")
    return {**c, "audience": aud.detect(c.get("headline") or "", text, aff, from_post=post)}


async def settle_scout_note(c: dict, sender, aff=None) -> dict:
    """Draft the connection note for the saved lead `c` for its audience, validate it (curator/note_check.py),
    retry once with the problems named, and when it still fails save no note and flag the lead for a hand-written
    one. Returns c with `note`, `note_manual`, `note_issue` and `audience` set. Never cuts a note."""
    from backend.curator import drafting
    c = c if c.get("audience") is not None or aff is None else with_audience(c, aff)
    brief = drafting.Brief(sender=sender, name=c.get("name") or "", card=drafting.card_text(c),
                           evidence=(c.get("evidence") or "")[:600] if c.get("evidence") != c.get("headline") else "",
                           company=c.get("company") or "", audience=c.get("audience"),
                           from_post=c.get("lead_kind") == "hiring_post")
    res = await drafting.draft_text("note", brief, "scout_note", max_tokens=500)
    return {**c, "note": res.note, "note_manual": res.manual, "note_issue": res.issue}


async def score_lead(c: dict, brief: str, drafts: bool = True) -> dict | None:
    from backend.curator.llm import ask_json
    out = await ask_json(_prompt_for(c, brief, drafts), _SYSTEM, "scout_lead_score", max_tokens=700)
    if not out:
        return None
    try:
        score = int(out.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    if not drafts:
        out = {**out, "talking_points": None}
    tp = out.get("talking_points")
    return {**c, "score": max(0, min(100, score)), "kind": out.get("kind") or None,
            "why": (out.get("why") or "")[:500],
            "talking_points": [str(x)[:300] for x in tp][:3] if isinstance(tp, list) else [],
            "project_ref": (out.get("project") or "")[:120] or None}


def _known_urls(db) -> set:
    return {u for (u,) in db.query(Contact.linkedin_url).filter(Contact.linkedin_url.isnot(None)).all()}


def _profile() -> tuple[str, dict, dict]:
    """(brief for prompts, networking profile, persona contact)."""
    from backend.accounts.owned import get_persona
    from backend.curator.projects_sync import networking_brief
    from backend.curator.run import _me
    _name, resume_brief, contact = _me()
    db = SessionLocal()
    try:
        p = get_persona(db, create=False)
        net = dict((p.networking if p else None) or {})
        wa = (p.work_auth if p else None) or {}
    finally:
        db.close()
    if not net.get("visa_note") and wa.get("requires_sponsorship_future"):
        net["visa_note"] = "Will need H-1B sponsorship later"
    brief = "\n".join(x for x in (networking_brief(net), resume_brief[:1800]) if x)
    return brief, net, contact


def _min_score() -> int:
    from backend.analyzer.llm_client import _get_setting
    db = SessionLocal()
    try:
        return int(_get_setting(db, "scout_min_score", str(MIN_SCORE)))
    except (TypeError, ValueError):
        return MIN_SCORE
    finally:
        db.close()


def _daily_target() -> int:
    from backend.analyzer.llm_client import _get_setting
    db = SessionLocal()
    try:
        return max(0, int(_get_setting(db, "scout_daily_leads", "15")))
    except (TypeError, ValueError):
        return 15
    finally:
        db.close()


def li_queries(li_people: list, aff) -> list:
    """[(kind, keywords, facet kwargs)] for the scout's LinkedIn people searches: the alumni query first, as one
    faceted search over the user's schools (their ids from li_entity_ids; 1st and 2nd degree, US) when any is
    known, else the keyword queries of plan_queries; then the project-fit keyword queries."""
    from backend.curator import entity_ids
    schools = entity_ids.ids_for(entity_ids.SCHOOL, getattr(aff, "schools", []))
    out, keyword_alumni = [], [x for x in li_people if x[0] == "alumni"]
    if schools:
        out.append(("alumni", "engineering manager", {"schools": [i for _, i in schools], "network": ["F", "S"],
                                                      "qtype": "alumni_scout", "names": {"schoolFilter": schools[0][0]}}))
    else:
        out.extend((k, kw, {}) for k, kw in keyword_alumni)
    out.extend((k, kw, {}) for k, kw in li_people if k != "alumni")
    return out


async def run_network_scout(sync_projects: bool = True) -> dict:
    """One scout run for the current account; returns counts for the run summary."""
    from backend.curator import li_budget
    from backend.curator.run import _settings
    li_budget.begin_scout(await asyncio.to_thread(lambda: _settings()["runs_per_day"]))
    try:
        return await _run_network_scout(sync_projects)
    finally:
        li_budget.end_run()


async def _run_network_scout(sync_projects: bool) -> dict:
    from backend.curator import llm
    from backend.curator.linkedin_people import LinkedInPeople
    from backend.curator import li_budget
    want = _daily_target()
    summary = {"candidates": 0, "scored": 0, "saved": 0, "follow_ups": 0, "posts": 0, "errors": [], "best": []}
    db = SessionLocal()
    try:
        from backend.curator import followups
        summary["follow_ups"] = followups.mark_due(db)
    finally:
        db.close()
    if want <= 0:
        return summary

    brief, net, contact = _profile()
    if sync_projects and (net.get("github_user") or net.get("projects")):
        from datetime import datetime, timezone
        synced = net.get("projects_synced_at")
        stale = not synced or datetime.fromisoformat(synced) < datetime.now(timezone.utc) - timedelta(days=7)
        if stale and net.get("github_user"):
            try:
                from backend.curator.projects_sync import sync_projects as _sync
                await _sync()
                brief, net, contact = _profile()
            except Exception as e:
                logger.warning(f"scout: project sync failed: {e}")
                summary["errors"].append(f"project sync: {e}")
    if not brief.strip():
        summary["errors"].append("no persona or résumé to scout from")
        return summary

    llm.reset_run()
    from backend.curator.people import drafts_enabled
    drafts = drafts_enabled()
    if not drafts:
        logger.info("scout: outreach drafts are off for this profile; leads are scored without notes or talking points")
        summary["drafts"] = "off"
    q = plan_queries(net, contact)
    from backend.curator import audience as aud
    aff = await asyncio.to_thread(aud.load_affiliations)
    cands: list[dict] = []
    async with LinkedInPeople() as li:
        for query in q["posts"]:
            try:
                posts = await li.search_posts(query)
            except Exception as e:
                summary["errors"].append(f"posts: {e}")
                break
            summary["posts"] += len(posts)
            cands.extend({**lead, "lead_kind": "hiring_post"} for lead in (parse_post(p) for p in posts) if lead)
        # LinkedIn's own people search, inside the scout's share of the allocator (the reserve); the alumni
        # query goes first, as one faceted search over the user's schools when their ids are known
        hits_all = []
        for kind, kw, facets in await asyncio.to_thread(li_queries, q["li_people"], aff):
            if not (li_budget.available(li_budget.MOCK) or li_budget.available(li_budget.MAIN)):
                summary["errors"].append("LinkedIn budget for the scout is spent")
                break
            try:
                hits = await li.search(kw, country="US", **facets)
            except Exception as e:
                summary["errors"].append(f"people: {e}")
                break
            for h in hits:
                c = {"name": h["name"], "headline": h.get("headline") or "", "url": h["url"],
                     "company": h.get("current_text") or company_from_headline(h.get("headline") or ""),
                     "location": h.get("location"), "country": h.get("country"),
                     "degree": h.get("degree"), "evidence": h.get("headline") or "",
                     "lead_kind": kind, "source": h.get("source"),
                     "company_verified": bool(h.get("current_text"))}
                cands.append(c)
                hits_all.append(c)
        if hits_all:
            from backend.curator import company_first
            await asyncio.to_thread(company_first.remember_scouted, hits_all)

        db = SessionLocal()
        try:
            known = _known_urls(db)
        finally:
            db.close()
        seen, uniq = set(known), []
        by_url: dict = {}
        for c in cands:
            if c.get("url") and c["url"] in by_url:
                if by_url[c["url"]].get("degree") is None and c.get("degree"):
                    by_url[c["url"]]["degree"] = c["degree"]       # the card of a search result says 2nd / 3rd+
                continue
            if c.get("url") and c["url"] not in seen and (c.get("country") in (None, "US") or c["lead_kind"] == "hiring_post"):
                seen.add(c["url"])
                by_url[c["url"]] = c
                uniq.append(c)
        summary["candidates"] = len(uniq)
        uniq = [with_audience(c, aff) for c in uniq]
        scored = await asyncio.gather(*(score_lead(c, brief, drafts) for c in uniq[:MAX_SCORED]), return_exceptions=True)
        best = sorted((s for s in scored if isinstance(s, dict) and s["score"] >= _min_score()),
                      key=lambda s: -s["score"])[:want]
        summary["scored"] = sum(1 for s in scored if isinstance(s, dict))
        ranked = sorted((s for s in scored if isinstance(s, dict)), key=lambda s: -s["score"])
        summary["best"] = [f"{s['name']} ({s['lead_kind']}) {s['score']}" for s in ranked[:5]]

    if drafts:      # the note is drafted for the audience, checked, retried once, then saved or flagged
        from backend.curator import drafting
        sender = await asyncio.to_thread(drafting.load_sender)
        best = list(await asyncio.gather(*(settle_scout_note(c, sender, aff) for c in best)))

    today = local_today()
    db = SessionLocal()
    try:
        for c in best:
            row = Contact(audience=c.get("audience"), message_stage="note" if c.get("note") else None,
                           note_manual=bool(c.get("note_manual")), note_issue=c.get("note_issue"), job_id=None, company=c.get("company") or "", name=c["name"], title=c.get("headline"),
                           kind=c.get("kind"), linkedin_url=c["url"], location=c.get("location"),
                           source=c.get("source"), note=c.get("note"), status="new", purpose="network",
                           lead_kind=c["lead_kind"], degree=c.get("degree"), score=c["score"], why=c.get("why"),
                           talking_points=c.get("talking_points"), project_ref=c.get("project_ref"),
                           evidence=(c.get("evidence") or "")[:1000], found_on=today, next_action_at=today)
            try:
                with db.begin_nested():
                    db.add(row)
                summary["saved"] += 1
            except Exception:   # the same person at the same company, saved by another run
                pass
        db.commit()
    finally:
        db.close()
    logger.info(f"scout done: {summary}")
    return summary


def summary_text(s: dict) -> str:
    return (f"{s['saved']} new leads from {s['candidates']} candidates ({s['scored']} scored, {s['posts']} posts read), "
            f"{s['follow_ups']} follow-ups due"
            + (f"; best scores: {', '.join(s['best'])}" if s.get("best") and not s["saved"] else "")
            + (f"; errors: {' | '.join(s['errors'])}"[:500] if s.get("errors") else ""))
