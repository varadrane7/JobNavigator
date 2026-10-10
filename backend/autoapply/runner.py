"""ready -> submitting -> submitted | dry_run_ok | needs_human | skipped | failed, one item per tick.

Nothing is retried on its own: a failure stays failed (with its screenshots) until a person retries it,
and a form whose submit button was clicked is never sent again unattended, because the application
may already be in even when no confirmation was recognised.

Headless Chromium with its own context per item (bypass_csp so the field engine's init script
runs on every page), closed afterwards so a stuck form can never leak a browser."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from backend.models.db import SessionLocal, Job, Resume, CoverLetter
from backend.autoapply import settings as aa_settings
from backend.autoapply import pacing
from backend.autoapply.models import AutoApplyItem, AutoApplyAttempt, AutoApplyQuestion
from backend.autoapply.submitters.registry import classify, site_host

logger = logging.getLogger("jobnavigator.autoapply.runner")

CLICKED_SUBMIT = "clicked submit"     # every submitter logs this step right after the submit click
STALE_SUBMITTING = timedelta(minutes=25)   # longer than one agent run
RETRY_LATER_MIN = 30


def submitter_for(name: str):
    from backend.autoapply.submitters.ats import Greenhouse, Lever, Ashby
    from backend.autoapply.submitters.generic import Generic
    from backend.autoapply.submitters.modal import LinkedInEasyApply, ZipRecruiter
    from backend.autoapply.submitters.workday import Workday
    return {"greenhouse": Greenhouse, "lever": Lever, "ashby": Ashby, "generic": Generic,
            "workday": Workday, "linkedin": LinkedInEasyApply, "ziprecruiter": ZipRecruiter}.get(name)


def _get_login(cfg):
    def get(host):
        from backend.autoapply import credentials
        s = SessionLocal()
        try:
            return credentials.load(s, cfg["autoapply_data_dir"], host)
        finally:
            s.close()
    return get


def _save_login(cfg):
    def save(host, email, password):
        from backend.autoapply import credentials
        s = SessionLocal()
        try:
            credentials.save(s, cfg["autoapply_data_dir"], host, email, password)
        finally:
            s.close()
    return save


def browser_for(submitter: str, cfg: dict):
    """Headless Chromium for hosted ATS forms; the user's own Chrome for sites that need their login."""
    if submitter in pacing.CDP_SITES:
        from backend.autoapply.cdp import CdpBrowser
        return lambda: CdpBrowser(cfg["autoapply_cdp_url"])
    return HeadlessBrowser


class HeadlessBrowser:
    """async with HeadlessBrowser() as page: ..."""

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        from backend.scraper._shared.browser import _STEALTH_ARGS, _USER_AGENT
        from backend.autoapply.submitters.base import page_script
        self.pw = await async_playwright().start()
        try:
            self.browser = await self.pw.chromium.launch(headless=True, args=_STEALTH_ARGS)
            self.ctx = await self.browser.new_context(
                user_agent=_USER_AGENT, viewport={"width": 1280, "height": 900},
                locale="en-US", bypass_csp=True)
            await self.ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            await self.ctx.add_init_script(page_script())
            return await self.ctx.new_page()
        except BaseException:
            await self.pw.stop()
            raise

    async def __aexit__(self, *exc):
        for closer in (getattr(self, "ctx", None), getattr(self, "browser", None)):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        await self.pw.stop()


def _cover_letter_text(db, cl_id) -> str | None:
    cl = db.query(CoverLetter).filter(CoverLetter.id == cl_id).first()
    if not cl:
        return None
    j = cl.json_data or {}
    return "\n\n".join([j.get("greeting", "")] + list(j.get("body_paragraphs") or []) + [j.get("closing", ""), j.get("signature", "")]).strip()


def release_stale(db, now) -> int:
    n = 0
    for it in db.query(AutoApplyItem).filter(AutoApplyItem.status == "submitting").all():
        ts = it.updated_at
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts is None or now - ts > STALE_SUBMITTING:
            # The submit click may have happened before the restart, so a person decides.
            it.status = "needs_human"
            it.error = "interrupted while submitting (restart?); check the site before retrying"
            n += 1
    db.commit()
    return n


