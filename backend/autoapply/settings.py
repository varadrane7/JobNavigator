"""autoapply_* settings: stored as rows in the shared `settings` table, but declared, validated and
written only here (upstream PATCH /api/settings rejects keys it does not know)."""
import json
import logging

from backend.models.db import Setting

logger = logging.getLogger("jobnavigator.autoapply.settings")

DEFAULT_SITE_CAPS = {"greenhouse": 40, "lever": 40, "ashby": 40, "workday": 40, "generic": 40}

# Local model first for volume, subscriptions for quality; each tier falls back to the others.
# Light models on purpose: an empty model lets Claude Code / Antigravity pick their own default, which
# is a heavy one and burns the subscription's limit in a handful of applications.
DEFAULT_CHEAP = [{"provider": "openai_local", "model": ""},
                 {"provider": "antigravity_cli", "model": "gemini-3.8-flash-low"},
                 {"provider": "claude_code", "model": "claude-haiku-4-5", "effort": "low"}]
DEFAULT_QUALITY = [{"provider": "claude_code", "model": "claude-sonnet-5", "effort": "medium"},
                   {"provider": "antigravity_cli", "model": "gemini-3.8-flash-medium"},
                   {"provider": "openai_local", "model": ""}]
# What seed() wrote before the chains named models; a stored value still equal to it was never edited.
_OLD_DEFAULTS = {
    "autoapply_llm_cheap": [{"provider": "openai_local", "model": ""}, {"provider": "antigravity_cli", "model": ""},
                            {"provider": "claude_code", "model": ""}],
    "autoapply_llm_quality": [{"provider": "claude_code", "model": ""}, {"provider": "antigravity_cli", "model": ""},
                              {"provider": "openai_local", "model": ""}],
}

TAILOR_PROMPT = """Tailor ONLY the summary and the skills of this resume to the job posting below.

The full resume is shown for context. Every fact you may use is in it.

FULL RESUME:
{resume_text}

SECTIONS YOU MAY CHANGE (JSON):
{sections_json}

JOB POSTING:
{job_description}

Rules:
- summary: 2-4 sentences aimed at this role, using the posting's vocabulary where it is truthful. Use only facts, numbers and technologies that appear in the full resume. Do not change any number (years, sizes, percentages) or add new ones.
- skills: keep the same JSON shape (same type of object or list). Reorder categories and items so the posting's skills come first. You may drop low-relevance items or rename a category label. Never add a skill that does not appear somewhere in the full resume.
- Plain ASCII, regular hyphens, no em-dashes.
- Do not mention the company name or the job title in the summary.

Return ONLY this JSON: {"summary": "...", "skills": <same shape as input skills>}"""

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
        "json", ["greenhouse", "lever", "ashby", "workday", "generic"],
        "ATS kinds allowed to receive jobs (greenhouse, lever, ashby, workday, generic = any other company site)"),
    "autoapply_extra_aggregators": ("json", [], "Extra job-board domains to leave out of the queue, on top of the built-in list (LinkedIn, ZipRecruiter, Indeed, ...)"),
    "autoapply_engine": ("enum:agent|builtin", "agent", "agent = the applier service (Claude Code driving Chromium) fills every form; builtin = the old scripted fillers"),
    "autoapply_applier_url": ("str", "http://applier:8080", "Where the applier service listens (docker-compose.autoapply.yml starts it)"),
    "autoapply_agent_model": ("str", "", "Claude model for the applier (sonnet, opus, haiku or a full id); empty = the applier's APPLIER_MODEL"),
    "autoapply_agent_timeout_s": ("int", 600, "Longest the agent may spend on one application"),
    "autoapply_agent_max_turns": ("int", 60, "Most agent turns per application (each turn is one or more browser actions)"),
    "autoapply_agent_autofill": ("bool", True, "Load the Navigator extension into the applier's Chromium so known fields (contact, work auth, EEO) fill without the model, and give the agent a draft_answer tool that writes free-text answers with the dashboard's own autofill LLM"),
    "autoapply_agent_backend_url": ("str", "http://backend:8000", "How the applier reaches this backend (for the extension and draft_answer)"),
    "autoapply_agent_instructions": ("str", "", "Extra instructions appended to every agent run (e.g. salary expectations, preferred start date)"),
    "autoapply_live_view_url": ("str", "/applier/vnc.html?autoconnect=1&resize=scale&path=applier/websockify", "Link to watch the applier's browser (needs APPLIER_VNC_PASSWORD)"),
    "autoapply_queue_cron": ("cron", "15 6 * * *", "When the daily queue is built (5-field cron, server time)"),
    "autoapply_jitter_min_s": ("int", 45, "Shortest wait between two submits"),
    "autoapply_jitter_max_s": ("int", 180, "Longest wait between two submits"),
    "autoapply_quiet_hours": ("str", "23-7", "Hours (start-end, local) with no submits; empty = none"),
    "autoapply_timezone": ("str", "America/New_York", "Time zone for quiet hours and the daily submit count"),
    "autoapply_submit_timeout_s": ("int", 420, "Longest one application may take before it is abandoned"),
    "autoapply_cover_letter": ("enum:off|if_field|always", "if_field", "When a cover letter is generated"),
    "autoapply_allow_new_skills": ("bool", False, "Let tailoring add skills that appear nowhere in the base résumé"),
    "autoapply_cdp_url": ("str", "http://host.docker.internal:9223", "Chrome DevTools endpoint of your logged-in Chrome (see docs/AUTOAPPLY.md)"),
    "autoapply_cdp_jitter_min_s": ("int", 90, "Shortest wait after a LinkedIn / ZipRecruiter submit"),
    "autoapply_cdp_jitter_max_s": ("int", 300, "Longest wait after a LinkedIn / ZipRecruiter submit"),
    "autoapply_site_pause_hours": ("int", 24, "How long a site is left alone after it shows a checkpoint"),
    "autoapply_paused_until": ("json", {}, "Sites paused after a checkpoint: {site: ISO time}"),
    # Under the ./backend bind mount, so files survive a container rebuild with no compose change.
    "autoapply_data_dir": ("str", "/app/backend/autoapply_data", "Where PDFs and screenshots are written"),
    "autoapply_llm_cheap": ("json", DEFAULT_CHEAP, "Form answers, many per application: pick light models. Priority 1 is tried first; the next takes over while one is at its limit or erroring. Stored as [{provider, model, effort?}]"),
    "autoapply_llm_quality": ("json", DEFAULT_QUALITY, "Tailoring, cover letters and free-text answers, a few per application. Same fallback order as the cheap tier"),
    "autoapply_llm_quota_cooldown_min": ("int", 300, "How long a provider that hit its usage limit is skipped"),
    "autoapply_llm_error_cooldown_min": ("int", 5, "How long a provider that errored is skipped"),
    "autoapply_llm_cooldowns": ("json", {}, "Providers cooling down: {provider: ISO time}"),
    "autoapply_tailor_prompt": ("str", TAILOR_PROMPT, "Prompt for the summary + skills rewrite ({resume_text}, {sections_json}, {job_description})"),
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
    rows = {r.key: r for r in db.query(Setting).filter(Setting.key.in_(list(SPEC))).all()}
    for key, (kind, default, desc) in SPEC.items():
        if key not in rows:
            db.add(Setting(key=key, value=_dump(kind, default), description=desc))
        elif key in _OLD_DEFAULTS:
            try:
                untouched = _parse(kind, rows[key].value) == _OLD_DEFAULTS[key]
            except (ValueError, TypeError):
                untouched = False
            if untouched:
                rows[key].value = _dump(kind, default)
    db.commit()
