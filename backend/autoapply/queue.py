"""Daily queue: pick today's jobs from what the scrapers found, scored against the chosen base
résumés, ranked, and cut to the global / per-site / per-company caps.

Phased like the upstream scorer: no DB session is held while scoring awaits the LLM."""
import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import exists

from backend.models.db import SessionLocal, Job, Resume
from backend.autoapply import settings as aa_settings
from backend.autoapply.models import AutoApplyItem
from backend.autoapply.submitters.registry import classify, site_host

logger = logging.getLogger("jobnavigator.autoapply.queue")

_COMPANY_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|company|gmbh|plc|sa|ag|bv|holdings?|group|technologies|technology|labs?)\b\.?",
    re.I)


def company_key(name: str | None) -> str:
    """Fold "Acme, Inc." / "ACME Corp" / "acme" to one key for the per-company cap."""
    s = _COMPANY_SUFFIX.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def allowed_resumes(db, cfg: dict) -> dict:
    """{résumé name: id} the queue may choose from: the configured ids, else every base résumé."""
    q = db.query(Resume).filter(Resume.is_base == True)  # noqa: E712
    ids = [str(i) for i in (cfg.get("autoapply_resume_ids") or [])]
    rows = q.all()
    if ids:
        rows = [r for r in rows if str(r.id) in ids]
    return {r.name: r.id for r in rows}


def best_allowed(cv_scores: dict | None, names) -> tuple[str | None, float | None]:
    """Highest numeric score among the allowed résumé names."""
    best_name, best = None, None
    for name in names:
        v = (cv_scores or {}).get(name)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if best is None or v > best:
            best_name, best = name, float(v)
    return best_name, best


def _missing_names(cv_scores: dict | None, names) -> list:
    return [n for n in names if not isinstance((cv_scores or {}).get(n), (int, float))]


def _rule_ok(job, cfg: dict) -> str | None:
    """Submitter name if the job passes the static rules, else None."""
    sub = classify(job.url)
    if not sub or sub not in (cfg.get("autoapply_enabled_submitters") or []):
        return None
    blocked = {c.strip().lower() for c in (cfg.get("autoapply_company_blocklist") or []) if c and c.strip()}
    if (job.company or "").strip().lower() in blocked:
        return None
    title = (job.title or "").lower()
    if any(t.strip().lower() in title for t in (cfg.get("autoapply_title_blocklist") or []) if t and t.strip()):
        return None
    return sub


def _candidates(db, cfg: dict, now: datetime):
    """Jobs still open to us: new/saved, recent, no application, never queued before."""
    cutoff = now - timedelta(days=int(cfg["autoapply_max_age_days"]))
    already = exists().where(AutoApplyItem.job_id == Job.id)
    q = (db.query(Job)
         .filter(Job.status.in_(("new", "saved")))
         .filter(Job.discovered_at >= cutoff)
         .filter(Job.url.isnot(None))
         .filter(~Job.applications.any())
         .filter(~already)
         .order_by(Job.discovered_at.desc()))
    return [j for j in q.all() if _rule_ok(j, cfg)]


async def _default_score(job_id: str, cv_ids: list) -> None:
    from backend.analyzer.cv_scorer import score_single_job
    await score_single_job(job_id, cv_ids=cv_ids, depth="light")


async def build_daily(run_date: date | None = None, score_fn=None, now: datetime | None = None) -> dict:
    """Add today's picks to the queue (idempotent: re-running only tops the day up to its caps)."""
    now = now or datetime.now(timezone.utc)
    run_date = run_date or now.date()
    score_fn = score_fn or _default_score

    # Phase 1: decide who needs scoring, then release the session.
    db = SessionLocal()
    try:
        cfg = aa_settings.get_all(db)
        resumes = allowed_resumes(db, cfg)
        if not resumes:
            return {"queued": 0, "scored": 0, "candidates": 0, "reason": "no base résumés"}
        names = list(resumes)
        cands = _candidates(db, cfg, now)
        to_score = []
        for j in cands:
            missing = _missing_names(j.cv_scores, names)
            if missing:
                to_score.append((str(j.id), [str(resumes[n]) for n in missing]))
        to_score = to_score[: int(cfg["autoapply_score_budget"])]
    finally:
        db.close()

    # Phase 2: score, no session held. Upstream's scoring semaphore bounds concurrency.
    async def _one(job_id, cv_ids):
        try:
            await score_fn(job_id, cv_ids)
            return True
        except Exception as e:
            logger.warning(f"AutoApply: scoring job {job_id} failed: {e}")
            return False
    scored = sum(await asyncio.gather(*(_one(j, ids) for j, ids in to_score))) if to_score else 0

    # Phase 3: rank and cut.
    db = SessionLocal()
    try:
        cfg = aa_settings.get_all(db)
        cands = _candidates(db, cfg, now)
        min_score = float(cfg["autoapply_min_score"])
        # A posting already queued under another board's copy must not come back.
        seen_hash = {h for (h,) in db.query(Job.content_hash)
                     .join(AutoApplyItem, AutoApplyItem.job_id == Job.id)
                     .filter(Job.content_hash.isnot(None)).all()}
        ranked = []
        for j in cands:
            name, score = best_allowed(j.cv_scores, names)
            if score is None or score < min_score:
                continue
            ranked.append((score, j.discovered_at or now, j, name))
        ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)

        today = db.query(AutoApplyItem).filter(AutoApplyItem.run_date == run_date).all()
        site_caps = cfg["autoapply_site_caps"] or {}
        company_cap = int(cfg["autoapply_company_daily_cap"])
        site_n, company_n = {}, {}
        for it in today:
            site_n[it.submitter] = site_n.get(it.submitter, 0) + 1
            company_n[it.company_key] = company_n.get(it.company_key, 0) + 1
        room = int(cfg["autoapply_daily_cap"]) - len(today)
        rank = max((it.rank or 0) for it in today) if today else 0
        status = "queued" if cfg["autoapply_mode"] == "review" else "approved"

        added, capped = 0, 0
        for score, _disc, j, name in ranked:
            if added >= room:
                break
            # Same posting scraped from two boards: keep the higher-ranked copy only.
            if j.content_hash:
                if j.content_hash in seen_hash:
                    continue
                seen_hash.add(j.content_hash)
            sub = classify(j.url)
            ck = company_key(j.company)
            if site_n.get(sub, 0) >= int(site_caps.get(sub, site_caps.get("generic", 0)) or 0):
                capped += 1
                continue
            if company_cap and ck and company_n.get(ck, 0) >= company_cap:
                capped += 1
                continue
            rank += 1
            db.add(AutoApplyItem(
                job_id=j.id, run_date=run_date, rank=rank, score=score,
                base_resume_id=resumes[name], base_resume_name=name,
                submitter=sub, site_host=site_host(j.url), company_key=ck, status=status,
            ))
            site_n[sub] = site_n.get(sub, 0) + 1
            company_n[ck] = company_n.get(ck, 0) + 1
            added += 1
        db.commit()
        result = {"queued": added, "scored": scored, "candidates": len(cands),
                  "eligible": len(ranked), "capped": capped, "run_date": run_date.isoformat()}
    finally:
        db.close()

    try:
        from backend.activity import log_activity
        log_activity("autoapply_queue", f"AutoApply queued {added} job(s) for {run_date.isoformat()}",
                     details=result)
    except Exception:
        pass
    logger.info(f"AutoApply queue built: {result}")
    return result