def _pause_site(db, cfg, site: str, now) -> None:
    from backend.autoapply import settings as s
    paused = dict(cfg.get("autoapply_paused_until") or {})
    paused[site] = (now + timedelta(hours=int(cfg["autoapply_site_pause_hours"]))).isoformat()
    s.update(db, {"autoapply_paused_until": paused})
    logger.warning(f"AutoApply: {site} paused until {paused[site]}")
    try:
        from backend.activity import log_activity
        log_activity("autoapply_pause", f"AutoApply paused {site} after a checkpoint", details={"until": paused[site]}, db=db)
    except Exception:
        pass


async def submit_item(item_id, browser_factory=None, planner=None, now=None) -> str:
    """Run one item through its submitter. `browser_factory`/`planner` are injectable for tests."""
    from backend.autoapply import docs
    from backend.autoapply.submitters.answers import Planner, load_context
    from backend.autoapply.submitters.base import Packet
    from backend.autoapply.tracker import record_application

    now = now or datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        cfg = aa_settings.get_all(db)
        it = db.query(AutoApplyItem).filter(AutoApplyItem.id == item_id).first()
        if not it or it.status != "ready":
            return "not ready"
        job = db.query(Job).filter(Job.id == it.job_id).first()
        tailored = db.query(Resume).filter(Resume.id == it.tailored_resume_id).first() if it.tailored_resume_id else None
        sub_cls = submitter_for(it.submitter)
        if not job or not it.resume_pdf_path or not sub_cls:
            it.status = "failed"
            it.error = "missing job, résumé PDF, or submitter"
            db.commit()
            return it.error
        it.status = "submitting"
        attempt = AutoApplyAttempt(item_id=it.id, dry_run=bool(cfg["autoapply_dry_run"]))
        db.add(attempt)
        db.commit()
        attempt_id = attempt.id
        snap = dict(company=job.company or "", title=job.title or "", url=it.apply_url or job.url, job_id=job.id,
                    resume_json=dict(tailored.json_data or {}) if tailored else {},
                    tailored_id=str(it.tailored_resume_id) if it.tailored_resume_id else None,
                    resume_pdf=it.resume_pdf_path, cl_pdf=it.cl_pdf_path,
                    cl_text=_cover_letter_text(db, it.cover_letter_id) if it.cover_letter_id else None,
                    submitter=it.submitter, base_name=it.base_resume_name)
    finally:
        db.close()

    folder = docs.item_dir(cfg["autoapply_data_dir"], item_id)
    made = {}

    async def make_cover_letter():
        if cfg["autoapply_cover_letter"] == "off" or not snap["tailored_id"]:
            return None, None
        cl_id, path = await docs.cover_letter(snap["tailored_id"], str(snap["job_id"]), folder)
        made["cl_id"], made["cl_path"] = cl_id, path
        s = SessionLocal()
        try:
            return path, _cover_letter_text(s, cl_id)
        finally:
            s.close()

    packet = Packet(item_id=str(item_id), company=snap["company"], title=snap["title"], url=snap["url"],
                    resume_pdf=snap["resume_pdf"], folder=folder, ctx=None, dry_run=bool(cfg["autoapply_dry_run"]),
                    cover_letter_pdf=snap["cl_pdf"], cover_letter_text=snap["cl_text"],
                    make_cover_letter=make_cover_letter, get_login=_get_login(cfg), save_login=_save_login(cfg))
    try:
        packet.ctx = planner.ctx if planner else load_context(snap["company"], snap["title"], snap["resume_json"])
        if cfg["autoapply_engine"] == "agent" and browser_factory is None and planner is None:
            from backend.autoapply import applier_client
            # The client enforces the agent's own deadline; this is the backstop.
            out = await asyncio.wait_for(applier_client.run(packet, cfg),
                                         timeout=int(cfg["autoapply_agent_timeout_s"]) + 300)
        else:
            plan = planner or Planner(packet.ctx)
            factory = browser_factory or browser_for(snap["submitter"], cfg)
            async with factory() as page:
                out = await asyncio.wait_for(sub_cls().run(page, packet, plan),
                                             timeout=int(cfg["autoapply_submit_timeout_s"]))
    except asyncio.TimeoutError:
        from backend.autoapply.submitters.base import Outcome
        out = Outcome(status="failed", error="timed out")
    except Exception as e:
        from backend.autoapply.submitters.base import Outcome
        logger.error(f"AutoApply submit {item_id} crashed: {e}")
        out = Outcome(status="failed", error=f"crashed: {e}"[:1000])

    db = SessionLocal()
    try:
        it = db.query(AutoApplyItem).filter(AutoApplyItem.id == item_id).first()
        at = db.query(AutoApplyAttempt).filter(AutoApplyAttempt.id == attempt_id).first()
        at.finished_at = datetime.now(timezone.utc)
        at.outcome = out.status
        at.step_log = out.steps
        at.receipt = out.receipt
        at.screenshots = out.screenshots
        at.confirmation_text = out.confirmation_text or out.error
        at.llm_calls = out.llm_calls
        if made.get("cl_id"):
            it.cover_letter_id, it.cl_pdf_path = made["cl_id"], made["cl_path"]
        it.error = out.error
        if out.status == "redirect" and out.redirect_url:
            new_sub = classify(out.redirect_url)
            it.submitter, it.site_host, it.apply_url = new_sub, site_host(out.redirect_url), out.redirect_url
            it.status = "ready"
            it.error = None
            it.flags = list(it.flags or []) + [f"apply flow moved to {new_sub}"]
        elif out.status == "retry":
            # Applier busy, down, or Claude out of quota: not this job's fault, try it again later.
            it.status = "ready"
            it.error = out.error
            it.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=RETRY_LATER_MIN)
        elif out.status == "submitted":
            app = record_application(db, it.job_id, snap["base_name"], it.submitter, it.id)
            it.application_id = app.id
            it.status = "submitted"
            it.submitted_at = datetime.now(timezone.utc)
        elif out.status == "dry_run_ok":
            it.status = "dry_run_ok"
        elif out.status == "already":
            it.status = "skipped"
        elif out.status == "closed":
            it.status = "skipped"
            job = db.query(Job).filter(Job.id == it.job_id).first()
            if job and job.status in (None, "new", "saved"):
                job.status = "skip"
        elif out.status == "failed" and CLICKED_SUBMIT in (out.steps or []):
            # The application may have gone through: park it for a person instead of failing it.
            it.status = "needs_human"
            it.error = (f"submit was clicked but no confirmation was recognised ({out.error}); "
                        "check your email or the site, then Mark applied or Skip")
        elif out.status == "needs_human":
            it.status = "needs_human"
            for q in out.questions:
                db.add(AutoApplyQuestion(item_id=it.id, question=q["question"][:2000], field_type=q.get("field_type"),
                                         options=q.get("options") or []))
        else:
            it.attempts = (it.attempts or 0) + 1
            it.status = "failed"
        if out.pause_site:
            _pause_site(db, cfg, out.pause_site, now)
        db.commit()
        pacing.mark_done(cfg, it, now)
        return f"{snap['company']}: {out.status}" + (f" ({out.error})" if out.error else "")
    finally:
        db.close()


async def tick(now=None) -> str:
    """One pacing-worker tick: maybe submit one item."""
    now = now or datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        cfg = aa_settings.get_all(db)
        if not cfg["autoapply_enabled"]:
            return "disabled"
        release_stale(db, now)
        it, why = pacing.pick_next(db, cfg, now)
        item_id = it.id if it else None
    finally:
        db.close()
    if not item_id:
        return why
    from backend.job_monitor import tracked_run, JobAlreadyRunningError
    try:
        async with tracked_run("autoapply_submit", "scheduler", target_job_id=None) as run:
            run.summary = await submit_item(item_id, now=now)
            return run.summary
    except JobAlreadyRunningError:
        return "already submitting"
