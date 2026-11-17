"""Find people to contact about a curated job: founders, hiring managers, recruiters.

Only people who still work at the company (their profile's current employer) and
sit in the job's country are kept; hiring managers for the role's area and
technical / early-career recruiters rank first.

LinkedIn's own people search (curator/linkedin_people.py; the US lane goes through curator/company_first.py first),
then a best-guess work email from the company's known email format, and a short LinkedIn note + cold email drafted
by the LLM (curator/drafting.py).
"""
import asyncio
import logging
import re

from urllib.parse import urlparse

from backend.analyzer.location import fold
from backend.analyzer.location import parse as parse_location
from backend.curator.intel import make_email
from backend.curator.linkedin_people import LinkedInPeople
from backend.curator.scoring import co_words as _co_words

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
# The country of a profile shows in its Location text or, failing that, in the LinkedIn subdomain
# (in.linkedin.com, ch.linkedin.com; www is mostly the US).
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


def priority(kind: str | None) -> int:
    if kind == "hiring_manager":
        return 3
    if kind == "founder":
        return 1
    if kind == "recruiter":
        return 2
    return 0


# ── what team the role sits on ──────────────────────────────────────────────
# (area, pattern on a job title or a headline, LinkedIn search word)
_AREAS = [
    ("ml", r"machine learning|\bml\b|\bai\b|applied scien|deep learning|\bllm|\bnlp\b|computer vision|perception",
     "machine learning"),
    ("data", r"\bdata\b|analytics", "data"),
    ("mobile", r"\bios\b|android|mobile", "mobile"),
    ("frontend", r"front[- ]?end|\bui\b|\bweb\b", "frontend"),
    ("infra", r"infra|platform|devops|\bsre\b|site reliability|\bcloud\b|distributed|kubernetes", "infrastructure"),
    ("security", r"secur", "security"),
    ("backend", r"back[- ]?end|\bapi\b|server|payments", "backend"),
    ("fullstack", r"full[- ]?stack", ""),
]
_EARLY = re.compile(r"new grad|graduate|university|entry|junior|early career|\bassociate\b|\b(?:i|1)\b\s*$", re.I)


def job_areas(title: str) -> list[str]:
    return [a for a, rx, _ in _AREAS if re.search(rx, title or "", re.I)]


def _area_terms(areas: list[str]) -> str:
    return next((row[2] for row in _AREAS if row[0] in areas and row[2]), "")


def relevance(c: dict, areas: list[str], job_region: str | None, early: bool) -> int:
    """Ordering score: who they are first, then how close they sit to this role."""
    h = c.get("headline") or ""
    score = priority(c["kind"]) * 10
    if areas and any(re.search(rx, h, re.I) for a, rx, _ in _AREAS if a in areas):
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


async def find_people(company: str, job_title: str, li: LinkedInPeople | None,
                      want: int = 2, country: str = "US", region: str | None = None,
                      city: str | None = None) -> list[dict]:
    """Up to `want` people who work at `company` now, in the job's `country`, best first:
    hiring managers for the role's area, then technical recruiters, founders."""
    areas = job_areas(job_title)
    early = bool(_EARLY.search(job_title or ""))
    li_keywords = [f"{company} engineering manager {_area_terms(areas)}".strip(), f"{company} technical recruiter"]
    return await _search_people(company, li_keywords, li, areas=areas, want=want, country=country, region=region,
                                early=early)


# Who can put someone forward when there is no open role to apply to: the people who
# own a team's headcount. Recruiters are left out; they work open requisitions.
DECISION_MAKERS = {"hiring_manager", "founder"}


def profile_areas(text: str) -> list[str]:
    """The team areas the sender's résumé speaks to, in _AREAS order ("backend", "ml", ...)."""
    return [a for a, rx, _ in _AREAS if a != "fullstack" and re.search(rx, text or "", re.I)]


async def find_decision_makers(company: str, areas: list[str], li: LinkedInPeople | None,
                               want: int = 1, country: str = "US", region: str | None = None,
                               city: str | None = None) -> list[dict]:
    """Up to `want` engineering managers / directors / heads of engineering (founders and CTOs too) at
    `company` who run the kind of team the sender fits, whether or not it is hiring."""
    li_keywords = [f"{company} engineering manager {_area_terms(areas)}".strip(),
                   f"{company} director of engineering"]
    return await _search_people(company, li_keywords, li, areas=areas, want=want, country=country,
                                region=region, early=False, kinds=DECISION_MAKERS)


