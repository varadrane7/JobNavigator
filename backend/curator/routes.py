"""API for the daily curator: run it, read a day's Top list and contacts, update a contact, test search."""
import logging
from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.job_monitor import JobAlreadyRunningError, launch_background
from backend.models.db import CompanyIntel, Contact, Job, Resume, get_db
from backend.tz import APP_TZ_NAME, local_today

logger = logging.getLogger("jobnavigator.curator.api")

router = APIRouter(tags=["curator"])


def _summary_text(s: dict) -> str:
    return (f"{s['saved']} saved from {s['candidates']} candidates ({s['dropped']} dropped, "
            f"{s['llm_pool']} ranked), {s['ignored']} ignored, {s['contacts']} contacts, "
            f"{s.get('network', 0)} people to know"
            + (f"; people search errors: {' | '.join(s['outreach_errors'])}"[:600] if s.get("outreach_errors") else ""))


@router.post("/curator/run", status_code=202, summary="Run the daily curation now")
async def run_now(outreach: bool = True, send: bool = True, db: Session = Depends(get_db)):
    from backend.curator.run import run_daily_curation

    async def _do():
        return _summary_text(await run_daily_curation(outreach=outreach, send=send))
    try:
        return {"run_id": launch_background("curate", _do, trigger="manual"), "status": "running"}
    except JobAlreadyRunningError as e:
        return JSONResponse(status_code=409, content={"detail": f"{e.job_type} is already running"})


def _job_out(j: Job, resume_ids: dict | None = None) -> dict:
    c = j.curation or {}
    return {
        "id": str(j.id), "short_id": j.short_id, "rank": j.curated_rank, "title": j.title, "company": j.company,
        "url": j.url, "location": j.location, "status": j.status, "source": j.source,
        "posted_at": (j.posted_at or j.discovered_at).isoformat() if (j.posted_at or j.discovered_at) else None,
        "best_score": j.best_cv_score, "best_cv": j.best_cv,
        "best_cv_id": (resume_ids or {}).get(j.best_cv) if j.best_cv else None,
        "salary_min": j.salary_min, "salary_max": j.salary_max,
        "h1b_verdict": j.h1b_verdict, "h1b_lca_count": j.h1b_company_lca_count,
        "score": c.get("score"), "parts": c.get("parts"), "reason": c.get("reason"),
        "apply_window": c.get("apply_window"), "intel": c.get("intel"),
    }


def _contact_out(c: Contact) -> dict:
    return {
        "id": str(c.id), "job_id": str(c.job_id) if c.job_id else None, "company": c.company, "name": c.name,
        "title": c.title, "kind": c.kind, "linkedin_url": c.linkedin_url, "email": c.email,
        "email_confidence": c.email_confidence, "source": c.source, "location": c.location, "note": c.note,
        "email_draft": c.email_draft, "status": c.status, "purpose": c.purpose or "job",
        "found_on": c.found_on.isoformat() if c.found_on else None,
    }


@router.get("/curator/today", summary="A day's curated Top list and contacts")
def today(day: Optional[date] = None, db: Session = Depends(get_db)):
    d = day or local_today()
    jobs = (db.query(Job).filter(Job.curated_on == d, Job.curated_rank.isnot(None))
            .order_by(Job.curated_rank).all())
    contacts = db.query(Contact).filter(Contact.found_on == d).order_by(Contact.created_at).all()
    # a network contact has no job card next to it, so it carries the company's one-line research note
    keys = {(c.company or "").strip().lower() for c in contacts if c.purpose == "network"}
    notes = {r.name_key: r.notes for r in db.query(CompanyIntel).filter(CompanyIntel.name_key.in_(keys)).all()} if keys else {}
    # best_cv holds the résumé's name; the cover-letter deep link needs its id
    resume_ids = {"Persona": "persona"}
    for rid, name, _ in db.query(Resume.id, Resume.name, Resume.is_base).order_by(Resume.is_base).all():
        resume_ids[name] = str(rid)      # base résumés last, so they win a name clash
    days = [r[0].isoformat() for r in db.query(Job.curated_on).filter(Job.curated_on.isnot(None))
            .distinct().order_by(Job.curated_on.desc()).limit(30).all()]
    return {"date": d.isoformat(), "timezone": APP_TZ_NAME, "days": days,
            "jobs": [_job_out(j, resume_ids) for j in jobs], "contacts": [{**_contact_out(c), "company_note": notes.get((c.company or "").strip().lower()) or None}
                         for c in contacts]}


class ContactPatch(BaseModel):
    status: Optional[str] = None
    email: Optional[str] = None
    note: Optional[str] = None
    email_draft: Optional[str] = None


@router.patch("/contacts/{contact_id}", summary="Update a contact")
def patch_contact(contact_id: str, body: ContactPatch, db: Session = Depends(get_db)):
    from backend.accounts.owned import get_owned_or_404
    c = get_owned_or_404(db, Contact, contact_id, "Contact not found")
    data = body.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in ("new", "contacted", "replied", "skip"):
        raise HTTPException(422, "status must be new, contacted, replied or skip")
    for k, v in data.items():
        setattr(c, k, v)
    db.commit()
    return _contact_out(c)


@router.get("/contacts", summary="Contacts, newest first")
def list_contacts(status: Optional[str] = None, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                  db: Session = Depends(get_db)):
    q = db.query(Contact)
    if status:
        q = q.filter(Contact.status == status)
    return [_contact_out(c) for c in q.order_by(Contact.created_at.desc()).limit(limit).all()]


@router.get("/search/test", summary="Run one SearXNG query (checks the search service)")
async def search_test(q: str = "software engineer jobs"):
    from backend.search.searxng import SearchError, search
    try:
        results = await search(q, n=5)
    except SearchError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "count": len(results), "results": results}
