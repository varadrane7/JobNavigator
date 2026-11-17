"""API for the daily curator: run it, read a day's Top list and contacts, update a contact, test search."""
import logging
from datetime import date
from typing import Annotated, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.accounts.auth import require_owner
from backend.curator import audience as audience_mod
from backend.curator import followups, invites
from backend.job_monitor import JobAlreadyRunningError, launch_background
from backend.models.db import Contact, Job, Resume, get_db
from backend.tz import APP_TZ_NAME, local_today

logger = logging.getLogger("jobnavigator.curator.api")

router = APIRouter(tags=["curator"])


def _summary_text(s: dict) -> str:
    return (f"{s['saved']} saved from {s['candidates']} candidates ({s['dropped']} dropped, "
            f"{s['llm_pool']} ranked), {s['ignored']} ignored, {s['contacts']} contacts, "
            f"{s.get('network', 0)} people to know"
            + (f"; {s['released']} old picks back in the queue" if s.get("released") else "")
            + (f", {s['retired']} put away" if s.get("retired") else "")
            + (f"; {s['resurfaced']} people not yet contacted back on the list" if s.get("resurfaced") else "")
            + (f"; people search errors: {' | '.join(s['outreach_errors'])}"[:600] if s.get("outreach_errors") else ""))


@router.post("/curator/run", status_code=202, summary="Run the daily curation now")
async def run_now(outreach: bool = True, send: bool = True, db: Session = Depends(get_db)):
    from backend.curator.run import run_daily_curation

    async def _do():
        return _summary_text(await run_daily_curation(outreach=outreach, send=send, manual=True))
    try:
        return {"run_id": launch_background("curate", _do, trigger="manual"), "status": "running"}
    except JobAlreadyRunningError as e:
        return JSONResponse(status_code=409, content={"detail": f"{e.job_type} is already running"})


def _job_out(j: Job, resume_ids: dict | None = None, resumes: dict | None = None, general_id: str | None = None,
             tailored: dict | None = None) -> dict:
    """`resumes` is {id: (name, is_base, parent_id)}; best_cv is a résumé id (older rows: a name, or
    "Tailored" for the job's tailored copy, which `tailored` maps from the job id)."""
    c = j.curation or {}
    resumes = resumes or {}
    best = str(j.best_cv) if j.best_cv else None
    if best == "Tailored":
        best_id = (tailored or {}).get(str(j.id))
    else:
        best_id = best if best in resumes else ((resume_ids or {}).get(best) if best else None)
    hit = resumes.get(best_id) if best_id else None
    return {
        "id": str(j.id), "short_id": j.short_id, "rank": j.curated_rank, "title": j.title, "company": j.company,
        "url": j.url, "location": j.location, "status": j.status, "source": j.source,
        "posted_at": (j.posted_at or j.discovered_at).isoformat() if (j.posted_at or j.discovered_at) else None,
        "best_score": j.best_cv_score, "best_cv": hit[0] if hit else j.best_cv,
        "best_cv_id": best_id,
        "salary_min": j.salary_min, "salary_max": j.salary_max,
        "h1b_verdict": j.h1b_verdict, "h1b_lca_count": j.h1b_company_lca_count,
        "score": c.get("score"), "parts": c.get("parts"), "reason": c.get("reason"),
        "apply_window": c.get("apply_window"),
    }


def _contact_out(c: Contact, sched: tuple | None = None, flags: dict | None = None) -> dict:
    """`sched` is followups.schedule(db) (the default 2 follow-ups at days 4 and 9 when left out); `flags` is
    everify.flags_for(db, companies), {name_key: status}."""
    from backend.curator import everify
    fu = followups.info(c, sched=sched)
    return {
        "id": str(c.id), "job_id": str(c.job_id) if c.job_id else None, "company": c.company, "name": c.name,
        "title": c.title, "kind": c.kind, "linkedin_url": c.linkedin_url, "email": c.email,
        "email_confidence": c.email_confidence, "source": c.source, "location": c.location, "note": c.note,
        "email_draft": c.email_draft, "status": c.status, "purpose": c.purpose or "job",
        "found_on": c.found_on.isoformat() if c.found_on else None,
        "lead_kind": c.lead_kind or c.purpose or "job", "degree": c.degree, "score": c.score, "why": c.why,
        "talking_points": c.talking_points or [], "project_ref": c.project_ref, "evidence": c.evidence,
        "next_action_at": c.next_action_at.isoformat() if c.next_action_at else None,
        "last_action_at": c.last_action_at.isoformat() if c.last_action_at else None,
        "invited_on": c.invited_on.isoformat() if c.invited_on else None,
        "note_manual": bool(c.note_manual), "note_issue": c.note_issue,
        "audience": c.audience, "message_stage": c.message_stage, "message_draft": c.message_draft,
        "follow_ups_sent": c.follow_ups_sent or 0, "follow_up": fu, "contacted_on": c.contacted_on.isoformat() if c.contacted_on else None,
        "everify": (flags or {}).get(everify.name_key(c.company), "unknown") if flags is not None else None,
    }


