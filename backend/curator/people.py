"""Find people to contact about a curated job: founders, hiring managers, recruiters.

Only people who still work at the company (their profile's current employer) and
sit in the job's country are kept; hiring managers for the role's area and
technical / early-career recruiters rank first.

Public search first (SearXNG `site:linkedin.com/in`), LinkedIn's own people search
only when that finds nobody useful, then a best-guess work email from the company's
known email format, and a short LinkedIn note + cold email drafted by the LLM.
"""
import logging
import re

from urllib.parse import urlparse

from backend.analyzer.location import fold
from backend.analyzer.location import parse as parse_location
from backend.curator.intel import make_email
from backend.curator.linkedin_people import LinkedInPeople
from backend.curator.llm import ask_json
from backend.curator.scoring import co_words as _co_words
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


# ── where they are, and whether they still work there ──────────────────────
# Public profile snippets read "Engineering Manager at Ramp · Experience: Ramp ·
# Location: New York City Metropolitan Area". The Experience line is the current
# employer; the profile's country shows in the Location line or, failing that, in
# the LinkedIn subdomain (in.linkedin.com, ch.linkedin.com; www is mostly the US).
_LOCATION = re.compile(r"\b(?:Location|Lokalizacja|Ubicación|Localização|Standort|Lieu|Locatie|Località|Plats)\s*:\s*([^·\n]+)", re.I)
_EXPERIENCE = re.compile(r"\b(?:Experience|Doświadczenie|Experiencia|Experiência|Berufserfahrung|Expérience|Ervaring|Esperienza|Erfarenhet)\s*:\s*([^·\n]+)", re.I)
_LI_SUBDOMAIN = {"uk": "GB"}
_ZIP = re.compile(r"^\d{5}(?:-\d{4})?$")
_FORMER = re.compile(r"\b(?:ex|former(?:ly)?|previously|prev|past|alum\w*|retired)\b[^|·,;/]*$", re.I)


def same_company(a: str, b: str) -> bool:
    """"Stripe, Inc." == "Stripe"; "Ramp Network" != "Ramp"."""
    return bool(a and b) and _co_words(a) == _co_words(b)


def names_company(text: str, company: str) -> bool:
    """True when `text` names `company` as a current employer, not "ex-Ramp" or "formerly at Ramp"."""
    words = _co_words(company)
    if not words or not text:
        return False
    folded = fold(text)
    for m in re.finditer(r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(map(re.escape, words)) + r"(?![a-z0-9])", folded):
        if not _FORMER.search(folded[max(0, m.start() - 30):m.start()]):
            return True
    return False


def profile_country(url: str, location: str | None) -> str | None:
    """ISO country of a profile from its Location text, else its LinkedIn subdomain; None when unknown."""
    loc = (location or "").strip()
    if loc:
        if _ZIP.match(loc):
            return "US"
        country = parse_location(loc).get("country")
        if country:
            return country
    host = (urlparse(url).hostname or "").lower()
    sub = host.split(".")[0] if host.endswith("linkedin.com") else ""
    if len(sub) == 2:
        return _LI_SUBDOMAIN.get(sub, sub.upper())
    return None


def parse_linkedin_hit(r: dict, company: str) -> dict | None:
    """{name, headline, url, location, country, current} from a SearXNG result for a linkedin.com/in
    profile that mentions `company`. `current` is False when the profile shows they left (or never
    worked there: the hit only matched on the snippet)."""
    url = (r.get("url") or "").split("?")[0]
    if "linkedin.com/in/" not in url:
        return None
    title = re.sub(r"\s*\|\s*LinkedIn.*$", "", r.get("title") or "").strip()
    parts = re.split(r"\s+[-–—|]\s+", title, maxsplit=1)
    name, headline = parts[0], (parts[1] if len(parts) > 1 else "")
    snippet = r.get("snippet") or ""
    headline = headline or snippet[:140]
    if not name or not company or not names_company(f"{title} {snippet}", company):
        return None
    exp = _EXPERIENCE.search(snippet)
    loc = _LOCATION.search(snippet)
    location = loc.group(1).strip(" .…") if loc else None
    if exp:
        current = same_company(exp.group(1).strip(" .…"), company)
    else:
        current = names_company(headline, company)
    return {"name": name.strip(), "headline": headline.strip(), "url": url, "location": location,
            "country": profile_country(url, location), "current": current}


