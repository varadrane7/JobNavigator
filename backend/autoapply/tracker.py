"""Write the Applications-tracker row for a submitted item.

Not via upstream's POST /applications: that looks the job up by a recomputed external id and can
create a second Job for a scraped posting. Here the job is known, so the row is written against it
directly, with the same fields and initial funnel edge the upstream route seeds."""
import logging

from backend.models.db import Application, Job, utcnow

logger = logging.getLogger("jobnavigator.autoapply.tracker")


def record_application(db, job_id, resume_name: str | None, submitter: str, item_id) -> Application:
    existing = db.query(Application).filter(Application.job_id == job_id).first()
    if existing:
        return existing
    app = Application(
        job_id=job_id,
        status="applied",
        cv_version_used=resume_name,
        notes=f"Auto-applied via AutoApply ({submitter}), item {item_id}",
        status_transitions=[{"from": None, "to": "applied", "at": utcnow().isoformat(), "source": "autoapply"}],
    )
    db.add(app)
    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        job.status = "applied"
    db.flush()
    try:
        from backend.activity import log_activity
        log_activity("autoapply_submit", f"Auto-applied to {job.title if job else ''} at {job.company if job else ''}",
                     company=job.company if job else None, details={"item_id": str(item_id), "submitter": submitter}, db=db)
    except Exception:
        pass
    return app
