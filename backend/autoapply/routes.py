"""/api/autoapply/* — queue review, settings, manual triggers."""
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session, joinedload

from backend.models.db import get_db, Resume
from backend.autoapply import settings as aa_settings
from backend.autoapply import pacing
from backend.autoapply.models import AutoApplyItem, AutoApplyAttempt, AutoApplyQuestion, STATUSES

logger = logging.getLogger("jobnavigator.autoapply.routes")

router = APIRouter(prefix="/autoapply", tags=["autoapply"])

# Review actions: action -> (statuses it may start from, status it sets).
ACTIONS = {
    "approve": (("queued", "failed"), "approved"),
    "reject": (("queued", "approved", "ready", "needs_human", "failed", "dry_run_ok"), "rejected"),
    "skip": (("queued", "approved", "ready", "needs_human", "failed", "dry_run_ok"), "skipped"),
    "requeue": (("rejected", "skipped", "failed"), "queued"),
    # Retry a parked or dry-run item through the submitter (documents are kept).
    "submit": (("dry_run_ok", "needs_human", "failed"), "ready"),
}


def _item_to_dict(it: AutoApplyItem) -> dict:
    j = it.job
    return {
        "id": str(it.id),
        "job_id": str(it.job_id),
        "short_id": getattr(j, "short_id", None),
        "company": getattr(j, "company", None),
        "title": getattr(j, "title", None),
        "url": getattr(j, "url", None),
        "location": getattr(j, "location", None),
        "cv_scores": getattr(j, "cv_scores", None) or {},
        "run_date": it.run_date.isoformat() if it.run_date else None,
        "rank": it.rank,
        "score": it.score,
        "base_resume_id": str(it.base_resume_id) if it.base_resume_id else None,
        "base_resume_name": it.base_resume_name,
        "submitter": it.submitter,
        "site_host": it.site_host,
        "status": it.status,
        "tailored_resume_id": str(it.tailored_resume_id) if it.tailored_resume_id else None,
        "cover_letter_id": str(it.cover_letter_id) if it.cover_letter_id else None,
        "application_id": str(it.application_id) if it.application_id else None,
        "flags": it.flags or [],
        "error": it.error,
        "attempts": it.attempts or 0,
        "submitted_at": it.submitted_at.isoformat() if it.submitted_at else None,
        "updated_at": it.updated_at.isoformat() if it.updated_at else None,
    }


def _parse_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"bad date: {raw}")


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return {"values": aa_settings.get_all(db),
            "spec": {k: {"kind": v[0], "default": v[1], "description": v[2]} for k, v in aa_settings.SPEC.items()}}


@router.put("/settings")
def put_settings(body: dict, db: Session = Depends(get_db)):
    try:
        values = aa_settings.update(db, body or {})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=e.args[0])
    # Re-register (or drop) our cron jobs; this also re-runs upstream's, same as its own PATCH does.
    try:
        from backend.scheduler import configure_scheduler
        configure_scheduler()
    except Exception as e:
        logger.error(f"configure_scheduler after autoapply settings save failed: {e}")
    return {"values": values}


@router.get("/items")
def list_items(run_date: Optional[str] = None, status: Optional[str] = None,
               limit: int = 500, db: Session = Depends(get_db)):
    q = db.query(AutoApplyItem).options(joinedload(AutoApplyItem.job))
    d = _parse_date(run_date)
    if d:
        q = q.filter(AutoApplyItem.run_date == d)
    if status:
        wanted = [s for s in status.split(",") if s]
        bad = [s for s in wanted if s not in STATUSES]
        if bad:
            raise HTTPException(status_code=422, detail=f"unknown status: {', '.join(bad)}")
        q = q.filter(AutoApplyItem.status.in_(wanted))
    rows = (q.order_by(AutoApplyItem.run_date.desc(), AutoApplyItem.rank.asc())
            .limit(max(1, min(limit, 2000))).all())
    return [_item_to_dict(r) for r in rows]


