"""The lead scout: people worth knowing, found from who the user is rather than from today's jobs.

Runs once a day per account (scheduler `network_scout`, after the curator) and on demand
(POST /scout/run). Sources, cheapest first:

1. Hiring posts: LinkedIn posts from the past week that say someone is hiring for the user's
   roles or domains (mock session). The author is the lead.
2. Project fit: founders, CTOs and engineering managers whose public profile matches a domain
   the user's projects show (SearXNG over linkedin.com/in; no LinkedIn request).
3. Affinity: people from the user's schools and past employers who now lead or hire (SearXNG).
4. Network distance: the best few leads are looked up once on the main session to learn whether
   they are a 2nd or 3rd connection (capped by linkedin_main_daily_cap).

Every candidate is scored against the persona's networking profile by the curator model, which
writes why the person matters, two or three talking points, the project to show them and a
connection note; the best `scout_daily_leads` are saved as contacts (purpose 'network').
Follow-ups: a request without an answer for 7 days, or a connection not yet messaged after 3,
becomes due again.
"""
import asyncio
import logging
import re
from datetime import timedelta

from backend.models.db import Contact, SessionLocal
from backend.search.searxng import SearchError, search
from backend.tz import local_today

logger = logging.getLogger("jobnavigator.curator.scout")

FOLLOW_UP_AFTER = {"requested": 7, "connected": 3, "messaged": 7}
MAX_SCORED = 40          # candidates the model scores per run
DEGREE_CHECKS = 5        # main-session lookups per run (also capped by the session's own budget)
MIN_SCORE = 55

_DEFAULT_ROLES = ["backend engineer", "software engineer"]
_LEAD_TITLES = '("founder" OR "co-founder" OR "CTO" OR "engineering manager" OR "head of engineering" OR "technical recruiter")'

_SYSTEM = ("You help a job seeker decide who is worth contacting on LinkedIn and how. Be blunt: most "
           "people are not worth it. Never invent facts about the sender or the person beyond what you "
           "are given. No flattery, no buzzwords, no em dashes.")
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
sponsor an F-1 STEM OPT holder later (H-1B) or is the role abroad with a work visa, does a sender
project match their work, is there a shared school or employer.

