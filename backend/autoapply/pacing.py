"""When the next submission may happen, and which item it is: quiet hours, a jittered gap
between submits, global / per-site daily caps on actual submissions, and never the same company
twice in a row."""
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.autoapply.models import AutoApplyItem

# Submitters with a working implementation; others stay 'ready' until theirs lands.
IMPLEMENTED = {"greenhouse", "lever", "ashby", "generic", "workday", "linkedin", "ziprecruiter"}
# Driven in the user's own Chrome: slower pacing, pause on checkpoints.
CDP_SITES = {"linkedin", "ziprecruiter"}


def paused_sites(cfg, now: datetime) -> set:
    out = set()
    for site, until in (cfg.get("autoapply_paused_until") or {}).items():
        try:
            t = datetime.fromisoformat(str(until))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if t > now:
            out.add(site)
    return out


_state = {"next_at": None, "last_company": None}


def reset():
    _state.update(next_at=None, last_company=None)


def _tz(cfg) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.get("autoapply_timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def in_quiet_hours(cfg, now: datetime) -> bool:
    raw = (cfg.get("autoapply_quiet_hours") or "").strip()
    if not raw:
        return False
    try:
        a, b = (int(x) for x in raw.split("-", 1))
    except ValueError:
        return False
    h = now.astimezone(_tz(cfg)).hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def local_day_bounds(cfg, now: datetime):
    local = now.astimezone(_tz(cfg))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def submitted_today(db, cfg, now) -> dict:
    lo, hi = local_day_bounds(cfg, now)
    counts = {"_total": 0}
    for (sub,) in db.query(AutoApplyItem.submitter).filter(
            AutoApplyItem.status == "submitted", AutoApplyItem.submitted_at >= lo, AutoApplyItem.submitted_at < hi):
        counts[sub] = counts.get(sub, 0) + 1
        counts["_total"] += 1
    return counts


def pick_next(db, cfg, now: datetime, implemented=None):
    """The next ready item allowed to go now, or (None, reason)."""
    implemented = implemented or IMPLEMENTED
    if in_quiet_hours(cfg, now):
        return None, "quiet hours"
    if _state["next_at"] and now < _state["next_at"]:
        return None, "waiting for jitter gap"
    done = submitted_today(db, cfg, now)
    if done["_total"] >= int(cfg["autoapply_daily_cap"]):
        return None, "daily cap reached"
    caps = cfg.get("autoapply_site_caps") or {}
    rows = (db.query(AutoApplyItem)
            .filter(AutoApplyItem.status == "ready")
            .order_by(AutoApplyItem.run_date.desc(), AutoApplyItem.rank.asc()).all())
    paused = paused_sites(cfg, now)
    fallback = None
    for it in rows:
        if it.submitter not in implemented or it.submitter in paused:
            continue
        na = it.next_attempt_at
        if na is not None:
            if na.tzinfo is None:
                na = na.replace(tzinfo=timezone.utc)
            if na > now:
                continue
        if done.get(it.submitter, 0) >= int(caps.get(it.submitter, caps.get("generic", 0)) or 0):
            continue
        if it.company_key and it.company_key == _state["last_company"]:
            fallback = fallback or it
            continue
        return it, None
    if fallback:
        return fallback, None
    return None, "nothing ready"


def mark_done(cfg, item, now: datetime) -> None:
    if item.submitter in CDP_SITES:
        lo, hi = int(cfg["autoapply_cdp_jitter_min_s"]), int(cfg["autoapply_cdp_jitter_max_s"])
    else:
        lo, hi = int(cfg["autoapply_jitter_min_s"]), int(cfg["autoapply_jitter_max_s"])
    _state["next_at"] = now + timedelta(seconds=random.uniform(lo, max(lo, hi)))
    _state["last_company"] = item.company_key
