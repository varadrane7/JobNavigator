"""The connector's tools.

Each tool answers one question with the fewest fields that answer it: lists are short rows
with ids, and a `get_*` tool returns one item in full. Empty fields are left out. A tool runs
inside the caller's account context, so every query sees that account's rows only.

A tool is `Tool(name, description, input schema, function, write)`; `write` tools need a
token with the write scope and are hidden from a read-only one.
"""
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import func, or_

import re

from backend.models.db import ActivityLog, Application, Contact, Interview, Job, JobRun, Persona, Resume, utcnow
from backend.tz import local_today

DESCRIPTION_CHARS = 6000
MAX_ROWS = 50


class ToolError(Exception):
    """A message for the model: what was wrong with the call."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    fn: Callable
    write: bool = False
    title: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _lean(value):
    """Drop None, empty strings and empty containers, recursively."""
    if isinstance(value, dict):
        out = {k: _lean(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_lean(v) for v in value]
    return value


def _day(value) -> Optional[str]:
    if value is None:
        return None
    return (value.date() if isinstance(value, datetime) else value).isoformat()


def _limit(args: dict, default: int) -> int:
    try:
        return max(1, min(MAX_ROWS, int(args.get("limit") or default)))
    except (TypeError, ValueError):
        return default


def _since(args: dict, key: str = "since_days") -> Optional[datetime]:
    try:
        days = int(args.get(key) or 0)
    except (TypeError, ValueError):
        days = 0
    return datetime.now(timezone.utc) - timedelta(days=days) if days > 0 else None


def _uuid(value, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        raise ToolError(f"{what} must be an id from a list tool, got {value!r}")


def _date(value, what: str) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ToolError(f"{what} must be a date like 2026-01-31")


def _like(column, text: str):
    return column.ilike(f"%{text.strip()}%")


def _job(db, ref) -> Job:
    """A job by id or by the short number the dashboard shows (#1234)."""
    text = str(ref or "").strip().lstrip("#")
    job = None
    if text.isdigit():
        job = db.query(Job).filter(Job.short_id == int(text)).first()
    elif text:
        job = db.query(Job).filter(Job.id == _uuid(text, "job_id")).first()
    if job is None:
        raise ToolError(f"No job {ref!r}")
    return job


def _pay(job: Job) -> Optional[str]:
    lo, hi = job.salary_min, job.salary_max
    if not lo and not hi:
        return None
    k = lambda n: f"{round(n / 1000)}k"
    return f"{k(lo)}-{k(hi)}" if lo and hi and lo != hi else k(lo or hi)


def _job_row(job: Job) -> dict:
    return {"id": str(job.id), "no": job.short_id, "company": job.company, "title": job.title,
            "location": job.location, "pay": _pay(job),
            "fit": round(job.best_cv_score) if job.best_cv_score is not None else None,
            "status": job.status, "h1b": job.h1b_verdict, "posted": _day(job.posted_at or job.discovered_at)}


# ── Read tools ───────────────────────────────────────────────────────────────

def overview(db, args):
    today = local_today()
    now = datetime.now(timezone.utc)
    week, month = now - timedelta(days=7), now - timedelta(days=30)
    jobs = dict(db.query(Job.status, func.count()).group_by(Job.status).all())
    apps = dict(db.query(Application.status, func.count()).group_by(Application.status).all())
    total_apps = sum(apps.values())
    heard = sum(n for s, n in apps.items() if s in ("interview", "offer", "rejected"))
    leads = dict(db.query(Contact.status, func.count()).group_by(Contact.status).all())
    upcoming = (db.query(Interview, Application).join(Application, Interview.application_id == Application.id)
                .filter(Interview.status == "scheduled", Interview.when_at >= now)
                .order_by(Interview.when_at).limit(5).all())
    due_apps = (db.query(Application).filter(Application.next_action_date.isnot(None),
                                             Application.next_action_date <= today,
                                             Application.status.in_(("applied", "interview"))).count())
    return {
        "date": today.isoformat(),
        "jobs": {"in_feed": sum(n for s, n in jobs.items() if s not in ("duplicate", "ignored")),
                 "new": jobs.get("new", 0), "saved": jobs.get("saved", 0),
                 "found_last_7d": db.query(Job).filter(Job.discovered_at >= week, Job.status != "duplicate").count(),
                 "curated_today": db.query(Job).filter(Job.curated_on == today, Job.curated_rank.isnot(None)).count()},
        "applications": {"total": total_apps, "by_status": apps,
                         "sent_last_7d": db.query(Application).filter(Application.applied_at >= week).count(),
                         "sent_last_30d": db.query(Application).filter(Application.applied_at >= month).count(),
                         "heard_back_pct": round(100 * heard / total_apps) if total_apps else None,
                         "interview_pct": round(100 * (apps.get("interview", 0) + apps.get("offer", 0)) / total_apps)
                         if total_apps else None,
                         "next_step_due": due_apps},
        "interviews_upcoming": [{"when": i.when_at.isoformat() if i.when_at else None, "what": i.what,
                                 "where": i.where_text, "company": a.job.company if a.job else None,
                                 "title": a.job.title if a.job else None, "application_id": str(a.id)}
                                for i, a in upcoming],
        "leads": {"by_status": leads,
                  "due_today": db.query(Contact).filter(Contact.next_action_at.isnot(None),
                                                        Contact.next_action_at <= today,
                                                        Contact.status.notin_(("skip", "referral"))).count()},
    }


# ── Insights ─────────────────────────────────────────────────────────────────

_LANES = (("fullstack", r"full.?stack"), ("fde / applied", r"forward deployed|solutions|applied ai"),
          ("ml / ai", r"machine learning|\bml\b|research|data scien|\bai\b"), ("frontend", r"front.?end"),
          ("platform / infra", r"platform|infra|cloud|devops|\bsre\b|reliab"), ("security", r"security"),
          ("backend", r"back.?end"))
_SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|architect|iii|iv)\b", re.I)
_JOURNAL = "journal"