def priority(kind: str | None, small_company: bool) -> int:
    if kind == "hiring_manager":
        return 3
    if kind == "founder":
        return 3 if small_company else 1
    if kind == "recruiter":
        return 2
    return 0


# ── what team the role sits on ──────────────────────────────────────────────
# (area, pattern on a job title or a headline, SearXNG query terms, LinkedIn search word)
_AREAS = [
    ("ml", r"machine learning|\bml\b|\bai\b|applied scien|deep learning|\bllm|\bnlp\b|computer vision|perception",
     '("machine learning" OR AI OR ML)', "machine learning"),
    ("data", r"\bdata\b|analytics", "data", "data"),
    ("mobile", r"\bios\b|android|mobile", "(mobile OR iOS OR Android)", "mobile"),
    ("frontend", r"front[- ]?end|\bui\b|\bweb\b", "(frontend OR web)", "frontend"),
    ("infra", r"infra|platform|devops|\bsre\b|site reliability|\bcloud\b|distributed|kubernetes",
     "(infrastructure OR platform)", "infrastructure"),
    ("security", r"secur", "security", "security"),
    ("backend", r"back[- ]?end|\bapi\b|server|payments", "(backend OR platform)", "backend"),
    ("fullstack", r"full[- ]?stack", "", ""),
]
_EARLY = re.compile(r"new grad|graduate|university|entry|junior|early career|\bassociate\b|\b(?:i|1)\b\s*$", re.I)


def job_areas(title: str) -> list[str]:
    return [a for a, rx, _, _ in _AREAS if re.search(rx, title or "", re.I)]


def _area_terms(areas: list[str], li: bool = False) -> str:
    return next((row[3 if li else 2] for row in _AREAS if row[0] in areas and row[2]), "")


def relevance(c: dict, areas: list[str], small_company: bool, job_region: str | None, early: bool) -> int:
    """Ordering score: who they are first, then how close they sit to this role."""
    h = c.get("headline") or ""
    score = priority(c["kind"], small_company) * 10
    if areas and any(re.search(rx, h, re.I) for a, rx, _, _ in _AREAS if a in areas):
        score += 4                                  # manages / hires for this kind of role
    if c["kind"] == "recruiter":
        if re.search(r"technical|tech\b|engineering|software", h, re.I):
            score += 3
        if early and re.search(r"university|campus|early career|emerging talent|new grad", h, re.I):
            score += 3
    if c.get("country"):
        score += 1                                  # confirmed location beats an assumed one
    if job_region and c.get("region") == job_region:
        score += 2
    return score


async def find_people(company: str, job_title: str, small_company: bool, li: LinkedInPeople | None,
                      want: int = 2, country: str = "US", region: str | None = None,
                      city: str | None = None) -> list[dict]:
    """Up to `want` people who work at `company` now, in the job's `country`, best first:
    hiring managers for the role's area, then technical recruiters, founders at small companies."""
    areas = job_areas(job_title)
    early = bool(_EARLY.search(job_title or ""))
    q_co = f'"{company}"'
    queries = []
    if areas and _area_terms(areas):
        queries.append(f'site:linkedin.com/in {q_co} ("engineering manager" OR "head of engineering" OR director) {_area_terms(areas)}')
    queries.append(f'site:linkedin.com/in {q_co} ("engineering manager" OR "head of engineering" OR founder OR CTO)')
    queries.append(f'site:linkedin.com/in {q_co} ("technical recruiter" OR recruiter OR "talent acquisition"'
                   + (' OR "university recruiter" OR "early career"' if early else "") + ")")
    if city:
        queries.append(f'site:linkedin.com/in {q_co} ("engineering manager" OR recruiter) "{city}"')
    li_keywords = [f"{company} engineering manager {_area_terms(areas, li=True)}".strip(), f"{company} technical recruiter"]
    return await _search_people(company, queries, li_keywords, li, areas=areas, small_company=small_company,
                                want=want, country=country, region=region, early=early)