# The lead pipeline (People screen). "contacted" is the old name of "requested".
CONTACT_STATUSES = ("new", "requested", "connected", "messaged", "replied", "referral", "snoozed", "skip")
_SNOOZE_DAYS = 14


def _ctx(db, contacts) -> dict:
    """What _contact_out needs for a batch of contacts, read once: the follow-up schedule and the E-Verify flags."""
    from backend.curator import everify
    return {"sched": followups.schedule(db), "flags": everify.flags_for(db, [c.company for c in contacts])}


@router.get("/curator/today", summary="A day's curated Top list and contacts")
def today(day: Optional[date] = None, db: Session = Depends(get_db)):
    d = day or local_today()
    jobs = (db.query(Job).filter(Job.curated_on == d, Job.curated_rank.isnot(None))
            .order_by(Job.curated_rank).all())
    contacts = db.query(Contact).filter(Contact.found_on == d).order_by(Contact.created_at).all()
    ctx = _ctx(db, contacts)
    # best_cv holds the résumé's name; the cover-letter deep link needs its id
    resume_ids = {"Persona": "persona"}
    resumes, general_id, tailored = {}, None, {}
    for rid, name, is_base, parent_id, job_id in (db.query(Resume.id, Resume.name, Resume.is_base, Resume.parent_id,
                                                           Resume.job_id)
                                                  .order_by(Resume.is_base, Resume.created_at).all()):
        resume_ids[name] = str(rid)      # base résumés last, so they win a name clash
        resumes[str(rid)] = (name, bool(is_base), parent_id)
        if is_base and name.strip().lower() == "general":
            general_id = str(rid)
        if not is_base and job_id:
            tailored[str(job_id)] = str(rid)   # newest copy last, so it wins
    days = [r[0].isoformat() for r in db.query(Job.curated_on).filter(Job.curated_on.isnot(None))
            .distinct().order_by(Job.curated_on.desc()).limit(30).all()]
    return {"date": d.isoformat(), "timezone": APP_TZ_NAME, "days": days,
            "jobs": [_job_out(j, resume_ids, resumes, general_id, tailored) for j in jobs], "contacts": [_contact_out(c, **ctx) for c in contacts]}


class ContactPatch(BaseModel):
    status: Optional[str] = None
    email: Optional[str] = None
    note: Optional[str] = None
    email_draft: Optional[str] = None
    next_action_at: Optional[date] = None
    talking_points: Optional[list[str]] = None
    follow_up_sent: Optional[bool] = None     # true: a follow-up was just sent (counts toward followup_max)


@router.patch("/contacts/{contact_id}", summary="Update a contact (status moves the lead along the pipeline)")
def patch_contact(contact_id: str, body: ContactPatch, db: Session = Depends(get_db)):
    from datetime import timedelta
    from backend.accounts.owned import get_owned_or_404
    from backend.models.db import utcnow
    c = get_owned_or_404(db, Contact, contact_id, "Contact not found")
    data = body.model_dump(exclude_unset=True)
    follow_up_sent = bool(data.pop("follow_up_sent", False))
    if data.get("status") == "contacted":
        data["status"] = "requested"
    if "status" in data and data["status"] not in CONTACT_STATUSES:
        raise HTTPException(422, "status must be one of " + ", ".join(CONTACT_STATUSES))
    if "status" in data and data["status"] != c.status:
        c.last_action_at = utcnow()
        invites.mark_invited(c, data["status"])
        sched = followups.schedule(db)
        c.status = data["status"]
        followups.on_status_change(c, data["status"], sched=sched)
        if "next_action_at" not in data:
            # a step taken clears what was due; snoozing brings it back later; the scout's
            # follow-up pass (curator/scout.py) sets the next one when nothing happens
            data["next_action_at"] = (local_today() + timedelta(days=_SNOOZE_DAYS)
                                      if data["status"] == "snoozed" else None)
    for k, v in data.items():
        setattr(c, k, v)
    if follow_up_sent:          # a nudge that went out; it is not a request, so the invite cap is untouched
        try:
            followups.record_sent(c, followups.schedule(db))
        except ValueError as e:
            raise HTTPException(409, str(e))
    if data.get("note"):        # a note written by hand answers the flag
        c.note_manual, c.note_issue = False, None
    db.commit()
    return {**_contact_out(c, **_ctx(db, [c])), "invites": invites.state(db)}


