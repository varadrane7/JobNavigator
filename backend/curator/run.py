"""Daily curation: from the day's few hundred new jobs to a ranked Top-N worth applying
to today, plus people to contact about them.

Pipeline (each step narrows the pool before the next, more expensive one):
  pool        status=new, posted/seen within curate_window_hours (7 days for companies on the
              Companies list), US or unknown country, no application to the company in 30 days
  hard drops  staffing / gig companies and titles, title seniority words, JD says no
              sponsorship, JD asks for 6+ years
  embed       résumé-vs-JD similarity on the local embedding model (seconds)
  LLM score   the top curate_llm_pool by similarity get the usual light fit score
  intel       company research (SearXNG + one LLM extraction, cached 30 days)
  rank        fit / freshness / visa-or-pay / reply odds / stability / tracked (scoring.py),
              one copy of a posting seen on two boards
  write       Top-N -> saved + curated_rank; the rest of the pool -> ignored
  release     a Top pick left unapplied for curate_release_hours goes back to the pool (it may be
              picked again until it is 30 days old or released 3 times), and people never contacted
              come back to the Today list in place of some of the day's new finds
  outreach    people for the top companies, then decision makers at good companies with
              no open role for the user (people.py); a one-line Telegram summary
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_

from backend.curator import scoring
from backend.curator.embed import percentiles, similarity
from backend.curator.intel import get_intel, name_key, own_reply_rates
from backend.models.db import Application, Company, Contact, Job, Persona, SessionLocal, Setting, VisaCache
from backend.scraper._shared.link_kinds import job_link
from backend.tz import local_now, local_today

logger = logging.getLogger("jobnavigator.curator")

PER_COMPANY_CAP = 2
TRACKED_WINDOW_HOURS = 7 * 24
RELEASE_MAX_AGE_DAYS = 30     # a released job older than this is never picked again
RELEASE_MAX_TIMES = 3         # a job or person passed over this many times is put away


def _settings() -> dict:
    """The current account's curator settings (own or inherited per section, MT-9)."""
    from backend.accounts.settings import get_setting
    keys = ("curate_count", "curate_window_hours", "curate_llm_pool", "curate_auto_ignore", "curate_title_exclude",
            "curate_company_exclude", "curate_weights", "target_base_min", "target_base_max", "outreach_count",
            "outreach_network_count", "curate_max_years", "curate_drop_unlikely_sponsors", "curate_release_hours")
    db = SessionLocal()
    try:
        rows = {k: get_setting(db, k) for k in keys}
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
        "company_exclude": j("curate_company_exclude", scoring.DEFAULT_COMPANY_EXCLUDE),
        "weights": j("curate_weights", scoring.DEFAULT_WEIGHTS),
        "target_min": i("target_base_min", 115000),
        "target_max": i("target_base_max", 150000),
        "outreach": i("outreach_count", 20),
        "network": i("outreach_network_count", 10),
        "max_years": i("curate_max_years", 5),
        "drop_unlikely": (rows.get("curate_drop_unlikely_sponsors") or "true").lower() == "true",
        "release_hours": i("curate_release_hours", 72),
    }


