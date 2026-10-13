"""Find people to contact about a curated job: founders, hiring managers, recruiters.

Public search first (SearXNG `site:linkedin.com/in`), LinkedIn's own people search
only when that finds nobody useful, then a best-guess work email from the company's
known email format, and a short LinkedIn note + cold email drafted by the LLM.
"""
import logging
import re

from backend.curator.intel import make_email
from backend.curator.linkedin_people import LinkedInPeople
from backend.curator.llm import ask_json
from backend.search.searxng import SearchError, search

logger = logging.getLogger("jobnavigator.curator.people")

KIND_PATTERNS = [
    ("founder", re.compile(r"\b(co-?founder|founder|ceo|cto|chief technology)\b", re.I)),
    ("hiring_manager", re.compile(r"\b(engineering manager|eng(ineering)? lead|head of (engineering|platform|infrastructure|backend)|"
                                  r"director,? (of )?engineering|vp,? (of )?engineering|manager,? software|software engineering manager)\b", re.I)),
    ("recruiter", re.compile(r"\b(recruit\w*|talent acquisition|sourcer|talent partner|people partner)\b", re.I)),
    ("engineer", re.compile(r"\b(engineer|developer)\b", re.I)),
]


def classify(headline: str) -> str | None:
    for kind, rx in KIND_PATTERNS:
        if rx.search(headline or ""):
            return kind
    return None


def split_name(name: str) -> tuple[str, str]:
    clean = re.sub(r"\(.*?\)|,.*$|\b(MBA|PhD|PMP|CPA)\b", "", name or "").strip()
    parts = [p for p in clean.split() if p]
    if not parts:
        return "", ""
    return parts[0], (parts[-1] if len(parts) > 1 else "")


def parse_linkedin_hit(r: dict, company: str) -> dict | None:
    """{name, headline, url} from a SearXNG result for a linkedin.com/in profile at `company`."""
    url = (r.get("url") or "").split("?")[0]
    if "linkedin.com/in/" not in url:
        return None
    title = re.sub(r"\s*\|\s*LinkedIn.*$", "", r.get("title") or "").strip()
    parts = re.split(r"\s+[-–—|]\s+", title, maxsplit=1)
    name, headline = parts[0], (parts[1] if len(parts) > 1 else "")
    headline = headline or (r.get("snippet") or "")[:140]
    blob = f"{title} {r.get('snippet', '')}".lower()
    ckey = company.lower().split()[0] if company else ""
    if not name or not ckey or ckey not in blob:
        return None
    return {"name": name.strip(), "headline": headline.strip(), "url": url}


def priority(kind: str | None, small_company: bool) -> int:
    if kind == "hiring_manager":
        return 3
    if kind == "founder":
        return 3 if small_company else 1
    if kind == "recruiter":
        return 2
    return 0


async def find_people(company: str, job_title: str, small_company: bool, li: LinkedInPeople | None,
                      want: int = 2) -> list[dict]:
    """Up to `want` people at `company`, best first."""
    cands: dict[str, dict] = {}
    queries = [
        f'site:linkedin.com/in {company} ("engineering manager" OR "head of engineering" OR founder OR CTO)',
        f'site:linkedin.com/in {company} (recruiter OR "technical recruiter" OR "talent acquisition")',
    ]
    for q in queries:
        try:
            for r in await search(q, n=10):
                hit = parse_linkedin_hit(r, company)
                if hit:
                    hit["source"] = "searxng"
                    cands.setdefault(hit["url"], hit)
        except SearchError as e:
            logger.warning(f"people search for {company} failed: {e}")
            break

    def ranked():
        out = []
        for c in cands.values():
            kind = classify(c["headline"])
            if kind and kind != "engineer":
                out.append({**c, "kind": kind, "_p": priority(kind, small_company)})
        return sorted(out, key=lambda c: -c["_p"])

    best = ranked()
    if (not best or best[0]["_p"] < 2) and li is not None:
        for kw in (f"{company} engineering manager", f"{company} technical recruiter"):
            for hit in await li.search(kw):
                if company.lower().split()[0] in (hit["headline"] or "").lower():
                    cands.setdefault(hit["url"], hit)
            best = ranked()
            if best and best[0]["_p"] >= 2:
                break
    return [{k: v for k, v in c.items() if k != "_p"} for c in best[:want]]


_DRAFT_SYSTEM = ("You write short, specific, human outreach for a job seeker. No flattery, no buzzwords, "
                 "no em dashes. Never invent facts about the sender beyond the profile given.")

_DRAFT_PROMPT = """Sender: {me}
Sender profile: {profile}
Target: {name}, {headline} at {company}
Role the sender is applying to: {job_title} ({job_url})

Write JSON:
- "note": LinkedIn connection note, max 280 characters, mentions the role and one concrete overlap
- "email_subject": max 8 words
- "email_body": 70-110 words, plain text, says the sender applied (or is applying today), one line of relevant proof, a soft ask for a quick chat or a referral, sign-off with the sender's first name"""


async def draft(contact: dict, company: str, job: dict, me: str, profile: str) -> dict:
    out = await ask_json(
        _DRAFT_PROMPT.format(me=me, profile=profile[:900], name=contact["name"], headline=contact.get("headline", ""),
                             company=company, job_title=job.get("title", ""), job_url=job.get("url", "")),
        _DRAFT_SYSTEM, purpose="curator_outreach_draft", max_tokens=900, job_id=job.get("id"))
    if not out:
        return {}
    note = str(out.get("note") or "")[:300]
    subj = str(out.get("email_subject") or "").strip()
    body = str(out.get("email_body") or "").strip()
    return {"note": note, "email_draft": (f"Subject: {subj}\n\n{body}" if subj else body) or None}


def guess_email(name: str, intel: dict | None) -> tuple[str | None, float | None]:
    intel = intel or {}
    first, last = split_name(name)
    email = make_email(first, last, intel.get("email_pattern"), intel.get("domain"))
    return (email, 0.6) if email else (None, None)