def _lane(title: str) -> str:
    return next((name for name, rx in _LANES if re.search(rx, title or "", re.I)), "generic swe / other")


def _age_bucket(days) -> str:
    if days is None:
        return "unknown"
    return "0-2 days" if days <= 2 else "3-7 days" if days <= 7 else "8-14 days" if days <= 14 else "15+ days"


def _tally(rows: list, key) -> list:
    """[{group, applied, rejected, interviews, heard_pct}] for `rows` of (application, job), largest first."""
    groups: dict = {}
    for a, j in rows:
        g = groups.setdefault(key(a, j) or "(none)", [0, 0, 0, 0])
        g[0] += 1
        g[1] += a.status == "rejected"
        g[2] += a.status in ("interview", "offer")
        g[3] += a.status != "applied" or a.last_email_received is not None
    return [{"group": k, "applied": n, "rejected": rej, "interviews": iv, "heard_pct": round(100 * heard / n)}
            for k, (n, rej, iv, heard) in sorted(groups.items(), key=lambda kv: -kv[1][0])]


def insights(db, args):
    since = _since(args)
    q = db.query(Application, Job).join(Job, Application.job_id == Job.id)
    if since is not None:
        q = q.filter(Application.applied_at >= since)
    rows = q.all()
    contacted = {(c or "").strip().lower() for (c,) in db.query(Contact.company).filter(
        Contact.status.in_(("requested", "connected", "messaged", "replied", "referral"))).all() if c}

    def age(a, j):
        seen = j.posted_at or j.discovered_at
        if not a.applied_at or not seen:
            return None
        aware = lambda d: d if d.tzinfo else d.replace(tzinfo=timezone.utc)   # SQLite hands back naive times
        return (aware(a.applied_at) - aware(seen)).days

    def week(a, j):
        d = a.applied_at.date() if a.applied_at else None
        return (d - timedelta(days=d.weekday())).isoformat() if d else None

    leads = db.query(Contact.status, Contact.kind, Contact.lead_kind, func.count()).group_by(
        Contact.status, Contact.kind, Contact.lead_kind).all()
    by_role, by_kind = {}, {}
    for status, role, kind, n in leads:
        if status in ("requested", "connected", "messaged", "replied", "referral"):
            r = by_role.setdefault(role or "unknown", {"contacted": 0, "replied": 0, "referral": 0})
            r["contacted"] += n
            r["replied"] += n if status in ("replied", "referral") else 0
            r["referral"] += n if status == "referral" else 0
            by_kind[kind or "job"] = by_kind.get(kind or "job", 0) + n
    runs = (db.query(JobRun).filter(JobRun.job_type.in_(("curate", "network_scout")))
            .order_by(JobRun.started_at.desc()).limit(6).all())
    n = len(rows)
    return {
        "window": f"last {args['since_days']} days" if since is not None else "all time",
        "applications": n,
        "interviews": sum(a.status in ("interview", "offer") for a, _ in rows),
        "by_source": _tally(rows, lambda a, j: j.source)[:12],
        "by_lane": _tally(rows, lambda a, j: _lane(j.title)),
        "by_resume": _tally(rows, lambda a, j: (a.cv_version_used or "").split(" → ")[0] or "(not recorded)")[:8],
        "by_posting_age": _tally(rows, lambda a, j: _age_bucket(age(a, j))),
        "by_sponsor_record": _tally(rows, lambda a, j: j.h1b_verdict or "unknown"),
        "by_apply_link": _tally(rows, lambda a, j: j.link_kind or "unknown"),
        "by_week": sorted(_tally(rows, week), key=lambda r: r["group"])[-10:],
        "flags": {"senior_title": sum(bool(_SENIOR.search(j.title or "")) for _, j in rows),
                  "with_a_contacted_person_at_the_company": sum((j.company or "").strip().lower() in contacted
                                                                for _, j in rows),
                  "resume_not_recorded": sum(not a.cv_version_used for a, _ in rows)},
        "outreach": {"by_role": by_role, "by_kind": by_kind},
        "recent_runs": [{"run": r.job_type, "at": r.started_at.isoformat(timespec="minutes") if r.started_at else None,
                         "status": r.status, "summary": _short(r.result_summary or r.error, 400)} for r in runs],
        "note": "Small groups prove little: read a rate only where applied is 30 or more.",
    }