# Who can put someone forward when there is no open role to apply to: the people who
# own a team's headcount. Recruiters are left out; they work open requisitions.
DECISION_MAKERS = {"hiring_manager", "founder"}
_PROFILE_AREA_WORDS = {"ml": "AI", "backend": "backend", "infra": "platform", "data": "data",
                       "mobile": "mobile", "frontend": "frontend", "security": "security"}


def profile_areas(text: str) -> list[str]:
    """The team areas the sender's résumé speaks to, in _AREAS order ("backend", "ml", ...)."""
    return [a for a, rx, _, _ in _AREAS if a != "fullstack" and re.search(rx, text or "", re.I)]


async def find_decision_makers(company: str, areas: list[str], small_company: bool, li: LinkedInPeople | None,
                               want: int = 1, country: str = "US", region: str | None = None,
                               city: str | None = None) -> list[dict]:
    """Up to `want` engineering managers / directors / heads of engineering at `company` (founders and
    CTOs too when it is small) who run the kind of team the sender fits, whether or not it is hiring."""
    q_co = f'"{company}"'
    words = [_PROFILE_AREA_WORDS[a] for a in areas if a in _PROFILE_AREA_WORDS][:4]
    area_or = f"({' OR '.join(words)})" if words else ""
    queries = [f'site:linkedin.com/in {q_co} ("engineering manager" OR "director of engineering" OR "head of engineering") {area_or}'.strip(),
               f'site:linkedin.com/in {q_co} ("VP of engineering" OR "VP engineering" OR CTO'
               + (' OR founder OR "co-founder"' if small_company else "") + ")"]
    if city:
        queries.append(f'site:linkedin.com/in {q_co} ("engineering manager" OR "director of engineering") "{city}"')
    li_keywords = [f"{company} engineering manager {_area_terms(areas, li=True)}".strip(),
                   f"{company} director of engineering"]
    return await _search_people(company, queries, li_keywords, li, areas=areas, small_company=small_company,
                                want=want, country=country, region=region, early=False, kinds=DECISION_MAKERS)


async def _search_people(company: str, queries: list[str], li_keywords: list[str], li: LinkedInPeople | None, *,
                         areas: list[str], small_company: bool, want: int, country: str, region: str | None,
                         early: bool, kinds: set[str] | None = None) -> list[dict]:
    """Run `queries` on SearXNG (then `li_keywords` on LinkedIn when nobody useful turned up) and
    return the best `want` current employees in `country` whose kind is in `kinds` (default: all but engineers)."""
    cands: dict[str, dict] = {}
    rejected = {"left": 0, "country": 0}

    def keep(hit: dict) -> bool:
        if not hit.get("current"):
            rejected["left"] += 1
            return False
        pc = hit.get("country")
        if (pc and pc != country) or (pc is None and country != "US"):
            rejected["country"] += 1
            return False
        if hit.get("location") and pc:
            hit["region"] = parse_location(hit["location"]).get("region")
        return True

    def ranked():
        out = []
        for c in cands.values():
            kind = classify(c["headline"])
            if kind and kind != "engineer" and (kinds is None or kind in kinds):
                c = {**c, "kind": kind}
                out.append({**c, "_p": priority(kind, small_company),
                            "_r": relevance(c, areas, small_company, region, early)})
        return sorted(out, key=lambda c: -c["_r"])

    for q in queries:
        try:
            for r in await search(q, n=10):
                hit = parse_linkedin_hit(r, company)
                if hit and keep(hit):
                    hit["source"] = "searxng"
                    cands.setdefault(hit["url"], hit)
        except SearchError as e:
            logger.warning(f"people search for {company} failed: {e}")
            break
        if len([c for c in ranked() if c["_p"] >= 2]) >= want + 2:
            break

    best = ranked()
    if (not best or best[0]["_p"] < 2) and li is not None:
        for kw in li_keywords:
            for hit in await li.search(kw, country=country):
                if hit.get("current_text"):
                    hit["current"] = names_company(hit["current_text"], company)
                elif hit.get("past_text") and names_company(hit["past_text"], company):
                    hit["current"] = False
                else:
                    hit["current"] = names_company(hit["headline"], company)
                if keep(hit):
                    cands.setdefault(hit["url"], hit)
            best = ranked()
            if best and best[0]["_p"] >= 2:
                break
    if rejected["left"] or rejected["country"]:
        logger.info(f"people at {company}: dropped {rejected['left']} no longer there, "
                    f"{rejected['country']} outside {country}")
    return [{k: v for k, v in c.items() if not k.startswith("_")} for c in best[:want]]


