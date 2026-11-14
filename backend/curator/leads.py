"""Record a person as a lead from outside the curator and the scout: an agent that found
or already contacted someone, a script, the user's own notes.

One function, `log_lead`, behind both doors: the connector tool `add_lead` (connector/tools.py)
and `POST /api/contacts` (curator/routes.py). It is an upsert: the same person sent twice is
one row, so several agents can report the same outreach without making duplicates.
"""
from datetime import date, datetime, time, timezone
from typing import Optional

from sqlalchemy import func

from backend.models.db import Contact, Job, utcnow
from backend.tz import local_today

STATUSES = ("new", "requested", "connected", "messaged", "replied", "referral", "snoozed", "skip")
LEAD_KINDS = ("job", "network", "hiring_post", "project_fit", "alumni", "founder")
ROLES = ("founder", "hiring_manager", "recruiter", "engineer")
NOTE_CHARS = 300          # LinkedIn's limit for a connection note

# How far along the pipeline a status is. An upsert only moves a lead forward: an agent
# that reports "requested" for someone who already replied must not undo the reply.
_RANK = {"skip": 0, "snoozed": 0, "new": 1, "requested": 2, "connected": 3, "messaged": 4, "replied": 5,
         "referral": 6}


class LeadError(ValueError):
    """What was wrong with the lead that was sent, in words for the caller."""


def clean_linkedin_url(url) -> Optional[str]:
    """A profile URL without its query string, fragment or trailing slash; None when empty."""
    text = str(url or "").strip().split("?")[0].split("#")[0].rstrip("/")
    return text or None


def _text(data: dict, key: str) -> Optional[str]:
    value = data.get(key)
    return (str(value).strip() or None) if value is not None else None


def _day(value, what: str) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise LeadError(f"{what} must be a date like 2026-01-31")


def find_lead(db, linkedin_url=None, email=None, name=None, company=None) -> Optional[Contact]:
    """The row for this person, if there is one: by profile URL, else email, else name and company."""
    url = clean_linkedin_url(linkedin_url)
    if url:
        slug = url.lower().split("://")[-1].removeprefix("www.")
        for c in db.query(Contact).filter(func.lower(Contact.linkedin_url).like(f"%{slug}%")).all():
            if clean_linkedin_url(c.linkedin_url).lower().split("://")[-1].removeprefix("www.") == slug:
                return c
    if email:
        c = db.query(Contact).filter(func.lower(Contact.email) == email.lower()).first()
        if c is not None:
            return c
    if name and company:
        return db.query(Contact).filter(func.lower(Contact.name) == name.lower(),
                                        func.lower(Contact.company) == company.lower()).first()
    return None


def log_lead(db, data: dict, default_source: str = "api") -> tuple[Contact, bool]:
    """Add the person in `data`, or update the row they already have. Returns (lead, created).
    Does not commit.

    Keys: name (required), company, title, linkedin_url, email, location,
    role (founder | hiring_manager | recruiter | engineer), kind (LEAD_KINDS),
    status (STATUSES; "contacted" reads as requested), contacted_on and next_action_at (dates),
    why, evidence, talking_points (list), linkedin_note, email_draft, show_project, score (0-100),
    job (a Job row), agent (who is reporting: stored as the lead's source).

    On an existing lead: empty fields are filled, the notes and drafts sent replace the stored
    ones, and the status moves forward only (a skipped or snoozed lead that was contacted
    after all moves to the reported status).
    """
    name = _text(data, "name")
    if not name:
        raise LeadError("name is required")
    status = _text(data, "status")
    status = "requested" if status == "contacted" else status
    if status is not None and status not in STATUSES:
        raise LeadError("status must be one of " + ", ".join(STATUSES))
    kind, role = _text(data, "kind"), _text(data, "role")
    if kind is not None and kind not in LEAD_KINDS:
        raise LeadError("kind must be one of " + ", ".join(LEAD_KINDS))
    if role is not None and role not in ROLES:
        raise LeadError("role must be one of " + ", ".join(ROLES))
    note = _text(data, "linkedin_note")
    if note and len(note) > NOTE_CHARS:
        raise LeadError(f"linkedin_note is {len(note)} characters; LinkedIn takes {NOTE_CHARS}")
    points = data.get("talking_points")
    if points is not None and (not isinstance(points, list) or not all(isinstance(p, str) for p in points)):
        raise LeadError("talking_points must be a list of strings")
    score = data.get("score")
    if score is not None:
        try:
            score = max(0, min(100, int(score)))
        except (TypeError, ValueError):
            raise LeadError("score must be a number from 0 to 100")
    contacted_on = _day(data.get("contacted_on"), "contacted_on")
    next_action_at = _day(data.get("next_action_at"), "next_action_at")
    job: Optional[Job] = data.get("job")
    url, email, company = clean_linkedin_url(data.get("linkedin_url")), _text(data, "email"), _text(data, "company")
    agent = _text(data, "agent")
    source = f"agent:{agent[:40]}" if agent else default_source

    lead = find_lead(db, url, email, name, company)
    created = lead is None
    if created:
        lead = Contact(name=name, status="new", found_on=local_today(), source=source,
                       purpose="job" if job is not None else "network")
        db.add(lead)

    fill = {"company": company, "title": _text(data, "title"), "linkedin_url": url, "email": email,
            "location": _text(data, "location"), "kind": role, "why": _text(data, "why"),
            "evidence": _text(data, "evidence"), "project_ref": _text(data, "show_project"),
            "job_id": job.id if job is not None else None, "score": score,
            "lead_kind": kind or (("job" if job is not None else "network") if created else None)}
    for column, value in fill.items():
        if value is not None and getattr(lead, column) in (None, ""):
            setattr(lead, column, value)
    # what was actually sent is the record; it replaces an earlier draft
    for column, value in (("note", note), ("email_draft", _text(data, "email_draft")), ("talking_points", points)):
        if value is not None:
            setattr(lead, column, value)

    if status is not None and status != lead.status:
        current = lead.status if lead.status in _RANK else "new"
        if _RANK[status] > _RANK[current] or (created and status in ("snoozed", "skip")):
            lead.status = status
            lead.last_action_at = (datetime.combine(contacted_on, time(12), tzinfo=timezone.utc)
                                   if contacted_on else utcnow())
            lead.next_action_at = None     # the scout's follow-up pass sets the next one
    if next_action_at is not None:
        lead.next_action_at = next_action_at
    return lead, created