def journal(db, args):
    q = db.query(ActivityLog).filter(ActivityLog.type == _JOURNAL)
    if args.get("query"):
        q = q.filter(_like(ActivityLog.message, args["query"]))
    since = _since(args)
    if since is not None:
        q = q.filter(ActivityLog.created_at >= since)
    total = q.count()
    rows = q.order_by(ActivityLog.created_at.desc()).limit(_limit(args, 20)).all()
    return {"total": total, "entries": [{"at": _day(r.created_at), "by": (r.details or {}).get("by"),
                                         "topic": r.company, "note": r.message} for r in rows]}


def log_note(db, args):
    note = str(args.get("note") or "").strip()
    if not note:
        raise ToolError("note is required")
    row = ActivityLog(type=_JOURNAL, message=note[:4000], company=(str(args.get("topic") or "").strip() or None),
                      details={"by": str(args.get("agent") or "connector")[:40]})
    db.add(row)
    db.commit()
    return {"saved": True, "at": _day(row.created_at), "topic": row.company}


def list_applications(db, args):
    q = db.query(Application).join(Job, Application.job_id == Job.id)
    if args.get("status"):
        q = q.filter(Application.status.in_([s.strip() for s in str(args["status"]).split(",")]))
    if args.get("query"):
        q = q.filter(or_(_like(Job.company, args["query"]), _like(Job.title, args["query"])))
    since = _since(args)
    if since:
        q = q.filter(Application.applied_at >= since)
    total = q.count()
    rows = q.order_by(Application.updated_at.desc()).limit(_limit(args, 20)).all()
    return {"total": total, "applications": [{
        "id": str(a.id), "company": a.job.company, "title": a.job.title, "status": a.status,
        "applied": _day(a.applied_at), "updated": _day(a.updated_at),
        "next": a.next_action, "next_date": _day(a.next_action_date),
        "last_email": _day(a.last_email_received),
        "interviews": len(a.interviews or []) or None} for a in rows]}


def get_application(db, args):
    a = db.query(Application).filter(Application.id == _uuid(args.get("application_id"), "application_id")).first()
    if a is None:
        raise ToolError("No such application")
    job = a.job
    return {"id": str(a.id), "status": a.status, "applied": _day(a.applied_at), "updated": _day(a.updated_at),
            "resume_used": a.cv_version_used, "notes": a.notes,
            "next": a.next_action, "next_date": _day(a.next_action_date),
            "last_email": {"on": _day(a.last_email_received), "snippet": a.last_email_snippet},
            "history": [{"to": t.get("to"), "at": (t.get("at") or "")[:10], "by": t.get("source")}
                        for t in (a.status_transitions or [])],
            "interviews": [{"what": i.what, "when": i.when_at.isoformat() if i.when_at else None,
                            "where": i.where_text, "status": i.status, "prep": i.prep} for i in (a.interviews or [])],
            "job": _job_row(job) if job else None}