@router.get("/summary")
def summary(run_date: Optional[str] = None, db: Session = Depends(get_db)):
    d = _parse_date(run_date) or datetime.now(timezone.utc).date()
    rows = db.query(AutoApplyItem).filter(AutoApplyItem.run_date == d).all()
    by_status, by_submitter = {}, {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        by_submitter[r.submitter] = by_submitter.get(r.submitter, 0) + 1
    cfg = aa_settings.get_all(db)
    return {"run_date": d.isoformat(), "total": len(rows), "by_status": by_status,
            "by_submitter": by_submitter, "daily_cap": cfg["autoapply_daily_cap"],
            "site_caps": cfg["autoapply_site_caps"], "mode": cfg["autoapply_mode"],
            "dry_run": cfg["autoapply_dry_run"], "enabled": cfg["autoapply_enabled"],
            "paused_sites": {k: v for k, v in (cfg["autoapply_paused_until"] or {}).items()
                             if k in pacing.paused_sites(cfg, datetime.now(timezone.utc))}}


@router.post("/items/action")
async def item_action(body: dict, db: Session = Depends(get_db)):
    action = (body or {}).get("action")
    if action not in ACTIONS:
        raise HTTPException(status_code=422, detail=f"action must be one of {', '.join(ACTIONS)}")
    raw_ids = (body or {}).get("ids") or []
    try:
        ids = [uuid.UUID(str(i)) for i in raw_ids]
    except ValueError:
        raise HTTPException(status_code=422, detail="ids must be UUIDs")
    allowed_from, to = ACTIONS[action]
    changed, unchanged = [], []
    for it in db.query(AutoApplyItem).filter(AutoApplyItem.id.in_(ids)).all():
        if it.status in allowed_from:
            if to == "ready" and not it.resume_pdf_path:
                to_now = "approved"     # never prepared: build documents first
            else:
                to_now = to
            it.status = to_now
            if to_now == "ready":
                it.attempts = 0
                it.next_attempt_at = None
            it.error = None if it.status in ("approved", "queued", "ready") else it.error
            changed.append(str(it.id))
        else:
            unchanged.append({"id": str(it.id), "status": it.status})
    db.commit()
    if changed and any(i.status == "approved" for i in db.query(AutoApplyItem).filter(AutoApplyItem.id.in_(ids)).all()):
        _kick_prepare()
    return {"changed": changed, "unchanged": unchanged}


def _kick_prepare() -> None:
    """Start preparing approved items now rather than at the next worker tick."""
    from backend.job_monitor import launch_background, JobAlreadyRunningError
    from backend.autoapply.prepare import prepare_batch
    try:
        launch_background("autoapply_prepare", prepare_batch, trigger="manual")
    except JobAlreadyRunningError:
        pass   # the running batch, or the next tick, picks the new ones up
    except Exception as e:
        logger.warning(f"AutoApply prepare kick failed: {e}")


def _get_item(db, item_id: str) -> AutoApplyItem:
    try:
        iid = uuid.UUID(item_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="item not found")
    it = db.query(AutoApplyItem).options(joinedload(AutoApplyItem.job)).filter(AutoApplyItem.id == iid).first()
    if not it:
        raise HTTPException(status_code=404, detail="item not found")
    return it


@router.get("/items/{item_id}")
def get_item(item_id: str, db: Session = Depends(get_db)):
    """One item with what changed: base vs tailored summary/skills (Tsenta's "every change shown")."""
    it = _get_item(db, item_id)
    out = _item_to_dict(it)
    base = db.query(Resume).filter(Resume.id == it.base_resume_id).first() if it.base_resume_id else None
    tailored = db.query(Resume).filter(Resume.id == it.tailored_resume_id).first() if it.tailored_resume_id else None
    pick = lambda r: {"summary": (r.json_data or {}).get("summary"), "skills": (r.json_data or {}).get("skills")} if r else None
    out["base"] = pick(base)
    out["tailored"] = pick(tailored)
    out["has_resume_pdf"] = bool(it.resume_pdf_path)
    out["has_cover_letter_pdf"] = bool(it.cl_pdf_path)
    out["attempts_log"] = [{
        "id": str(a.id), "started_at": a.started_at.isoformat() if a.started_at else None,
        "finished_at": a.finished_at.isoformat() if a.finished_at else None,
        "dry_run": a.dry_run, "outcome": a.outcome, "receipt": a.receipt or {},
        "screenshots": len(a.screenshots or []), "confirmation_text": a.confirmation_text,
        "step_log": a.step_log or [],
    } for a in it.attempts_log]
    return out


@router.get("/items/{item_id}/file/{kind}")
def get_item_file(item_id: str, kind: str, db: Session = Depends(get_db)):
    it = _get_item(db, item_id)
    path = {"resume": it.resume_pdf_path, "cover_letter": it.cl_pdf_path}.get(kind)
    if not path:
        raise HTTPException(status_code=404, detail="no such file")
    import os
    data_dir = os.path.realpath(aa_settings.get(db, "autoapply_data_dir"))
    real = os.path.realpath(path)
    if not real.startswith(data_dir + os.sep) or not os.path.isfile(real):
        raise HTTPException(status_code=404, detail="file missing")
    return FileResponse(real, media_type="application/pdf", filename=os.path.basename(real))


@router.post("/items/{item_id}/prepare", status_code=202)
async def reprepare_item(item_id: str, db: Session = Depends(get_db)):
    """Rebuild the documents (e.g. after editing the base résumé): back to approved, then prepare."""
    it = _get_item(db, item_id)
    if it.status not in ("ready", "failed", "needs_human", "dry_run_ok", "approved"):
        raise HTTPException(status_code=409, detail=f"cannot prepare an item that is {it.status}")
    it.status = "approved"
    it.attempts = 0
    it.error = None
    db.commit()
    _kick_prepare()
    return {"status": "approved"}


@router.post("/queue/rebuild", status_code=202)
async def rebuild_queue():
    from backend.job_monitor import launch_background, JobAlreadyRunningError
    from backend.autoapply.queue import build_daily

    async def _run():
        r = await build_daily()
        return f"{r.get('queued', 0)} queued, {r.get('scored', 0)} scored"

    try:
        run_id = launch_background("autoapply_queue", _run, trigger="manual")
    except JobAlreadyRunningError:
        return JSONResponse(status_code=409, content={"detail": "queue build already running"})
    return {"run_id": str(run_id)}


@router.get("/attempts/{attempt_id}/screenshot/{n}")
def get_screenshot(attempt_id: str, n: int, db: Session = Depends(get_db)):
    import os
    try:
        aid = uuid.UUID(attempt_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="attempt not found")
    at = db.query(AutoApplyAttempt).filter(AutoApplyAttempt.id == aid).first()
    shots = (at.screenshots or []) if at else []
    if not (0 <= n < len(shots)):
        raise HTTPException(status_code=404, detail="no such screenshot")
    data_dir = os.path.realpath(aa_settings.get(db, "autoapply_data_dir"))
    real = os.path.realpath(shots[n])
    if not real.startswith(data_dir + os.sep) or not os.path.isfile(real):
        raise HTTPException(status_code=404, detail="file missing")
    return FileResponse(real, media_type="image/png")


def _question_to_dict(q: AutoApplyQuestion) -> dict:
    it = q.item
    return {"id": str(q.id), "item_id": str(q.item_id) if q.item_id else None, "question": q.question,
            "field_type": q.field_type, "options": q.options or [], "suggested_answer": q.suggested_answer,
            "answer": q.answer, "status": q.status,
            "company": it.job.company if it and it.job else None, "title": it.job.title if it and it.job else None}


@router.get("/questions")
def list_questions(status: str = "open", item_id: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(AutoApplyQuestion).filter(AutoApplyQuestion.status == status)
    if item_id:
        try:
            q = q.filter(AutoApplyQuestion.item_id == uuid.UUID(item_id))
        except ValueError:
            raise HTTPException(status_code=422, detail="bad item_id")
    rows = (q
            .order_by(AutoApplyQuestion.created_at.desc()).limit(500).all())
    return [_question_to_dict(q) for q in rows]


@router.post("/questions/{question_id}/answer")
async def answer_question(question_id: str, body: dict, db: Session = Depends(get_db)):
    """Answer a parked question. The answer is saved to the Persona Q&A bank (so every later form,
    and the extension, reuses it), and an item whose questions are all answered goes back to ready."""
    from backend.models.db import Persona
    from sqlalchemy.orm.attributes import flag_modified
    try:
        qid = uuid.UUID(question_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="question not found")
    q = db.query(AutoApplyQuestion).filter(AutoApplyQuestion.id == qid).first()
    if not q:
        raise HTTPException(status_code=404, detail="question not found")
    answer = str((body or {}).get("answer") or "").strip()
    if not answer:
        raise HTTPException(status_code=422, detail="answer is required")
    q.answer, q.status = answer, "answered"
    q.answered_at = datetime.now(timezone.utc)
    if (body or {}).get("save_to_bank", True):
        p = db.query(Persona).filter(Persona.id == 1).first()
        if p is None:
            p = Persona(id=1)
            db.add(p)
        bank = [e for e in (p.qa_bank or []) if not (isinstance(e, dict) and e.get("question") == q.question)]
        bank.append({"question": q.question, "answer": answer})
        p.qa_bank = bank
        flag_modified(p, "qa_bank")
    db.flush()
    requeued = False
    if q.item_id:
        open_left = db.query(AutoApplyQuestion).filter(AutoApplyQuestion.item_id == q.item_id,
                                                       AutoApplyQuestion.status == "open").count()
        it = db.query(AutoApplyItem).filter(AutoApplyItem.id == q.item_id).first()
        if not open_left and it and it.status == "needs_human":
            it.status = "ready" if it.resume_pdf_path else "approved"
            it.error = None
            requeued = True
    db.commit()
    return {"status": "answered", "item_requeued": requeued}


@router.post("/questions/{question_id}/dismiss")
async def dismiss_question(question_id: str, db: Session = Depends(get_db)):
    try:
        qid = uuid.UUID(question_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="question not found")
    q = db.query(AutoApplyQuestion).filter(AutoApplyQuestion.id == qid).first()
    if not q:
        raise HTTPException(status_code=404, detail="question not found")
    q.status = "dismissed"
    db.commit()
    return {"status": "dismissed"}


@router.get("/cdp/status")
async def cdp_status(db: Session = Depends(get_db)):
    """Can the backend reach the user's Chrome, and is it logged in to LinkedIn / ZipRecruiter?"""
    from backend.autoapply.cdp import status
    url = aa_settings.get(db, "autoapply_cdp_url")
    return await status(url)


@router.post("/sites/{site}/resume")
def resume_site(site: str, db: Session = Depends(get_db)):
    """Lift a checkpoint pause early (after you have dealt with the check in your browser)."""
    paused = dict(aa_settings.get(db, "autoapply_paused_until") or {})
    paused.pop(site, None)
    aa_settings.update(db, {"autoapply_paused_until": paused})
    return {"paused_until": paused}
