"""Daily curation: from the day's few hundred new jobs to a ranked Top-N worth applying
to today, plus people to contact about them.

Pipeline (each step narrows the pool before the next, more expensive one):
  pool        status=new, posted/seen within curate_window_hours, US or unknown country,
              no application to the company in the last 30 days
  hard drops  title seniority words, JD says no sponsorship, JD asks for 6+ years
  embed       résumé-vs-JD similarity on the local embedding model (seconds)
  LLM score   the top curate_llm_pool by similarity get the usual light fit score
  intel       company research (SearXNG + one LLM extraction, cached 30 days)
  rank        fit / freshness / visa-or-pay / reply odds / stability (scoring.py)
  write       Top-N -> saved + curated_rank; the rest of the pool -> ignored
  outreach    people for the top companies (people.py), Telegram summary
"""
import asyncio
import html
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_

from backend.curator import scoring
from backend.curator.embed import percentiles, similarity
from backend.curator.intel import get_intel, name_key, own_reply_rates
from backend.models.db import Application, Contact, Job, Persona, SessionLocal, Setting, VisaCache
from backend.tz import local_now, local_today

logger = logging.getLogger("jobnavigator.curator")

PER_COMPANY_CAP = 2


def _settings() -> dict:
    db = SessionLocal()
    try:
        rows = {r.key: r.value for r in db.query(Setting).filter(
            or_(Setting.key.like("curate_%"), Setting.key.in_(["target_base_min", "target_base_max", "outreach_count"]))).all()}
    finally:
        db.close()

    def i(k, d):
        try:
            return int(rows.get(k) or d)
        except ValueError:
            return d

    def j(k, d):
        try:
            v = json.loads(rows.get(k) or "null")
            return v if v is not None else d
        except ValueError:
            return d
    return {
        "count": i("curate_count", 50),
        "window_hours": i("curate_window_hours", 36),
        "llm_pool": i("curate_llm_pool", 150),
        "auto_ignore": (rows.get("curate_auto_ignore") or "true").lower() == "true",
        "title_exclude": j("curate_title_exclude", []),
        "weights": j("curate_weights", scoring.DEFAULT_WEIGHTS),
        "target_min": i("target_base_min", 115000),
        "target_max": i("target_base_max", 150000),
        "outreach": i("outreach_count", 20),
    }


def _job_dict(j: Job) -> dict:
    return {
        "id": j.id, "company": j.company or "", "title": j.title or "", "url": j.url or "",
        "description": j.description or "", "posted_at": j.posted_at or j.discovered_at,
        "best_cv_score": j.best_cv_score, "best_cv": j.best_cv,
        "lca": j.h1b_company_lca_count, "approval": j.h1b_company_approval_rate,
        "jd_flag": bool(j.h1b_jd_flag), "salary_min": j.salary_min, "salary_max": j.salary_max,
        "source": j.source, "loc_country": j.loc_country, "loc_region": j.loc_region,
        "loc_city": j.loc_city, "location": j.location or "",
    }


