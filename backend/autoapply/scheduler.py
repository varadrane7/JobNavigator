"""AutoApply's APScheduler jobs. Upstream configure_scheduler() wipes every job with
remove_all_jobs() and then calls register_jobs() as its last line, so ours come back after
each restart and each settings save."""
import logging

from backend.models.db import SessionLocal

logger = logging.getLogger("jobnavigator.autoapply.scheduler")

QUEUE_JOB_ID = "autoapply_queue"


async def run_queue_build(trigger: str = "scheduler") -> str:
    from backend.job_monitor import tracked_run, JobAlreadyRunningError
    from backend.autoapply.queue import build_daily
    try:
        async with tracked_run("autoapply_queue", trigger) as run:
            result = await build_daily()
            run.summary = f"{result.get('queued', 0)} queued, {result.get('scored', 0)} scored"
            await _notify(result)
            return run.summary
    except JobAlreadyRunningError:
        logger.info("AutoApply queue build already running; skipped")
        return "already running"


async def _notify(result: dict) -> None:
    if not result.get("queued"):
        return
    try:
        from backend.notifier.telegram import _send_message, _get_chat_id, _is_enabled
        if _is_enabled():
            await _send_message(_get_chat_id(),
                                f"AutoApply: {result['queued']} job(s) queued for {result.get('run_date')} — review in /autoapply")
    except Exception as e:
        logger.warning(f"AutoApply telegram notify failed: {e}")


def register_jobs(scheduler) -> None:
    """(Re)add AutoApply jobs. Never raises: a bad autoapply setting must not break the
    upstream schedule, which has already been configured when this runs."""
    try:
        from apscheduler.triggers.cron import CronTrigger
        from backend.autoapply import settings as aa_settings
        db = SessionLocal()
        try:
            cfg = aa_settings.get_all(db)
        finally:
            db.close()
        if not cfg["autoapply_enabled"]:
            return
        cron = cfg["autoapply_queue_cron"]
        if cron:
            scheduler.add_job(run_queue_build, CronTrigger.from_crontab(cron),
                              id=QUEUE_JOB_ID, replace_existing=True)
    except Exception as e:
        logger.error(f"AutoApply scheduler registration failed: {e}")
