"""/api/autoapply/* — queue review, settings, manual triggers."""
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, joinedload

from backend.models.db import get_db
from backend.autoapply import settings as aa_settings
from backend.autoapply.models import AutoApplyItem, STATUSES

logger = logging.getLogger("jobnavigator.autoapply.routes")

router = APIRouter(prefix="/autoapply", tags=["autoapply"])

# Review actions: action -> (statuses it may start from, status it sets).
ACTIONS = {
    "approve": (("queued", "needs_human", "failed", "dry_run_ok"), "approved"),
    "reject": (("queued", "approved", "ready", "needs_human", "failed", "dry_run_ok"), "rejected"),
    "skip": (("queued", "approved", "ready", "needs_human", "failed", "dry_run_ok"), "skipped"),
    "requeue": (("rejected", "skipped", "failed"), "queued"),
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
            "dry_run": cfg["autoapply_dry_run"], "enabled": cfg["autoapply_enabled"]}


@router.post("/items/action")
def item_action(body: dict, db: Session = Depends(get_db)):
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
            it.status = to
            it.error = None if to in ("approved", "queued") else it.error
            changed.append(str(it.id))
        else:
            unchanged.append({"id": str(it.id), "status": it.status})
    db.commit()
    return {"changed": changed, "unchanged": unchanged}


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