def load_pool(window_hours: int) -> tuple[list[dict], list]:
    """(candidate dicts, ids of every status=new job in the window)."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)
    db = SessionLocal()
    try:
        recent_apps = {name_key(c) for (c,) in db.query(Job.company).join(Application, Application.job_id == Job.id)
                       .filter(Application.applied_at >= now - timedelta(days=30)).all()}
        q = (db.query(Job).filter(Job.status == "new")
             .filter(func.coalesce(Job.posted_at, Job.discovered_at) >= since))
        jobs = q.all()
        all_ids = [j.id for j in jobs]
        pool = [_job_dict(j) for j in jobs
                if (j.loc_country in (None, "", "US")) and name_key(j.company) not in recent_apps]
        return pool, all_ids
    finally:
        db.close()


def hard_drop(job: dict, title_exclude: list) -> str | None:
    if not job["company"].strip():
        return "no company"
    w = scoring.title_excluded(job["title"], title_exclude)
    if w:
        return f"title: {w}"
    if job["jd_flag"]:
        return "no sponsorship"
    blocker = scoring.jd_blocker(job["description"])
    if blocker:
        return f"jd: {blocker}"
    yrs = scoring.min_years_required(job["description"])
    if yrs is not None and yrs >= 6:
        return f"{yrs}+ years"
    return None


async def _score_unscored(jobs: list[dict]) -> None:
    """Light LLM fit score for pool jobs that have none yet; writes back to the rows."""
    from types import SimpleNamespace
    from backend.analyzer.cv_scorer import _get_default_resume, _get_resume_texts, score_job_sync
    todo = [j for j in jobs if j["best_cv_score"] is None and len(j["description"].strip()) > 50]
    if not todo:
        return
    db = SessionLocal()
    try:
        cv_texts = _get_default_resume(db) or _get_resume_texts(db)
    finally:
        db.close()
    if not cv_texts:
        return
    results = await asyncio.gather(*[
        score_job_sync(SimpleNamespace(id=j["id"], company=j["company"], title=j["title"]), cv_texts,
                       depth="light", preloaded_text=j["description"]) for j in todo], return_exceptions=True)
    db = SessionLocal()
    try:
        for j, res in zip(todo, results):
            if isinstance(res, BaseException) or not res or not isinstance(res.get("scores"), dict):
                continue
            nums = [float(v) for v in res["scores"].values() if isinstance(v, (int, float))]
            row = db.get(Job, j["id"])
            if row is None or not nums:
                continue
            row.cv_scores = res["scores"]
            row.best_cv_score = max(nums)
            row.best_cv = res.get("best_cv", "")
            j["best_cv_score"], j["best_cv"] = row.best_cv_score, row.best_cv
        db.commit()
    finally:
        db.close()


def _resume_texts() -> list[str]:
    from backend.analyzer.cv_scorer import _get_default_resume, _get_resume_texts
    db = SessionLocal()
    try:
        return list((_get_default_resume(db) or _get_resume_texts(db)).values())
    finally:
        db.close()


def rank(jobs: list[dict], intel: dict, replies: dict, lca_median: dict, cfg: dict, now: datetime) -> list[dict]:
    year = local_today().year
    for j in jobs:
        k = name_key(j["company"])
        it = intel.get(k) or {}
        hours = scoring.hours_since(j["posted_at"], now)
        base, src = scoring.comp_estimate(j["salary_min"], j["salary_max"], lca_median.get(k),
                                          it.get("est_base_min"), it.get("est_base_max"))
        funded = scoring.well_funded(it.get("funding_stage"), it.get("last_raise_year"), j["lca"], year)
        own_rate, own_n = replies.get(k, (None, 0))
        parts = {
            "fit": scoring.fit_score(j["best_cv_score"], j.get("embed_pct")),
            "fresh": scoring.freshness(hours),
            "visa_comp": scoring.visa_comp_score(scoring.sponsor_score(j["lca"], j["approval"], j["jd_flag"]),
                                                 scoring.comp_score(base, cfg["target_min"], cfg["target_max"]), funded),
            "reply": scoring.reply_score(own_rate, own_n, it.get("reply_reputation"), j["url"]),
            "stability": scoring.stability_score(it.get("funding_stage"), it.get("last_raise_year"),
                                                 it.get("recent_layoffs"), year),
        }
        reply_note = f"you heard back {round(own_rate * 100)}% of {own_n}" if own_n >= 3 else ""
        j["parts"] = {k2: round(v, 3) for k2, v in parts.items()}
        j["score"] = scoring.composite(parts, cfg["weights"])
        j["hours"] = hours
        j["reason"] = scoring.reason(best_cv=j["best_cv"], llm_score=j["best_cv_score"], hours=hours,
                                     lca_count=j["lca"], stage=it.get("funding_stage"),
                                     last_raise_year=it.get("last_raise_year"), base=base, base_src=src,
                                     reply_note=reply_note)
        j["intel"] = {x: it.get(x) for x in ("funding_stage", "last_raise_year", "headcount",
                                             "recent_layoffs", "reply_reputation", "notes")}
        j["funded"] = funded
    return sorted(jobs, key=lambda j: -j["score"])


def pick_top(ranked: list[dict], count: int) -> list[dict]:
    top, per = [], {}
    for j in ranked:
        k = name_key(j["company"])
        if per.get(k, 0) >= PER_COMPANY_CAP:
            continue
        per[k] = per.get(k, 0) + 1
        top.append(j)
        if len(top) >= count:
            break
    return top


def write_results(top: list[dict], pool_ids: list, auto_ignore: bool, dropped: dict) -> int:
    today = local_today()
    top_ids = {j["id"] for j in top}
    db = SessionLocal()
    try:
        for i, j in enumerate(top, 1):
            row = db.get(Job, j["id"])
            if row is None or row.status != "new":
                continue
            row.status, row.saved = "saved", True
            row.curated_on, row.curated_rank = today, i
            row.curation = {"score": j["score"], "parts": j["parts"], "reason": j["reason"],
                            "apply_window": scoring.apply_window(i, j["parts"]["fresh"]), "intel": j["intel"]}
        ignored = 0
        if auto_ignore:
            rest = [i for i in pool_ids if i not in top_ids]
            for chunk in range(0, len(rest), 500):
                for row in db.query(Job).filter(Job.id.in_(rest[chunk:chunk + 500]), Job.status == "new").all():
                    row.status = "ignored"
                    row.curation = {"out": True, "on": today.isoformat(), "why": dropped.get(row.id, "below the cut")}
                    ignored += 1
        db.commit()
        return ignored
    finally:
        db.close()


def _me() -> tuple[str, str]:
    db = SessionLocal()
    try:
        p = db.get(Persona, 1)
        contact = (p.contact if p else None) or {}
        name = " ".join(x for x in (contact.get("first_name"), contact.get("last_name")) if x) or "the candidate"
        from backend.analyzer.cv_scorer import _get_default_resume, _get_resume_texts
        texts = list((_get_default_resume(db) or _get_resume_texts(db)).values())
        return name, (texts[0] if texts else "")
    finally:
        db.close()


async def find_contacts(top: list[dict], intel: dict, want_total: int) -> list[dict]:
    from backend.curator.linkedin_people import LinkedInPeople
    from backend.curator.people import draft, find_people, guess_email
    me, profile = _me()
    today = local_today()
    found, seen_companies = [], set()
    async with LinkedInPeople() as li:
        for j in top:
            if len(found) >= want_total:
                break
            k = name_key(j["company"])
            if k in seen_companies:
                continue
            seen_companies.add(k)
            it = intel.get(k) or {}
            small = (it.get("headcount") or 10**6) < 300 or (it.get("funding_stage") or "") in ("seed", "pre_seed", "series_a", "series_b")
            city = (j.get("loc_city") or "").title() or None
            people = await find_people(j["company"], j["title"], small, li, want=1 if want_total - len(found) <= len(top) else 2,
                                       country=j.get("loc_country") or "US", region=j.get("loc_region"), city=city)
            for p in people:
                email, conf = guess_email(p["name"], it)
                d = await draft(p, j["company"], j, me, profile)
                found.append({"job_id": j["id"], "company": j["company"], "name": p["name"],
                              "title": p.get("headline"), "kind": p.get("kind"), "linkedin_url": p["url"],
                              "location": p.get("location"),
                              "email": email, "email_confidence": conf, "source": p.get("source"),
                              "note": d.get("note"), "email_draft": d.get("email_draft"), "found_on": today})
    db = SessionLocal()
    try:
        saved = []
        for c in found:
            exists = db.query(Contact).filter(Contact.company == c["company"], Contact.linkedin_url == c["linkedin_url"]).first()
            if exists:
                continue
            db.add(Contact(**c))
            saved.append(c)
        db.commit()
        return saved
    finally:
        db.close()


async def notify(top: list[dict], contacts: list[dict]) -> None:
    from backend.notifier.telegram import _get_chat_id, _is_enabled, _send_message
    if not _is_enabled():
        return
    chat = _get_chat_id()
    if not chat:
        return
    e = html.escape
    lines = [f"🎯 <b>Today's Top {len(top)} — {local_now().strftime('%a %b %d')}</b>"]
    for i, j in enumerate(top, 1):
        win = scoring.apply_window(i, j["parts"]["fresh"])
        lines.append(f"{i}. [{win}] <a href=\"{e(j['url'])}\">{e(j['title'][:70])}</a> — {e(j['company'])}\n   <i>{e(j['reason'])}</i>")
    if contacts:
        lines.append(f"\n🤝 <b>{len(contacts)} people to contact</b>")
        for c in contacts:
            bits = [f"<a href=\"{e(c['linkedin_url'])}\">{e(c['name'])}</a>", e((c.get('title') or '')[:60]), e(c['company'])]
            if c.get("email"):
                bits.append(e(c["email"]) + " (guess)")
            lines.append("• " + " · ".join(b for b in bits if b))
    msg, chunks = "", []
    for line in lines:
        if len(msg) + len(line) > 3800:
            chunks.append(msg)
            msg = ""
        msg += line + "\n"
    chunks.append(msg)
    for chunk in chunks:
        await _send_message(chat, chunk)


def _count(values) -> dict:
    out = {}
    for v in values:
        k = v.split(":")[0]
        out[k] = out.get(k, 0) + 1
    return out


async def run_daily_curation(outreach: bool = True, send: bool = True, dry_run: bool = False) -> dict:
    """dry_run: rank and return the Top list without saving/ignoring jobs, finding people or messaging."""
    from backend.curator.llm import reset_run
    reset_run()
    cfg = _settings()
    now = datetime.now(timezone.utc)
    pool, pool_ids = await asyncio.to_thread(load_pool, cfg["window_hours"])
    dropped, keep = {}, []
    for j in pool:
        why = hard_drop(j, cfg["title_exclude"])
        if why:
            dropped[j["id"]] = why
        else:
            keep.append(j)
    logger.info(f"curator: pool {len(pool)} (window {len(pool_ids)}), {len(keep)} after hard drops")

    # embedding pre-filter
    sims = await similarity(_resume_texts(), [f"{j['title']}\n{j['description'][:4000]}" for j in keep]) if keep else None
    if sims:
        for j, p in zip(keep, percentiles(sims)):
            j["embed_pct"] = p
        keep.sort(key=lambda j: -j["embed_pct"])
    else:
        keep.sort(key=lambda j: -(j["best_cv_score"] or 0))
    shortlist = keep[: cfg["llm_pool"]]
    await _score_unscored(shortlist)

    # prelim rank (no intel) to choose which companies to research
    db = SessionLocal()
    try:
        keys = list({name_key(j["company"]) for j in shortlist})
        lca_median = {r.name_key: r.median_salary for r in db.query(VisaCache).filter(VisaCache.name_key.in_(keys)).all() if r.median_salary}
        replies = own_reply_rates(db)
    finally:
        db.close()
    prelim = pick_top(rank(shortlist, {}, replies, lca_median, cfg, now), min(len(shortlist), cfg["count"] * 2))
    intel = await get_intel(list({j["company"] for j in prelim}))
    ranked = rank(shortlist, intel, replies, lca_median, cfg, now)
    top = pick_top(ranked, cfg["count"])
    if dry_run:
        return {"pool": len(pool_ids), "candidates": len(pool), "dropped": len(dropped), "llm_pool": len(shortlist),
                "top": [{"rank": i, "title": j["title"], "company": j["company"], "score": j["score"],
                         "parts": j["parts"], "reason": j["reason"], "url": j["url"]} for i, j in enumerate(top, 1)],
                "drop_reasons": _count(dropped.values())}
    ignored = await asyncio.to_thread(write_results, top, pool_ids, cfg["auto_ignore"], dropped)

    contacts = []
    if outreach and cfg["outreach"] > 0 and top:
        try:
            contacts = await find_contacts(top, intel, cfg["outreach"])
        except Exception as e:
            logger.exception(f"curator outreach failed: {e}")
    if send:
        try:
            await notify(top, contacts)
        except Exception as e:
            logger.warning(f"curator Telegram summary failed: {e}")
    summary = {"pool": len(pool_ids), "candidates": len(pool), "dropped": len(dropped),
               "llm_pool": len(shortlist), "saved": len(top), "ignored": ignored, "contacts": len(contacts)}
    logger.info(f"curator done: {summary}")
    return summary