def search_jobs(db, args):
    q = db.query(Job)
    status = args.get("status")
    q = q.filter(Job.status.in_([s.strip() for s in str(status).split(",")])) if status \
        else q.filter(Job.status.notin_(("duplicate", "ignored")))
    if args.get("query"):
        q = q.filter(or_(_like(Job.company, args["query"]), _like(Job.title, args["query"])))
    if args.get("location"):
        q = q.filter(_like(Job.location, args["location"]))
    if args.get("min_fit") is not None:
        q = q.filter(Job.best_cv_score >= float(args["min_fit"]))
    since = _since(args)
    if since:
        q = q.filter(Job.discovered_at >= since)
    total = q.count()
    order = Job.discovered_at.desc() if args.get("sort") == "newest" else Job.best_cv_score.desc().nullslast()
    return {"total": total, "jobs": [_job_row(j) for j in q.order_by(order, Job.discovered_at.desc())
                                     .limit(_limit(args, 10)).all()]}


def get_job(db, args):
    job = _job(db, args.get("job_id"))
    report = job.scoring_report if isinstance(job.scoring_report, dict) else {}
    text = job.description or ""
    out = _job_row(job)
    out.update({
        "url": job.apply_url or job.url, "source": job.source, "found": _day(job.discovered_at), "open": job.open_state,
        "h1b_note": job.h1b_jd_snippet, "best_resume": job.best_cv,
        "fit_summary": report.get("summary") if isinstance(report.get("summary"), str) else None,
        "curation": (job.curation or {}).get("reason") if isinstance(job.curation, dict) else None,
        "application": [{"id": str(a.id), "status": a.status} for a in (job.applications or [])],
    })
    if args.get("description", True):
        out["description"] = text[:DESCRIPTION_CHARS]
        if len(text) > DESCRIPTION_CHARS:
            out["description_cut"] = f"{len(text) - DESCRIPTION_CHARS} more characters not shown"
    return out


def today(db, args):
    day = _date(args.get("day"), "day") or local_today()
    jobs = (db.query(Job).filter(Job.curated_on == day, Job.curated_rank.isnot(None))
            .order_by(Job.curated_rank).limit(_limit(args, 20)).all())
    rows = []
    for j in jobs:
        row = _job_row(j)
        row["rank"] = j.curated_rank
        row["why"] = (j.curation or {}).get("reason") if isinstance(j.curation, dict) else None
        rows.append(row)
    people = db.query(Contact).filter(Contact.found_on == day).count()
    return {"day": day.isoformat(), "jobs": rows, "people_found": people}


def list_resumes(db, args):
    resumes = db.query(Resume).order_by(Resume.is_base.desc(), Resume.updated_at.desc()).all()
    if not args.get("tailored"):
        shown = [r for r in resumes if r.is_base]
    else:
        shown = resumes[:MAX_ROWS]
    job_ids = {r.job_id for r in shown if r.job_id}
    jobs = {j.id: j for j in db.query(Job).filter(Job.id.in_(job_ids)).all()} if job_ids else {}
    return {"base": sum(1 for r in resumes if r.is_base), "tailored": sum(1 for r in resumes if not r.is_base),
            "resumes": [{"id": str(r.id), "name": r.name, "kind": "base" if r.is_base else "tailored",
                         "for": (f"{jobs[r.job_id].company}: {jobs[r.job_id].title}" if r.job_id in jobs else None),
                         "template": r.template, "updated": _day(r.updated_at)} for r in shown]}


def get_resume(db, args):
    ref = str(args.get("resume") or "").strip()
    if not ref:
        raise ToolError("resume is required: an id or a name from list_resumes")
    resume = None
    try:
        resume = db.query(Resume).filter(Resume.id == uuid.UUID(ref)).first()
    except ValueError:
        named = (db.query(Resume).filter(func.lower(Resume.name) == ref.lower())
                 .order_by(Resume.is_base.desc(), Resume.updated_at.desc()).all())
        resume = named[0] if named else None
    if resume is None:
        raise ToolError(f"No résumé {ref!r}")
    out = {"id": str(resume.id), "name": resume.name, "kind": "base" if resume.is_base else "tailored",
           "updated": _day(resume.updated_at)}
    if args.get("format") == "json":
        out["data"] = resume.json_data or {}
    else:
        from backend.analyzer.cv_scorer import _flatten_resume
        out["text"] = _flatten_resume(resume.json_data or {})
    return out