class ContactIn(BaseModel):
    model_config = {"extra": "forbid"}
    name: str
    company: Optional[str] = None
    title: Optional[str] = None
    linkedin_url: Optional[str] = None
    email: Optional[str] = None
    location: Optional[str] = None
    role: Optional[str] = None
    kind: Optional[str] = None
    status: Optional[str] = None
    contacted_on: Optional[date] = None
    next_action_at: Optional[date] = None
    why: Optional[str] = None
    evidence: Optional[str] = None
    talking_points: Optional[list[str]] = None
    linkedin_note: Optional[str] = None
    email_draft: Optional[str] = None
    show_project: Optional[str] = None
    score: Optional[int] = None
    job_id: Optional[str] = None
    agent: Optional[str] = None
    audience: Optional[str] = None     # alum | ex_colleague | engineer | recruiter | hiring_manager | post


@router.post("/contacts", summary="Add or log a contact (one object or a list)")
def add_contacts(body: Union[ContactIn, list[ContactIn]], db: Session = Depends(get_db)):
    """Record people found or contacted outside the curator: by another agent, a script, by hand.
    The body is one contact or a list of up to 100. Only `name` is required. An upsert: a person
    already on the list (same LinkedIn URL, email, or name and company) is returned with
    `created: false`, their empty fields filled and their status moved forward, never back.
    `status` defaults to `new`; send `requested` or `messaged` with `contacted_on` once the
    message went out, with the text sent as `linkedin_note` (300 characters) or `email_draft`.
    `job_id` is a job's id or dashboard number; `agent` names who is reporting (stored as source).
    Nothing is written when any contact in a list is invalid (422 names it)."""
    from backend.accounts.owned import get_owned
    from backend.curator import leads
    items = body if isinstance(body, list) else [body]
    if not items:
        raise HTTPException(422, "No contacts")
    if len(items) > 100:
        raise HTTPException(422, "At most 100 contacts in one call")
    out = []
    for i, item in enumerate(items):
        data = item.model_dump(exclude_unset=True)
        ref = str(data.pop("job_id", "") or "").strip().lstrip("#")
        if ref:
            job = (db.query(Job).filter(Job.short_id == int(ref)).first() if ref.isdigit()
                   else get_owned(db, Job, ref))
            if job is None:
                db.rollback()
                raise HTTPException(422, f"contact {i} ({item.name}): no job {ref!r}")
            data["job"] = job
        try:
            lead, created = leads.log_lead(db, data, default_source="api")
            db.flush()
        except leads.LeadError as exc:
            db.rollback()
            raise HTTPException(422, f"contact {i} ({item.name}): {exc}")
        out.append((lead, created))
    db.commit()
    inv = invites.state(db)
    ctx = _ctx(db, [lead for lead, _c in out])
    rows = [{**_contact_out(lead, **ctx), "created": created, "invites": inv} for lead, created in out]
    return rows if isinstance(body, list) else rows[0]


@router.get("/contacts", summary="Contacts and leads")
def list_contacts(status: Optional[str] = None, kind: Optional[str] = None, due: bool = False,
                  purpose: Optional[str] = None, limit: Annotated[int, Query(ge=1, le=500)] = 200,
                  db: Session = Depends(get_db)):
    """`due`: leads whose next step is due today or earlier (the People screen's first tab);
    `kind`: lead_kind (hiring_post, project_fit, alumni, job, network)."""
    from sqlalchemy import or_
    plan = invites.plan(db)
    send, carried = set(plan["send"]), set(plan["carried"])
    if due:
        # read-only: follow-ups come due by their own dates (followups.info), so nothing is written to show them.
        # Follow-ups are never cut by the invite cap (it only carries new leads), and carried leads are removed before the limit.
        wanted_status = set(status.split(",")) if status else None
        kinds = set(kind.split(",")) if kind else None
        rows = [c for c in followups.due_leads(db) if c.id not in carried
                and (wanted_status is None or c.status in wanted_status)
                and (kinds is None or (c.lead_kind in kinds or c.purpose in kinds))
                and (not purpose or c.purpose == purpose)][:limit]
    else:
        q = db.query(Contact)
        if status:
            q = q.filter(Contact.status.in_(status.split(",")))
        if kind:
            q = q.filter(or_(Contact.lead_kind.in_(kind.split(",")), Contact.purpose.in_(kind.split(","))))
        if purpose:
            q = q.filter(Contact.purpose == purpose)
        rows = q.order_by(Contact.created_at.desc()).limit(limit).all()
    ctx = _ctx(db, rows)
    return [{**_contact_out(c, **ctx), "send_today": c.id in send, "carried": c.id in carried} for c in rows]