Return JSON:
{{"score": <0-100>,
  "kind": "hiring_manager|founder|recruiter|engineer|other",
  "why": "<one sentence, concrete>",
  "talking_points": ["<2-3 specific openers tied to their work or post and the sender's projects>"],
  "project": "<name of the one sender project to show them, or empty>",
  "note": "<LinkedIn connection note, under 280 characters, specific, ends with a light ask>"}}"""


# ── candidate discovery ──────────────────────────────────────────────────────

def parse_profile_hit(r: dict) -> dict | None:
    """{name, headline, url, company, location} from a SearXNG result for a linkedin.com/in profile."""
    from backend.curator.people import _EXPERIENCE, _LOCATION, profile_country
    url = (r.get("url") or "").split("?")[0]
    if "linkedin.com/in/" not in url:
        return None
    title = re.sub(r"\s*\|\s*LinkedIn.*$", "", r.get("title") or "").strip()
    parts = re.split(r"\s+[-–—|]\s+", title, maxsplit=1)
    name, headline = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
    if not name or len(name) > 60:
        return None
    snippet = r.get("snippet") or ""
    exp = _EXPERIENCE.search(snippet)
    loc = _LOCATION.search(snippet)
    location = loc.group(1).strip(" .…") if loc else None
    company = exp.group(1).strip(" .…") if exp else company_from_headline(headline)
    return {"name": name, "headline": headline or snippet[:140], "url": url, "company": company or "",
            "location": location, "country": profile_country(url, location), "evidence": snippet[:400]}


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
    """{posts: [...], project_fit: [...], affinity: [...]} search strings from the networking profile."""
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
    posts = [f'hiring "{r}"' for r in roles[:2]] + [f"hiring {d.replace('-', ' ')} engineer" for d in domains[:2]]
    project_fit = [f'site:linkedin.com/in {_LEAD_TITLES} "{d.replace("-", " ")}" startup {city}' for d in domains[:3]]
    affinity = [f'site:linkedin.com/in "{a}" {_LEAD_TITLES}' for a in (net.get("affinities") or [])[:4] if a]
    # LinkedIn's own people search (mock session) takes plain keywords, one title at a time
    words = [_DOMAIN_WORDS.get(d, d.replace("-", " ")) for d in domains[:3]]
    li_people = [("project_fit", f"{t} {w}") for w in words for t in ("engineering manager", "founder")][:4]
    li_people += [("alumni", f"{a} engineering manager") for a in (net.get("affinities") or [])[:2] if a]
    return {"posts": posts, "project_fit": project_fit, "affinity": affinity, "li_people": li_people}


_DOMAIN_WORDS = {"llm-infra": "LLM", "devtools": "developer tools", "healthtech": "health tech",
                 "fintech": "fintech", "homelab": "self-hosted"}


async def _searx_leads(queries: list[str], kind: str, n: int = 10) -> list[dict]:
    out = []
    for q in queries:
        try:
            for r in await search(q, n=n):
                hit = parse_profile_hit(r)
                if hit:
                    out.append({**hit, "lead_kind": kind, "source": "searxng"})
        except SearchError as e:
            logger.warning(f"scout search failed ({q!r}): {e}")
            break
    return out


# ── scoring and saving ───────────────────────────────────────────────────────

async def score_lead(c: dict, brief: str) -> dict | None:
    from backend.curator.llm import ask_json
    out = await ask_json(_PROMPT.format(
        brief=brief, kind=c.get("lead_kind"), name=c.get("name"), headline=c.get("headline") or "",
        company=c.get("company") or "unknown", location=c.get("location") or "unknown",
        evidence=(c.get("evidence") or "")[:600]), _SYSTEM, "scout_lead_score", max_tokens=700)
    if not out:
        return None
    try:
        score = int(out.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    tp = out.get("talking_points")
    return {**c, "score": max(0, min(100, score)), "kind": out.get("kind") or None,
            "why": (out.get("why") or "")[:500],
            "talking_points": [str(x)[:300] for x in tp][:3] if isinstance(tp, list) else [],
            "project_ref": (out.get("project") or "")[:120] or None, "note": (out.get("note") or "")[:300]}


def _known_urls(db) -> set:
    return {u for (u,) in db.query(Contact.linkedin_url).filter(Contact.linkedin_url.isnot(None)).all()}


def mark_follow_ups(db, today=None) -> int:
    """Leads whose last step has waited long enough become due today; returns how many."""
    today = today or local_today()
    n = 0
    for status, days in FOLLOW_UP_AFTER.items():
        for c in db.query(Contact).filter(Contact.status == status).all():
            last = c.last_action_at.date() if c.last_action_at else c.found_on
            if last and (today - last).days >= days and (c.next_action_at is None or c.next_action_at > today):
                c.next_action_at = today
                n += 1
    db.commit()
    return n


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


def _daily_target() -> int:
    from backend.analyzer.llm_client import _get_setting
    db = SessionLocal()
    try:
        return max(0, int(_get_setting(db, "scout_daily_leads", "15")))
    except (TypeError, ValueError):
        return 15
    finally:
        db.close()


async def run_network_scout(sync_projects: bool = True) -> dict:
    """One scout run for the current account; returns counts for the run summary."""
    from backend.curator import llm
    from backend.curator.linkedin_people import LinkedInPeople
    from backend.curator import li_budget
    want = _daily_target()
    summary = {"candidates": 0, "scored": 0, "saved": 0, "follow_ups": 0, "posts": 0, "errors": []}
    db = SessionLocal()
    try:
        summary["follow_ups"] = mark_follow_ups(db)
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
    q = plan_queries(net, contact)
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
        searx = await _searx_leads(q["project_fit"], "project_fit") + await _searx_leads(q["affinity"], "alumni")
        cands.extend(searx)
        if len(searx) < want:
            # SearXNG's engines often ignore site: and return no profiles; ask LinkedIn instead
            for kind, kw in q["li_people"]:
                try:
                    hits = await li.search(kw, country="US")
                except Exception as e:
                    summary["errors"].append(f"people: {e}")
                    break
                for h in hits:
                    cands.append({"name": h["name"], "headline": h.get("headline") or "", "url": h["url"],
                                  "company": h.get("current_text") or company_from_headline(h.get("headline") or ""),
                                  "location": h.get("location"), "country": h.get("country"),
                                  "degree": h.get("degree"), "evidence": h.get("headline") or "",
                                  "lead_kind": kind, "source": h.get("source")})

        db = SessionLocal()
        try:
            known = _known_urls(db)
        finally:
            db.close()
        seen, uniq = set(known), []
        for c in cands:
            if c.get("url") and c["url"] not in seen and (c.get("country") in (None, "US") or c["lead_kind"] == "hiring_post"):
                seen.add(c["url"])
                uniq.append(c)
        summary["candidates"] = len(uniq)
        scored = await asyncio.gather(*(score_lead(c, brief) for c in uniq[:MAX_SCORED]), return_exceptions=True)
        best = sorted((s for s in scored if isinstance(s, dict) and s["score"] >= MIN_SCORE),
                      key=lambda s: -s["score"])[:want]
        summary["scored"] = sum(1 for s in scored if isinstance(s, dict))

        # network distance for the best few, on the main session
        for c in best[:DEGREE_CHECKS]:
            if not li_budget.available(li_budget.MAIN):
                break
            try:
                hits = await li.search(f"{c['name']} {c.get('company') or ''}".strip(), network=["F", "S", "O"])
            except Exception as e:
                summary["errors"].append(f"degree: {e}")
                break
            match = next((h for h in hits if h["url"].rstrip("/") == c["url"].rstrip("/")), None)
            if match:
                c["degree"] = match.get("degree")

    today = local_today()
    db = SessionLocal()
    try:
        for c in best:
            row = Contact(job_id=None, company=c.get("company") or "", name=c["name"], title=c.get("headline"),
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
            + (f"; errors: {' | '.join(s['errors'])}"[:500] if s.get("errors") else ""))