PROFILE_SECTIONS = ("contact", "work_auth", "preferences", "compensation", "networking", "projects", "writing",
                    "experience", "answers")
_NETWORKING_KEYS = ("pitch", "asks", "affinities", "visa_note", "target_roles", "target_domains")
_PROJECT_KEYS = ("name", "one_liner", "who_cares", "stack", "highlights", "url", "homepage")


def get_profile(db, args):
    wanted = args.get("sections") or ["contact", "work_auth", "preferences", "networking"]
    if isinstance(wanted, str):
        wanted = [s.strip() for s in wanted.split(",")]
    unknown = [s for s in wanted if s not in PROFILE_SECTIONS]
    if unknown:
        raise ToolError(f"sections may be: {', '.join(PROFILE_SECTIONS)}")
    persona = db.query(Persona).first()
    if persona is None:
        raise ToolError("No profile yet: fill in Persona in the dashboard")
    net = persona.networking if isinstance(persona.networking, dict) else {}
    source = {"contact": persona.contact, "work_auth": persona.work_auth, "preferences": persona.preferences,
              "compensation": persona.compensation,
              "networking": {**{k: net.get(k) for k in _NETWORKING_KEYS},
                             "projects": [p.get("name") for p in net.get("projects") or []
                                          if isinstance(p, dict) and not p.get("hidden")]},
              "projects": [{k: p.get(k) for k in _PROJECT_KEYS} for p in net.get("projects") or []
                           if isinstance(p, dict) and not p.get("hidden")],
              "writing": net.get("writing"),
              "experience": persona.resume_content, "answers": persona.qa_bank}
    return {name: source[name] for name in wanted}


def _short(text, chars: int = 140):
    return text if not text or len(text) <= chars else text[:chars - 1].rstrip() + "…"


def _lead_row(c: Contact) -> dict:
    return {"id": str(c.id), "name": c.name, "title": _short(c.title, 80), "company": c.company,
            "kind": c.lead_kind or c.purpose or "job", "status": c.status, "score": c.score, "degree": c.degree,
            "why": _short(c.why), "due": _day(c.next_action_at)}


def list_leads(db, args):
    q = db.query(Contact)
    if args.get("status"):
        q = q.filter(Contact.status.in_([s.strip() for s in str(args["status"]).split(",")]))
    if args.get("kind"):
        kinds = [s.strip() for s in str(args["kind"]).split(",")]
        q = q.filter(or_(Contact.lead_kind.in_(kinds), Contact.purpose.in_(kinds)))
    if args.get("query"):
        q = q.filter(or_(_like(Contact.company, args["query"]), _like(Contact.name, args["query"])))
    if args.get("min_score") is not None:
        q = q.filter(Contact.score >= int(args["min_score"]))
    if args.get("due"):
        q = q.filter(Contact.next_action_at.isnot(None), Contact.next_action_at <= local_today(),
                     Contact.status.notin_(("skip", "referral")))
        order = (Contact.next_action_at, Contact.score.desc().nullslast())
    else:
        order = (Contact.score.desc().nullslast(), Contact.created_at.desc())
    total = q.count()
    return {"total": total, "leads": [_lead_row(c) for c in q.order_by(*order).limit(_limit(args, 10)).all()]}


def _lead(db, ref) -> Contact:
    c = db.query(Contact).filter(Contact.id == _uuid(ref, "lead_id")).first()
    if c is None:
        raise ToolError("No such lead")
    return c


def _lead_full(db, c: Contact) -> dict:
    out = _lead_row(c)
    job = db.query(Job).filter(Job.id == c.job_id).first() if c.job_id else None
    out.update({"title": c.title, "why": c.why, "linkedin": c.linkedin_url, "email": c.email, "location": c.location, "role": c.kind, "source": c.source,
                "talking_points": c.talking_points, "show_project": c.project_ref, "evidence": c.evidence,
                "linkedin_note": c.note, "email_draft": c.email_draft,
                "last_step": _day(c.last_action_at), "found": _day(c.found_on),
                "job": {"id": str(job.id), "title": job.title, "company": job.company,
                        "url": job.apply_url or job.url} if job else None})
    return out