async def _search_people(company: str, li_keywords: list[str], li: LinkedInPeople | None, *,
                         areas: list[str], want: int, country: str, region: str | None,
                         early: bool, kinds: set[str] | None = None) -> list[dict]:
    """Run `li_keywords` on LinkedIn until somebody useful turns up and return the best `want` current
    employees in `country` whose kind is in `kinds` (default: all but engineers)."""
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
                out.append({**c, "_p": priority(kind), "_r": relevance(c, areas, region, early)})
        return sorted(out, key=lambda c: -c["_r"])

    best = ranked()
    if li is not None:
        from backend.curator import names as _names
        li.wanted_ids.add(_names.company_key(company))   # the page this loads may carry the company's LinkedIn id
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


def drafts_enabled() -> bool:
    """Whether this profile has LLM-drafted outreach (connection notes, cold emails, talking points) turned on.
    Per-account setting `outreach_drafts_enabled`, default on. Off skips the drafting only: people are still
    found and stored, and nothing already drafted is touched."""
    from backend.accounts.settings import get_setting
    from backend.models.db import SessionLocal
    db = SessionLocal()
    try:
        raw = get_setting(db, "outreach_drafts_enabled")
    except Exception as e:     # an unreadable setting never costs the run: draft as before
        logger.warning(f"outreach_drafts_enabled unreadable, drafting: {e}")
        raw = None
    finally:
        db.close()
    return str(raw if raw is not None else "true").strip().lower() not in ("false", "0", "no", "off")


def _brief(contact: dict, company: str, job_title: str, sender, applied: bool, aff=None):
    """What a draft for `contact` may draw on (curator/drafting.py): the sender facts, their card text, the
    evidence snippet, the job title and the company. The audience is the contact's own, else read from the card."""
    from backend.curator import audience as aud
    from backend.curator import drafting
    card = drafting.card_text(contact)
    who = contact.get("audience") or aud.detect(contact.get("headline") or "", card, aff,
                                                from_post=bool(contact.get("from_post")))
    return drafting.Brief(sender=sender, name=contact["name"], card=card, evidence=contact.get("evidence") or "",
                          company=company, job_title=job_title, audience=who, applied=applied,
                          from_post=bool(contact.get("from_post")))


async def _drafted(brief, purpose: str, job_id=None) -> dict:
    """The connection note and the cold email for `brief`, each validated (curator/note_check.py), retried once
    and flagged for a hand-written one when it still fails. {"note", "email_draft", "note_manual", "note_issue"}."""
    from backend.curator import drafting
    note = await drafting.draft_text("note", brief, purpose, job_id=job_id, max_tokens=500)
    email = await drafting.draft_text("email", brief, purpose + "_email", job_id=job_id, max_tokens=900)
    if email.note is None:
        logger.info(f"email draft for {brief.name} not saved: {email.issue}")
    return {"note": note.note, "email_draft": email.note, "note_manual": note.manual, "note_issue": note.issue}


async def draft(contact: dict, company: str, job: dict, sender, enabled: bool | None = None, aff=None) -> dict:
    """Note and email for a person found about `job`. `sender` is the run's drafting.Sender."""
    if not (drafts_enabled() if enabled is None else enabled):
        return {"note": None, "email_draft": None}
    from backend.curator import note_check
    applied = await asyncio.to_thread(note_check.job_applied, job.get("id"))
    return await _drafted(_brief(contact, company, job.get("title", ""), sender, applied, aff), "curator_outreach_draft",
                          job.get("id"))


async def draft_network(contact: dict, company: str, sender, enabled: bool | None = None, aff=None) -> dict:
    """Note and email for a decision maker at a company with no posting for the sender; no job, never "applied"."""
    if not (drafts_enabled() if enabled is None else enabled):
        return {"note": None, "email_draft": None}
    return await _drafted(_brief(contact, company, "", sender, False, aff), "curator_network_draft")


def guess_email(name: str, intel: dict | None) -> tuple[str | None, float | None]:
    intel = intel or {}
    first, last = split_name(name)
    email = make_email(first, last, intel.get("email_pattern"), intel.get("domain"))
    return (email, 0.6) if email else (None, None)