@router.get("/contacts/invites", summary="Connection requests sent today against the daily cap")
def contact_invites(db: Session = Depends(get_db)):
    """sent_today, cap (setting invites_daily_cap, 8 by default, at most 12), remaining, over_cap_by,
    and how many new leads are carried to tomorrow because the cap is used up."""
    plan = invites.plan(db)
    return {k: plan[k] for k in ("sent_today", "cap", "remaining", "over_cap_by")} | {
        "send_today": len(plan["send"]), "carried": len(plan["carried"]),
        "drafts_enabled": __import__("backend.curator.people", fromlist=["drafts_enabled"]).drafts_enabled()}


@router.post("/scout/run", status_code=202, summary="Run the lead scout now")
async def scout_run():
    from backend.curator.scout import run_network_scout, summary_text

    async def _do():
        return summary_text(await run_network_scout())
    try:
        return {"run_id": launch_background("network_scout", _do, trigger="manual"), "status": "running"}
    except JobAlreadyRunningError as e:
        return JSONResponse(status_code=409, content={"detail": f"{e.job_type} is already running"})


@router.post("/nag/send", summary="Send the Telegram nag now (what the nag cron sends)")
async def nag_now():
    from backend.curator.nag import send_nag
    text = await send_nag()
    return {"sent": text is not None, "text": text}


@router.get("/linkedin/usage", summary="Today's LinkedIn requests and cooldowns per session")
def linkedin_usage():
    from backend.curator import li_budget
    return li_budget.status()


@router.post("/persona/networking/sync", summary="Refresh the persona's projects from GitHub")
async def persona_sync(force: bool = False):
    from backend.curator.projects_sync import sync_projects
    return await sync_projects(force=force)


class DraftIn(BaseModel):
    stage: str = "after_acceptance"       # after_acceptance | follow_up


@router.post("/contacts/{contact_id}/draft", summary="Draft the after-acceptance message or the next follow-up for a lead")
async def draft_message(contact_id: str, body: DraftIn, db: Session = Depends(get_db)):
    """Writes the text with the shared drafting prompt for the lead's audience, validates it (at most about 600
    characters after acceptance, 400 for a follow-up) and stores it in message_draft. Nothing is sent. 409 when the
    profile has outreach drafts off, when the lead is not at the right step (after_acceptance needs status
    connected; follow_up needs a follow-up still to send), when there is no earlier sent text for a follow-up to
    refer to, or when the text could not be made to pass the checks (the reason is in the answer; the old draft is cleared)."""
    from backend.accounts.owned import get_owned_or_404
    from backend.curator import drafting, people
    c = get_owned_or_404(db, Contact, contact_id, "Contact not found")
    if body.stage not in ("after_acceptance", "follow_up"):
        raise HTTPException(422, "stage must be after_acceptance or follow_up")
    if not people.drafts_enabled():
        raise HTTPException(409, "Outreach drafts are turned off for this profile")
    kind = "message" if body.stage == "after_acceptance" else "follow_up"
    previous, stage = "", "after_acceptance"
    if kind == "message":
        if c.status != "connected":
            raise HTTPException(409, f"The message after acceptance is for a connected lead; this one is {c.status}")
    else:
        fu = followups.info(c, sched=followups.schedule(db))
        if fu is None or fu.get("closed") or fu.get("waiting"):
            raise HTTPException(409, "There is no follow-up left to send for this lead")
        previous = followups.previous_text(c) or ""
        if not previous:
            raise HTTPException(409, "No earlier sent text is recorded for this lead, so a follow-up has nothing to refer to")
        stage = f"follow_up_{fu['n']}"
    sender = drafting.load_sender(db)
    brief = drafting.Brief(sender=sender, name=c.name,
                           card=drafting.card_text({"headline": c.title, "location": c.location}),
                           evidence=(c.evidence or "")[:600], company=c.company or "", previous=previous,
                           audience=c.audience, from_post=bool(c.from_post) or c.lead_kind == "hiring_post")
    res = await drafting.draft_text(kind, brief, f"outreach_{kind}")
    c.message_stage = stage
    if res.note is None:
        c.message_draft = None          # never leave an older draft looking current
        db.commit()
        raise HTTPException(409, res.issue or "no usable draft")
    c.message_draft = res.note
    db.commit()
    return _contact_out(c, **_ctx(db, [c]))


class EverifyIn(BaseModel):
    company: str
    status: str
    source: str


@router.get("/everify", summary="What is on file about companies and E-Verify")
def everify_list(db: Session = Depends(get_db)):
    from backend.curator import everify
    return everify.listing(db)


@router.put("/everify", summary="Set a company's E-Verify status by hand (yes, no or unknown) with a source note",
            dependencies=[Depends(require_owner)])
def everify_set(body: EverifyIn, db: Session = Depends(get_db)):
    """Shown on jobs and leads only; it never changes a score, a filter or an order."""
    from backend.curator import everify
    try:
        out = everify.set_status(db, body.company, body.status, body.source)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return out