def get_lead(db, args):
    return _lead_full(db, _lead(db, args.get("lead_id")))


# ── Write tools ──────────────────────────────────────────────────────────────

def update_lead(db, args):
    from backend.curator.routes import CONTACT_STATUSES, _SNOOZE_DAYS
    c = _lead(db, args.get("lead_id"))
    status = args.get("status")
    if status is not None:
        status = "requested" if status == "contacted" else status
        if status not in CONTACT_STATUSES:
            raise ToolError("status must be one of " + ", ".join(CONTACT_STATUSES))
        if status != c.status:
            c.status = status
            c.last_action_at = utcnow()
            if "next_action_at" not in args:
                c.next_action_at = local_today() + timedelta(days=_SNOOZE_DAYS) if status == "snoozed" else None
    if "next_action_at" in args:
        c.next_action_at = _date(args["next_action_at"], "next_action_at")
    for arg, column in (("linkedin_note", "note"), ("email_draft", "email_draft"), ("email", "email")):
        if arg in args:
            setattr(c, column, args[arg] or None)
    if "talking_points" in args:
        points = args["talking_points"]
        if not isinstance(points, list) or not all(isinstance(p, str) for p in points):
            raise ToolError("talking_points must be a list of strings")
        c.talking_points = points
    db.commit()
    return _lead_full(db, c)


def add_lead(db, args):
    from backend.curator import leads
    data = dict(args)
    if data.get("job_id"):
        data["job"] = _job(db, data["job_id"])
    try:
        lead, created = leads.log_lead(db, data, default_source="connector")
    except leads.LeadError as exc:
        raise ToolError(str(exc))
    db.commit()
    return {**({} if created else {"already_a_lead": True}), **_lead_full(db, lead)}


def update_application(db, args):
    from backend.api.routes_applications import VALID_STATUSES
    from backend.models.db import record_transition
    a = db.query(Application).filter(Application.id == _uuid(args.get("application_id"), "application_id")).first()
    if a is None:
        raise ToolError("No such application")
    status = args.get("status")
    if status is not None:
        if status not in VALID_STATUSES:
            raise ToolError(f"status must be one of {sorted(VALID_STATUSES)}")
        if status != a.status:
            record_transition(a, status, "connector")
            a.status = status
    if "append_note" in args and args["append_note"]:
        stamp = local_today().isoformat()
        a.notes = f"{a.notes.rstrip()}\n\n[{stamp}] {args['append_note']}" if a.notes else f"[{stamp}] {args['append_note']}"
    if "next" in args:
        a.next_action = args["next"] or None
    if "next_date" in args:
        a.next_action_date = _date(args["next_date"], "next_date")
    a.updated_at = utcnow()
    db.commit()
    return get_application(db, {"application_id": str(a.id)})


# ── Registry ─────────────────────────────────────────────────────────────────

def _schema(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_ROWS, "description": "Rows to return"}
_SINCE = {"type": "integer", "minimum": 1, "description": "Only the last N days"}
_LEAD_STATUS = "new, requested (connection sent), connected, messaged, replied, referral, snoozed, skip"

