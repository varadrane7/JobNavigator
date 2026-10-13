"""API for the daily curator: run it, read a day's Top list and contacts, update a contact, test search."""
import logging
from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.job_monitor import JobAlreadyRunningError, launch_background
from backend.models.db import Contact, Job, get_db
from backend.tz import APP_TZ_NAME, local_today

logger = logging.getLogger("jobnavigator.curator.api")

router = APIRouter(tags=["curator"])


def _summary_text(s: dict) -> str:
    return (f"{s['saved']} saved from {s['candidates']} candidates ({s['dropped']} dropped, "
            f"{s['llm_pool']} ranked), {s['ignored']} ignored, {s['contacts']} contacts")


@router.post("/curator/run", status_code=202, summary="Run the daily curation now")
async def run_now(outreach: bool = True, send: bool = True):
    from backend.curator.run import run_daily_curation

    async def _do():
        return _summary_text(await run_daily_curation(outreach=outreach, send=send))
    try:
        return {"run_id": launch_background("curate", _do, trigger="manual"), "status": "running"}
    except JobAlreadyRunningError as e:
        return JSONResponse(status_code=409, content={"detail": f"{e.job_type} is already running"})


def _job_out(j: Job) -> dict:
    c = j.curation or {}
    return {
        "id": str(j.id), "short_id": j.short_id, "rank": j.curated_rank, "title": j.title, "company": j.company,
        "url": j.url, "location": j.location, "status": j.status, "source": j.source,
        "posted_at": (j.posted_at or j.discovered_at).isoformat() if (j.posted_at or j.discovered_at) else None,
        "best_score": j.best_cv_score, "best_cv": j.best_cv,
        "salary_min": j.salary_min, "salary_max": j.salary_max,
        "h1b_verdict": j.h1b_verdict, "h1b_lca_count": j.h1b_company_lca_count,
        "score": c.get("score"), "parts": c.get("parts"), "reason": c.get("reason"),
        "apply_window": c.get("apply_window"), "intel": c.get("intel"),
    }


def _contact_out(c: Contact) -> dict:
    return {
        "id": str(c.id), "job_id": str(c.job_id) if c.job_id else None, "company": c.company, "name": c.name,
        "title": c.title, "kind": c.kind, "linkedin_url": c.linkedin_url, "email": c.email,
        "email_confidence": c.email_confidence, "source": c.source, "note": c.note,
        "email_draft": c.email_draft, "status": c.status,
        "found_on": c.found_on.isoformat() if c.found_on else None,
    }


@router.get("/curator/today", summary="A day's curated Top list and contacts")
def today(day: Optional[date] = None, db: Session = Depends(get_db)):
    d = day or local_today()
    jobs = (db.query(Job).filter(Job.curated_on == d, Job.curated_rank.isnot(None))
            .order_by(Job.curated_rank).all())
    contacts = db.query(Contact).filter(Contact.found_on == d).order_by(Contact.created_at).all()
    days = [r[0].isoformat() for r in db.query(Job.curated_on).filter(Job.curated_on.isnot(None))
            .distinct().order_by(Job.curated_on.desc()).limit(30).all()]
    return {"date": d.isoformat(), "timezone": APP_TZ_NAME, "days": days,
            "jobs": [_job_out(j) for j in jobs], "contacts": [_contact_out(c) for c in contacts]}


class ContactPatch(BaseModel):
    status: Optional[str] = None
    email: Optional[str] = None
    note: Optional[str] = None
    email_draft: Optional[str] = None


@router.patch("/contacts/{contact_id}", summary="Update a contact")
def patch_contact(contact_id: str, body: ContactPatch, db: Session = Depends(get_db)):
    import uuid
    try:
        c = db.get(Contact, uuid.UUID(contact_id))
    except ValueError:
        c = None
    if c is None:
        raise HTTPException(404, "Contact not found")
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
