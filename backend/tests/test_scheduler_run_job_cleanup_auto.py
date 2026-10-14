"""Regression test: scheduler.run_job_cleanup_auto must not NameError on JobAlreadyRunningError."""
import pytest
from contextlib import asynccontextmanager


@pytest.mark.asyncio
async def test_run_job_cleanup_auto_handles_already_running(monkeypatch):
    """When tracked_run raises JobAlreadyRunningError, the function should return silently."""
    from backend import scheduler
    from backend.job_monitor import JobAlreadyRunningError

    @asynccontextmanager
    async def raising_tracked_run(*args, **kwargs):
        raise JobAlreadyRunningError("job_cleanup", 0.0)
        yield  # unreachable, but required for contextmanager signature

    # Patch the imported name inside job_monitor (scheduler imports it lazily)
    monkeypatch.setattr("backend.job_monitor.tracked_run", raising_tracked_run, raising=False)

    # must not raise: caught internally, and no NameError from an unbound `db` in `finally: db.close()`
    await scheduler.run_job_cleanup_auto()


@pytest.mark.asyncio
async def test_run_job_cleanup_auto_keeps_skipped_jobs_with_applications(test_db, monkeypatch):
    """A skipped job that was applied to (then rejected) must survive cleanup; deleting it used to
    null applications.job_id (NOT NULL) and roll back the whole batch."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from backend import scheduler
    from backend.models.db import Application, Job, Setting

    @asynccontextmanager
    async def fake_tracked_run(*args, **kwargs):
        yield SimpleNamespace(summary=None)

    monkeypatch.setattr("backend.job_monitor.tracked_run", fake_tracked_run, raising=False)

    old = datetime.now(timezone.utc) - timedelta(days=200)
    test_db.add(Setting(key="job_archive_after_days", value="90"))
    applied = Job(external_id="applied-then-skipped", company="Acme", title="SWE", status="skip", discovered_at=old)
    plain = Job(external_id="just-skipped", company="Acme", title="SWE II", status="skip", discovered_at=old)
    test_db.add_all([applied, plain])
    test_db.flush()
    test_db.add(Application(job_id=applied.id, status="rejected"))
    test_db.commit()
    applied_id, plain_id = applied.id, plain.id

    await scheduler.run_job_cleanup_auto()

    test_db.expire_all()
    assert test_db.get(Job, applied_id) is not None
    assert test_db.get(Job, plain_id) is None
    assert test_db.query(Application).filter(Application.job_id == applied_id).count() == 1