def _release_cutoff(today, hours: int):
    """Picks made on or before this date have waited at least `hours` (picks are dated, runs are daily)."""
    return today - timedelta(days=max(1, -(-hours // 24)))


def release_stale_jobs(db, today, hours: int) -> dict:
    """Top picks still unapplied after `hours` go back to status new, so a later run can pick them again.
    A job released RELEASE_MAX_TIMES times is ignored instead. Returns {released, retired}."""
    out = {"released": 0, "retired": 0}
    if hours <= 0:
        return out
    applied = db.query(Application.job_id).filter(Application.job_id.isnot(None))
    rows = (db.query(Job).filter(Job.status == "saved", Job.curated_on.isnot(None),
                                 Job.curated_on <= _release_cutoff(today, hours), ~Job.id.in_(applied)).all())
    for row in rows:
        times = int((row.curation or {}).get("released") or 0) + 1
        row.curated_on = row.curated_rank = None
        row.saved = False
        if times >= RELEASE_MAX_TIMES:
            row.status = "ignored"
            row.curation = {"out": True, "on": today.isoformat(), "released": times,
                            "why": f"picked {times} times, never applied"}
            out["retired"] += 1
        else:
            row.status = "new"
            row.curation = {"released": times, "released_on": today.isoformat()}
            out["released"] += 1
    db.commit()
    return out


def resurface_contacts(db, today, hours: int, want: int, top_companies: list[str]) -> list[dict]:
    """Up to `want` people from the curator's own lists (job and network contacts) never contacted after
    `hours`, moved onto today's list: people at today's Top companies first, then the most recent.
    Someone passed over RELEASE_MAX_TIMES times is skipped instead."""
    if hours <= 0:
        return []
    stale = (db.query(Contact).filter(Contact.status == "new", Contact.lead_kind.in_(("job", "network")),
                                      Contact.found_on <= _release_cutoff(today, hours))
             .order_by(Contact.found_on.desc(), Contact.created_at.desc()).all())
    for c in stale:
        if (c.resurfaced or 0) >= RELEASE_MAX_TIMES:
            c.status = "skip"
    stale = [c for c in stale if c.status == "new"]
    hot = {name_key(n) for n in top_companies}
    stale.sort(key=lambda c: name_key(c.company or "") not in hot)   # stable: recency within each group
    out = []
    for c in stale[:max(0, want)]:
        c.found_on = today
        c.resurfaced = (c.resurfaced or 0) + 1
        out.append({"company": c.company, "name": c.name, "purpose": c.purpose})
    db.commit()
    return out


def _job_dict(j: Job) -> dict:
    return {
        "id": j.id, "company": j.company or "", "title": j.title or "", "url": job_link(j) or "",
        "description": j.description or "", "posted_at": j.posted_at or j.discovered_at,
        "best_cv_score": j.best_cv_score, "best_cv": j.best_cv,
        "lca": j.h1b_company_lca_count, "approval": j.h1b_company_approval_rate,
        "jd_flag": bool(j.h1b_jd_flag), "salary_min": j.salary_min, "salary_max": j.salary_max,
        "source": j.source, "loc_country": j.loc_country, "loc_region": j.loc_region,
        "loc_city": j.loc_city, "location": j.location or "", "tracked": False, "h1b_verdict": j.h1b_verdict,
    }


def tracked_keys(db) -> set[str]:
    """name_key of every active company on the Companies list, aliases included."""
    keys = set()
    for name, aliases in db.query(Company.name, Company.aliases).filter(Company.active == True).all():  # noqa: E712
        keys.add(name_key(name))
        keys.update(name_key(a) for a in (aliases or []) if isinstance(a, str))
    keys.discard("")
    return keys


def load_pool(window_hours: int) -> tuple[list[dict], list]:
    """(candidate dicts, ids of every status=new job in the window).

    A company on the Companies list gets a 7-day window: its career page is scraped on its own
    schedule and rarely says when a job was posted, so its jobs used to age out unseen."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)
    since_tracked = now - timedelta(hours=max(window_hours, TRACKED_WINDOW_HOURS))
    db = SessionLocal()
    try:
        recent_apps = {name_key(c) for (c,) in db.query(Job.company).join(Application, Application.job_id == Job.id)
                       .filter(Application.applied_at >= now - timedelta(days=30)).all()}
        tracked = tracked_keys(db)
        # a closed posting, or a copy of one already listed (dedup_copy_* = new), is never picked
        oldest = min(since_tracked, now - timedelta(days=RELEASE_MAX_AGE_DAYS))
        q = (db.query(Job).filter(Job.status == "new")
             .filter(func.coalesce(Job.posted_at, Job.discovered_at) >= oldest)
             .filter(or_(Job.open_state.is_(None), Job.open_state != "closed"))
             .filter(Job.duplicate_of.is_(None)))
        jobs = []
        for j in q.all():
            seen = j.posted_at or j.discovered_at
            if seen is not None and seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            is_tracked = name_key(j.company) in tracked
            released = bool((j.curation or {}).get("released"))
            if released or (seen is not None and seen >= (since_tracked if is_tracked else since)):
                jobs.append((j, is_tracked))
        # a released job that loses today stays new for another try (never auto-ignored)
        all_ids = [j.id for j, _ in jobs if not (j.curation or {}).get("released")]
        pool = []
        for j, is_tracked in jobs:
            if (j.loc_country in (None, "", "US")) and name_key(j.company) not in recent_apps:
                d = _job_dict(j)
                d["tracked"] = is_tracked
                pool.append(d)
        return pool, all_ids
    finally:
        db.close()


def hard_drop(job: dict, title_exclude: list, company_exclude: list | None = None, max_years: int = 6,
              drop_unlikely: bool = False) -> str | None:
    if not job["company"].strip():
        return "no company"
    agency = scoring.company_excluded(job["company"], company_exclude or [])
    if agency:
        return f"company: {agency}"
    gig = scoring.gig_title(job["title"])
    if gig:
        return f"gig: {gig}"
    w = scoring.title_excluded(job["title"], title_exclude)
    if w:
        return f"title: {w}"
    if job["jd_flag"]:
        return "no sponsorship"
    blocker = scoring.jd_blocker(job["description"])
    if blocker:
        return f"jd: {blocker}"
    if drop_unlikely and job.get("h1b_verdict") == "unlikely":
        return "unlikely H-1B sponsor"
    yrs = scoring.min_years_required(job["description"])
    if yrs is not None and yrs >= max_years:
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
            "tracked": 1.0 if j.get("tracked") else 0.0,
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
    """Best first, at most PER_COMPANY_CAP per company, one copy of a posting seen on two boards."""
    top, per, seen = [], {}, set()
    for j in ranked:
        k = name_key(j["company"])
        if per.get(k, 0) >= PER_COMPANY_CAP:
            continue
        same = scoring.same_posting_key(j["company"], j["title"])
        if same in seen:
            continue
        seen.add(same)
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
                            "apply_window": scoring.apply_window(i, j["parts"]["fresh"]), "intel": j["intel"],
                            "released": (row.curation or {}).get("released") or 0}
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


def _with_db(fn, *args):
    db = SessionLocal()
    try:
        return fn(db, *args)
    finally:
        db.close()


def _clean(v) -> str:
    return " ".join(str(v or "").split()).strip(" ,;")


def sender_brief(data: dict) -> str:
    """A compact profile for outreach drafts from a résumé's json_data: summary, recent roles, skills and
    every project with its stack, one line and link, so a draft can name the project that fits."""
    lines = []
    if _clean(data.get("summary")):
        lines.append(f"Summary: {_clean(data['summary'])}")
    roles = []
    for e in data.get("experience") or []:
        if isinstance(e, dict) and _clean(e.get("title")):
            role = f"{_clean(e['title'])} at {_clean(e.get('company'))}" if _clean(e.get("company")) else _clean(e["title"])
            if role not in roles:
                roles.append(role)
    if roles:
        lines.append("Experience: " + "; ".join(roles[:3]))
    skills = data.get("skills")
    if isinstance(skills, dict):
        lines.append("Skills: " + "; ".join(f"{k}: {_clean(v)[:140]}" for k, v in skills.items() if _clean(v)))
    elif isinstance(skills, list):
        lines.append("Skills: " + ", ".join(_clean(x) for x in skills if _clean(x))[:500])
    projects = [p for p in data.get("projects") or [] if isinstance(p, dict) and _clean(p.get("name"))]
    if projects:
        lines.append("Projects:")
        for p in projects:
            bullets = [_clean(b) for b in p.get("bullets") or [] if _clean(b)]
            bits = [_clean(p["name"])]
            if _clean(p.get("description")):
                bits[0] += f" ({_clean(p['description'])})"
            if bullets:
                bits.append(bullets[0][:220])
            if _clean(p.get("url")):
                bits.append(_clean(p["url"]))
            lines.append("- " + ": ".join(bits[:2]) + (f" [{bits[2]}]" if len(bits) > 2 else ""))
    edu = [e for e in data.get("education") or [] if isinstance(e, dict)]
    if edu:
        lines.append("Education: " + "; ".join(f"{_clean(e.get('degree'))}, {_clean(e.get('school'))}" for e in edu[:2]))
    return "\n".join(lines)


def _me() -> tuple[str, str, dict]:
    """(sender's name, profile text for drafts, persona contact) from the Persona and the default résumé."""
    from backend.analyzer.cv_scorer import _get_default_resume, _get_resume_texts
    from backend.models.db import Resume
    db = SessionLocal()
    try:
        from backend.accounts.owned import get_persona
        from backend.accounts.settings import get_setting
        p = get_persona(db, create=False)
        contact = (p.contact if p else None) or {}
        name = " ".join(x for x in (contact.get("first_name"), contact.get("last_name")) if x) or "the candidate"
        brief = ""
        rid = get_setting(db, "default_resume_id") or ""
        if rid == "persona":
            brief = sender_brief((p.resume_content if p else None) or {})
        elif rid:
            try:
                r = db.query(Resume).filter(Resume.id == rid).first()
            except Exception:
                r = None
            brief = sender_brief(r.json_data or {}) if r else ""
        if not brief:      # no structured default: the first flattened résumé, as before
            texts = list((_get_default_resume(db) or _get_resume_texts(db)).values())
            brief = texts[0][:2500] if texts else ""
        return name, brief, contact
    finally:
        db.close()


def _is_small(it: dict) -> bool:
    return (it.get("headcount") or 10**6) < 300 or (it.get("funding_stage") or "") in ("seed", "pre_seed", "series_a", "series_b")


# What went wrong finding people in the current run, shown in the run's summary (curator/routes.py)
# instead of only the log: a broken LinkedIn session stayed invisible for ten days.
_OUTREACH_ERRORS: list = []


async def find_contacts(top: list[dict], intel: dict, want_total: int) -> list[dict]:
    from backend.curator.linkedin_people import LinkedInPeople
    from backend.curator.people import draft, find_people, guess_email
    me, profile, _ = _me()
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
            small = _is_small(it)
            city = (j.get("loc_city") or "").title() or None
            try:   # one company failing never costs the people already found
                people = await find_people(j["company"], j["title"], small, li, want=1 if want_total - len(found) <= len(top) else 2,
                                           country=j.get("loc_country") or "US", region=j.get("loc_region"), city=city)
            except Exception as e:
                logger.exception(f"curator outreach: {j['company']} failed: {e}")
                _OUTREACH_ERRORS.append(f"{j['company']}: {e}")
                continue
            for p in people:
                email, conf = guess_email(p["name"], it)
                d = await draft(p, j["company"], j, me, profile)
                found.append({"job_id": j["id"], "company": j["company"], "name": p["name"],
                              "title": p.get("headline"), "kind": p.get("kind"), "linkedin_url": p["url"],
                              "location": p.get("location"),
                              "email": email, "email_confidence": conf, "source": p.get("source"),
                              "note": d.get("note"), "email_draft": d.get("email_draft"), "found_on": today,
                              "purpose": "job", "lead_kind": "job"})
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


NETWORK_COOLDOWN_DAYS = 45


def network_companies(ranked: list[dict], top: list[dict], cfg: dict, limit: int) -> list[str]:
    """Companies to meet decision makers at, best first: the Companies list (tier 1, then 2, ...), then
    companies ranked just below today's cut, then well-funded researched companies. Leaves out any
    company with a contact found in the last 45 days (today's job contacts included) and staffing firms."""
    from backend.models.db import CompanyIntel
    today = local_today()
    year = today.year
    db = SessionLocal()
    try:
        recent = {name_key(c) for (c,) in db.query(Contact.company)
                  .filter(Contact.found_on >= today - timedelta(days=NETWORK_COOLDOWN_DAYS)).all()}
        tracked = [n for (n,) in db.query(Company.name).filter(Company.active == True)  # noqa: E712
                   .order_by(func.coalesce(Company.tier, 99), Company.name).all()]
        funded = [r.display_name or r.name_key for r in db.query(CompanyIntel).order_by(CompanyIntel.fetched_at.desc()).all()
                  if scoring.well_funded(r.funding_stage, r.last_raise_year, None, year)]
    finally:
        db.close()
    top_ids = {j["id"] for j in top}
    below_cut = [j["company"] for j in ranked if j["id"] not in top_ids]
    out, seen = [], set(recent)
    for name in tracked + below_cut + funded:
        k = name_key(name)
        if not k or k in seen or scoring.company_excluded(name, cfg["company_exclude"]):
            continue
        seen.add(k)
        out.append(name)
        if len(out) >= limit:
            break
    return out


async def find_network_contacts(companies: list[str], want_total: int) -> list[dict]:
    """Up to `want_total` decision makers (one per company) at `companies`, with drafts that ask for a
    chat and a referral rather than mention a posting; saved as purpose='network' with no job."""
    from backend.analyzer.location import parse as parse_location
    from backend.curator.linkedin_people import LinkedInPeople
    from backend.curator.people import draft_network, find_decision_makers, guess_email, profile_areas
    if want_total <= 0 or not companies:
        return []
    me, profile, contact = _me()
    areas = profile_areas(profile) or ["backend"]
    city = (contact.get("city") or "").strip() or None
    region = parse_location(", ".join(x for x in (contact.get("city"), contact.get("state")) if x)).get("region") if city else None
    country = "US"
    intel = await get_intel(companies[: want_total * 2])
    today = local_today()
    found = []
    async with LinkedInPeople() as li:
        for company in companies:
            if len(found) >= want_total:
                break
            it = intel.get(name_key(company)) or {}
            try:
                people = await find_decision_makers(company, areas, _is_small(it), li, want=1,
                                                    country=country, region=region, city=city)
            except Exception as e:
                logger.exception(f"curator network outreach: {company} failed: {e}")
                _OUTREACH_ERRORS.append(f"{company}: {e}")
                continue
            for p in people:
                email, conf = guess_email(p["name"], it)
                d = await draft_network(p, company, it, me, profile)
                found.append({"job_id": None, "company": company, "name": p["name"], "title": p.get("headline"),
                              "kind": p.get("kind"), "linkedin_url": p["url"], "location": p.get("location"),
                              "email": email, "email_confidence": conf, "source": p.get("source"),
                              "note": d.get("note"), "email_draft": d.get("email_draft"), "found_on": today,
                              "purpose": "network", "lead_kind": "network"})
    db = SessionLocal()
    try:
        saved = []
        for c in found:
            if db.query(Contact).filter(Contact.company == c["company"], Contact.linkedin_url == c["linkedin_url"]).first():
                continue
            db.add(Contact(**c))
            saved.append(c)
        db.commit()
        return saved
    finally:
        db.close()


def summary_message(top: list[dict], contacts: list[dict], network: list[dict]) -> str:
    """The morning Telegram line: counts only, the list itself lives on the Today screen."""
    companies = len({name_key(j["company"]) for j in top})
    people = len(contacts) + len(network)
    return (f"🎯 <b>Curated list generated — {local_now().strftime('%a %b %d')}</b>\n"
            f"{len(top)} job{'' if len(top) == 1 else 's'} to apply · {companies} unique compan{'y' if companies == 1 else 'ies'} · "
            f"{people} {'person' if people == 1 else 'people'} to connect ({len(contacts)} job-linked, {len(network)} network)\n"
            f"Good luck today!")


async def notify(top: list[dict], contacts: list[dict], network: list[dict] | None = None) -> None:
    from backend.notifier.telegram import _get_chat_id, _send_message, feature_on
    if not feature_on("telegram_curator_summary"):
        return
    chat = _get_chat_id()
    if not chat:
        return
    await _send_message(chat, summary_message(top, contacts, network or []))


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
    released = {"released": 0, "retired": 0}
    if not dry_run:
        released = await asyncio.to_thread(_with_db, release_stale_jobs, local_today(), cfg["release_hours"])
    pool, pool_ids = await asyncio.to_thread(load_pool, cfg["window_hours"])
    dropped, keep = {}, []
    for j in pool:
        why = hard_drop(j, cfg["title_exclude"], cfg["company_exclude"], cfg["max_years"], cfg["drop_unlikely"])
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

    contacts, network, again = [], [], []
    _OUTREACH_ERRORS.clear()
    if outreach and cfg["outreach"] > 0:
        # people never contacted come back first, taking up to half the day's slots
        again = await asyncio.to_thread(_with_db, resurface_contacts, local_today(), cfg["release_hours"],
                                        -(-cfg["outreach"] // 2), [j["company"] for j in top])
    if outreach and cfg["outreach"] - len(again) > 0 and top:
        try:
            contacts = await find_contacts(top, intel, cfg["outreach"] - len(again))
        except Exception as e:
            logger.exception(f"curator outreach failed: {e}")
            _OUTREACH_ERRORS.append(f"outreach: {e}")
    # Job slots nobody filled go to people at companies with no posting for the user today.
    network_want = cfg["network"] + max(0, cfg["outreach"] - len(again) - len(contacts)) if cfg["network"] > 0 else 0
    if outreach and network_want > 0:
        try:
            network = await find_network_contacts(network_companies(ranked, top, cfg, network_want * 3), network_want)
        except Exception as e:
            logger.exception(f"curator network outreach failed: {e}")
            _OUTREACH_ERRORS.append(f"network outreach: {e}")
    if send:
        try:
            await notify(top, contacts, network)
        except Exception as e:
            logger.warning(f"curator Telegram summary failed: {e}")
    summary = {"pool": len(pool_ids), "candidates": len(pool), "dropped": len(dropped),
               "llm_pool": len(shortlist), "saved": len(top), "ignored": ignored, "contacts": len(contacts),
               "network": len(network), "released": released["released"], "retired": released["retired"],
               "resurfaced": len(again)}
    if _OUTREACH_ERRORS:
        summary["outreach_errors"] = list(_OUTREACH_ERRORS[:5])
    logger.info(f"curator done: {summary}")
    return summary