_DRAFT_SYSTEM = ("You write short, specific, human outreach for a job seeker. No flattery, no buzzwords, "
                 "no em dashes. Never invent facts about the sender beyond the profile given, and never invent "
                 "facts about the company beyond what you are told.")

_DRAFT_PROMPT = """Sender: {me}
Sender profile:
{profile}

Target: {name}, {headline} at {company}
Role the sender is applying to: {job_title} ({job_url})

Write JSON:
- "note": LinkedIn connection note, max 290 characters, mentions the role and one concrete overlap: the project or past result from the profile closest to this team
- "email_subject": max 8 words
- "email_body": 80-120 words, plain text: says the sender applied (or is applying today); one line of proof naming the ONE project or past result from the profile that fits this role best, with the project's link if it has one; a soft ask for a quick chat or a referral; sign-off with the sender's first name"""

_NETWORK_PROMPT = """Sender: {me}
Sender profile:
{profile}

Target: {name}, {headline} at {company}
About {company}: {about}

The sender is not applying to a posted role. The target leads engineering people who could
put the sender forward for a role on their team now or when one opens.

Write JSON:
- "note": LinkedIn connection note, max 290 characters: who the sender is in a few words, the project from the profile closest to the target's team, and a request to connect. Do not say the sender applied
- "email_subject": max 8 words
- "email_body": 80-120 words, plain text: one line on why {company} (only from the About line or the target's headline; skip it if neither says anything specific); one line of proof naming the ONE project from the profile closest to the target's team, with its link; the ask: a 15-minute chat about the team, and if a backend or AI role opens there, to be considered or referred; sign-off with the sender's first name"""


def _split_draft(out: dict | None) -> dict:
    if not out:
        return {}
    note = str(out.get("note") or "")[:300]
    subj = str(out.get("email_subject") or "").strip()
    body = str(out.get("email_body") or "").strip()
    return {"note": note, "email_draft": (f"Subject: {subj}\n\n{body}" if subj else body) or None}


async def draft(contact: dict, company: str, job: dict, me: str, profile: str) -> dict:
    out = await ask_json(
        _DRAFT_PROMPT.format(me=me, profile=profile, name=contact["name"], headline=contact.get("headline", ""),
                             company=company, job_title=job.get("title", ""), job_url=job.get("url", "")),
        _DRAFT_SYSTEM, purpose="curator_outreach_draft", max_tokens=900, job_id=job.get("id"))
    return _split_draft(out)


async def draft_network(contact: dict, company: str, intel: dict | None, me: str, profile: str) -> dict:
    intel = intel or {}
    about = " ".join(str(x) for x in (intel.get("notes"),
                                       f"Funding: {intel['funding_stage'].replace('_', ' ')}" if intel.get("funding_stage") not in (None, "", "unknown") else "",
                                       f"About {intel['headcount']} people" if intel.get("headcount") else "") if x) or "nothing known"
    out = await ask_json(
        _NETWORK_PROMPT.format(me=me, profile=profile, name=contact["name"], headline=contact.get("headline", ""),
                               company=company, about=about),
        _DRAFT_SYSTEM, purpose="curator_network_draft", max_tokens=900)
    return _split_draft(out)


def guess_email(name: str, intel: dict | None) -> tuple[str | None, float | None]:
    intel = intel or {}
    first, last = split_name(name)
    email = make_email(first, last, intel.get("email_pattern"), intel.get("domain"))
    return (email, 0.6) if email else (None, None)
