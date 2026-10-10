"""autoapply_* settings: stored as rows in the shared `settings` table, but declared, validated and
written only here (upstream PATCH /api/settings rejects keys it does not know)."""
import json
import logging

from backend.models.db import Setting

logger = logging.getLogger("jobnavigator.autoapply.settings")

DEFAULT_SITE_CAPS = {
    "linkedin": 15, "ziprecruiter": 10,
    "greenhouse": 40, "lever": 40, "ashby": 40, "workday": 40, "generic": 40,
}

# key -> (kind, default, description). kind: int | bool | str | json | cron | enum:<a>|<b>
SPEC = {
    "autoapply_enabled": ("bool", False, "Master switch: build the daily queue and run the submit worker"),
    "autoapply_mode": ("enum:review|auto", "review", "review = approve each batch in the UI; auto = queue goes straight to prepare/submit"),
    "autoapply_dry_run": ("bool", True, "Fill forms and screenshot, but never click submit"),
    "autoapply_daily_cap": ("int", 100, "Most jobs queued per day"),
    "autoapply_min_score": ("int", 70, "Lowest best-résumé score (0-100) a job needs to be queued"),
    "autoapply_max_age_days": ("int", 14, "Only jobs discovered within this many days"),
    "autoapply_score_budget": ("int", 150, "Most unscored candidates scored per queue build"),
    "autoapply_resume_ids": ("json", [], "Base résumé ids to pick from; empty = every base résumé"),
    "autoapply_site_caps": ("json", DEFAULT_SITE_CAPS, "Per-submitter daily caps"),
    "autoapply_company_daily_cap": ("int", 2, "Most jobs per company per day"),
    "autoapply_company_blocklist": ("json", [], "Company names never auto-applied to (case-insensitive)"),
    "autoapply_title_blocklist": ("json", [], "Title substrings never auto-applied to (case-insensitive)"),
    "autoapply_enabled_submitters": (
        "json", ["greenhouse", "lever", "ashby", "generic", "linkedin", "ziprecruiter"],
        "Submitters allowed to receive jobs (workday is added once its handler exists)"),
    "autoapply_queue_cron": ("cron", "15 6 * * *", "When the daily queue is built (5-field cron, server time)"),
    "autoapply_jitter_min_s": ("int", 45, "Shortest wait between two submits"),
    "autoapply_jitter_max_s": ("int", 180, "Longest wait between two submits"),
    "autoapply_quiet_hours": ("str", "23-7", "Hours (start-end, local) with no submits; empty = none"),
    "autoapply_cover_letter": ("enum:off|if_field|always", "if_field", "When a cover letter is generated"),
    "autoapply_allow_new_skills": ("bool", False, "Let tailoring add skills that appear nowhere in the base résumé"),
    "autoapply_cdp_url": ("str", "http://host.docker.internal:9222", "Chrome DevTools endpoint of your logged-in Chrome"),
    "autoapply_data_dir": ("str", "/app/autoapply_data", "Where PDFs and screenshots are written"),
}


def _parse(kind: str, raw):
    """Turn a stored string (or an incoming JSON value) into the typed value; raises ValueError."""
    if kind == "int":
        v = int(str(raw).strip())
        if v < 0:
            raise ValueError("must be >= 0")
        return v
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off", ""):
            return False
        raise ValueError("not a boolean")
    if kind == "json":
        return json.loads(raw) if isinstance(raw, str) else raw
    if kind == "cron":
        s = str(raw or "").strip()
        if s and len(s.split()) != 5:
            raise ValueError("cron needs 5 fields")
        if s:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(s)
        return s
    if kind.startswith("enum:"):
        s = str(raw).strip()
        if s not in kind[5:].split("|"):
            raise ValueError(f"must be one of {kind[5:]}")
        return s
    return "" if raw is None else str(raw)


def _dump(kind: str, value) -> str:
    if kind == "json":
        return json.dumps(value)
    if kind == "bool":
        return "true" if value else "false"
    return str(value)


def get_all(db) -> dict:
    """Every autoapply setting, typed; a junk stored value falls back to its default."""
    rows = {r.key: r.value for r in db.query(Setting).filter(Setting.key.like("autoapply\\_%", escape="\\")).all()}
    out = {}
    for key, (kind, default, _desc) in SPEC.items():
        if key not in rows:
            out[key] = default
            continue
        try:
            out[key] = _parse(kind, rows[key])
        except (ValueError, TypeError) as e:
            logger.error(f"Setting {key}={rows[key]!r} is invalid ({e}); using default")
            out[key] = default
    return out


def get(db, key):
    return get_all(db)[key]


def update(db, updates: dict) -> dict:
    """Validate and write; raises ValueError naming every bad key, writing nothing in that case."""
    errors, parsed = {}, {}
    for key, value in updates.items():
        if key not in SPEC:
            errors[key] = "unknown setting"
            continue
        try:
            parsed[key] = _parse(SPEC[key][0], value)
        except (ValueError, TypeError) as e:
            errors[key] = str(e)
    if errors:
        raise ValueError(errors)
    for key, value in parsed.items():
        kind, _default, desc = SPEC[key]
        row = db.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = _dump(kind, value)
        else:
            db.add(Setting(key=key, value=_dump(kind, value), description=desc))
    db.commit()
    return get_all(db)


def seed(db) -> None:
    """Insert missing defaults; never overwrites a value the user set."""
    existing = {r.key for r in db.query(Setting).filter(Setting.key.in_(list(SPEC))).all()}
    for key, (kind, default, desc) in SPEC.items():
        if key not in existing:
            db.add(Setting(key=key, value=_dump(kind, default), description=desc))
    db.commit()