TOOLS: tuple[Tool, ...] = (
    Tool("overview", "The job search at a glance: feed size, applications by status with reply and interview "
         "rates, upcoming interviews, leads by status and how many are due. Start here for any question about "
         "progress or numbers.", _schema({}), overview, title="Search overview"),
    Tool("insights", "What the search's own data says works: applications, rejections, interviews and reply rate "
         "grouped by job source, role lane, résumé used, posting age, sponsor record, apply link and week; how many "
         "applications had a contacted person at the company; outreach by the kind of person; and the last curator "
         "and lead-scout runs with their errors. Use it for any why-is-this-not-working question. since_days limits "
         "the window.", _schema({"since_days": _SINCE}), insights, title="Search insights"),
    Tool("journal", "The search journal: dated notes on what was changed, decided or found (settings changed, "
         "résumé edits, experiments and their results), newest first. Read it before repeating an analysis or "
         "changing a setting. Filter by text or to the last N days.",
         _schema({"query": {"type": "string"}, "since_days": _SINCE, "limit": _LIMIT}), journal, title="Search journal"),
    Tool("list_applications", "Applications, most recently updated first, as short rows. Filter by status "
         "(applied, interview, offer, rejected; comma-separated), by company or title text, or to the last N days.",
         _schema({"status": {"type": "string"}, "query": {"type": "string", "description": "Company or title text"},
                  "since_days": _SINCE, "limit": _LIMIT}), list_applications, title="List applications"),
    Tool("get_application", "One application in full: notes, status history, interviews with prep notes, the last "
         "email heard, and its job.", _schema({"application_id": {"type": "string"}}, ["application_id"]),
         get_application, title="Application details"),
    Tool("search_jobs", "Jobs in the feed as short rows, best résumé fit first (sort: newest for most recent). "
         "Filter by company or title text, location text, minimum fit score (0-100), status (new, saved, applied, "
         "skip) or the last N days.",
         _schema({"query": {"type": "string"}, "location": {"type": "string"},
                  "min_fit": {"type": "number", "minimum": 0, "maximum": 100}, "status": {"type": "string"},
                  "since_days": _SINCE, "sort": {"type": "string", "enum": ["fit", "newest"]}, "limit": _LIMIT}),
         search_jobs, title="Search jobs"),
    Tool("get_job", "One job in full, by id or by its dashboard number: the posting text, fit summary, visa note, "
         "and any application to it. Pass description=false to leave the posting text out.",
         _schema({"job_id": {"type": "string", "description": "An id from a list, or the job number"},
                  "description": {"type": "boolean"}}, ["job_id"]), get_job, title="Job details"),
    Tool("today", "The curator's ranked list of jobs to apply to for a day (today by default), each with why it "
         "was picked.", _schema({"day": {"type": "string", "description": "YYYY-MM-DD"}, "limit": _LIMIT}),
         today, title="Today's picks"),
    Tool("list_resumes", "The base résumés (name and id). Pass tailored=true to include copies tailored to a job.",
         _schema({"tailored": {"type": "boolean"}}), list_resumes, title="List résumés"),
    Tool("get_resume", "A résumé's full content as plain text, by id or name. format=json returns the structured "
         "data instead.", _schema({"resume": {"type": "string", "description": "Id or name"},
                                   "format": {"type": "string", "enum": ["text", "json"]}}, ["resume"]),
         get_resume, title="Read a résumé"),
    Tool("get_profile", "Who the user is: contact details, work authorization, preferences and the networking "
         "profile (pitch, asks, schools and past employers, target roles, project names). Read it before writing "
         "anything in the user's voice. sections picks parts; projects (each project in full), writing, experience "
         f"and answers (saved application answers) are larger, ask for them only when needed: "
         f"{', '.join(PROFILE_SECTIONS)}.",
         _schema({"sections": {"type": "array", "items": {"type": "string", "enum": list(PROFILE_SECTIONS)}}}),
         get_profile, title="The user's profile"),
    Tool("list_leads", "People to reach out to for referrals, best first, as short rows. due=true gives those whose "
         f"next step is due. Filter by status ({_LEAD_STATUS}), kind (job, network, hiring_post, project_fit, "
         "alumni, founder), company or name text, or minimum score.",
         _schema({"due": {"type": "boolean"}, "status": {"type": "string"}, "kind": {"type": "string"},
                  "query": {"type": "string"}, "min_score": {"type": "integer", "minimum": 0, "maximum": 100},
                  "limit": _LIMIT}), list_leads, title="List leads"),
    Tool("get_lead", "One lead in full: why this person, talking points, the evidence that surfaced them, the "
         "project to show, drafted LinkedIn note and email, and the job they relate to.",
         _schema({"lead_id": {"type": "string"}}, ["lead_id"]), get_lead, title="Lead details"),
    Tool("update_lead", "Record what happened with a lead: move its status after a step was really taken "
         f"({_LEAD_STATUS}), save a drafted LinkedIn note (under 300 characters) or email, set talking points, or "
         "set the date of the next step.",
         _schema({"lead_id": {"type": "string"}, "status": {"type": "string"},
                  "linkedin_note": {"type": "string", "maxLength": 300}, "email_draft": {"type": "string"},
                  "email": {"type": "string"}, "talking_points": {"type": "array", "items": {"type": "string"}},
                  "next_action_at": {"type": "string", "description": "YYYY-MM-DD, or empty to clear"}},
                 ["lead_id"]), update_lead, write=True, title="Update a lead"),
    Tool("add_lead", "Record a person: someone worth reaching out to, or someone an agent or the user has "
         "already contacted. Safe to call again for the same person (matched by LinkedIn URL, email, or name and "
         "company): it returns the lead they already are, fills in what was missing and moves the status forward, "
         f"never back. status ({_LEAD_STATUS}) defaults to new; send requested or messaged with contacted_on once "
         "the message really went out, with the text sent as linkedin_note or email_draft. Give why in one line, "
         "job_id when it is about a specific job, and agent (your name) so the user can see who added it.",
         _schema({"name": {"type": "string"}, "company": {"type": "string"}, "title": {"type": "string"},
                  "linkedin_url": {"type": "string"}, "email": {"type": "string"}, "location": {"type": "string"},
                  "role": {"type": "string", "enum": ["founder", "hiring_manager", "recruiter", "engineer"]},
                  "kind": {"type": "string", "enum": ["job", "network", "hiring_post", "project_fit", "alumni",
                                                      "founder"]},
                  "status": {"type": "string"},
                  "contacted_on": {"type": "string", "description": "YYYY-MM-DD the step was taken; today if left out"},
                  "next_action_at": {"type": "string", "description": "YYYY-MM-DD of the follow-up"},
                  "why": {"type": "string"}, "evidence": {"type": "string"},
                  "talking_points": {"type": "array", "items": {"type": "string"}},
                  "linkedin_note": {"type": "string", "maxLength": 300}, "email_draft": {"type": "string"},
                  "show_project": {"type": "string", "description": "The project to show this person"},
                  "score": {"type": "integer", "minimum": 0, "maximum": 100},
                  "job_id": {"type": "string"},
                  "agent": {"type": "string", "description": "Who is reporting this, e.g. muse"}},
                 ["name"]), add_lead, write=True, title="Add or log a lead"),
    Tool("log_note", "Add a dated note to the search journal: a change made, a decision, a finding, an experiment "
         "and its result. One fact per note, with the numbers. topic is a short label (resume, curator, outreach, "
         "ats); agent is your name.",
         _schema({"note": {"type": "string"}, "topic": {"type": "string"}, "agent": {"type": "string"}}, ["note"]),
         log_note, write=True, title="Add a journal note"),
    Tool("update_application", "Update an application: move its status (applied, interview, offer, rejected), "
         "append a dated note, or set the next step and its date.",
         _schema({"application_id": {"type": "string"}, "status": {"type": "string"},
                  "append_note": {"type": "string"}, "next": {"type": "string"},
                  "next_date": {"type": "string", "description": "YYYY-MM-DD, or empty to clear"}},
                 ["application_id"]), update_application, write=True, title="Update an application"),
)

BY_NAME = {t.name: t for t in TOOLS}

INSTRUCTIONS = (
    "JobNavigator holds one person's job search: the jobs found for them, their applications, résumés, "
    "profile, and the people they are reaching out to for referrals. List tools return short rows with ids; "
    "call a get tool with an id for the full item. Call overview first for questions about progress. Before "
    "drafting outreach in the user's voice, read get_profile and the lead (get_lead). Change a lead's status "
    "only after the step was really taken."
)


def listing(can_write: bool) -> list:
    out = []
    for t in TOOLS:
        if t.write and not can_write:
            continue
        out.append({"name": t.name, "title": t.title or t.name, "description": t.description,
                    "inputSchema": t.schema,
                    "annotations": {"title": t.title or t.name, "readOnlyHint": not t.write,
                                    "destructiveHint": False, "idempotentHint": not t.write or t.name not in ("add_lead", "log_note"),
                                    "openWorldHint": False}})
    return out


def call(db, name: str, args, can_write: bool) -> str:
    """Run a tool and return its result as compact JSON text. Raises ToolError."""
    tool = BY_NAME.get(name)
    if tool is None or (tool.write and not can_write):
        raise ToolError(f"Unknown tool {name!r}" if tool is None
                        else f"{name} changes data and this connection is read-only")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    unknown = [k for k in args if k not in tool.schema["properties"]]
    if unknown:
        raise ToolError(f"Unknown argument(s) {', '.join(unknown)}; {name} takes: "
                        f"{', '.join(tool.schema['properties']) or 'none'}")
    missing = [k for k in tool.schema["required"] if args.get(k) in (None, "")]
    if missing:
        raise ToolError(f"Missing required argument(s): {', '.join(missing)}")
    return json.dumps(_lean(tool.fn(db, args)), ensure_ascii=False, separators=(",", ":"), default=str)
