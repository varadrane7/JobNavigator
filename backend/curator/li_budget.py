"""How much LinkedIn each signed-in session (mock / main) may still be asked today.

Every LinkedIn caller (curator people search, lead scout, employer-link lookup) goes through this:
a daily count per session and a cooldown after a 429, checkpoint or login wall, so a throttled
session is left alone and the next caller goes to the other one or waits. Keyed by the session's
cookie jar, so the owner, a member on 'inherit' and an install-level pass share one budget, and a
member with their own sessions has their own. Process-local: a restart forgets it.
"""
import logging
from datetime import datetime, timedelta, timezone

from backend.tz import local_today

logger = logging.getLogger("jobnavigator.curator.linkedin")

MOCK, MAIN = "mock", "main"
DEFAULT_CAP = {MOCK: 60, MAIN: 15}
DEFAULT_COOLDOWN_HOURS = 6

_USED: dict = {}     # (jar, date) -> requests made
_COOL: dict = {}     # jar -> aware datetime the cooldown ends


def _jar(which: str) -> str:
    from backend import refresh_linkedin_session as rls
    return rls.jar(which)


def _setting_int(key: str, default: int) -> int:
    from backend.analyzer.llm_client import _get_setting
    from backend.models.db import SessionLocal
    db = SessionLocal()
    try:
        try:
            return max(0, int(_get_setting(db, key, str(default))))
        except (TypeError, ValueError):
            return default
    finally:
        db.close()


def cap(which: str) -> int:
    return _setting_int(f"linkedin_{which}_daily_cap", DEFAULT_CAP[which])


def used(which: str) -> int:
    return _USED.get((_jar(which), local_today()), 0)


def cooling_until(which: str):
    until = _COOL.get(_jar(which))
    if until and until <= datetime.now(timezone.utc):
        _COOL.pop(_jar(which), None)
        return None
    return until


def available(which: str) -> bool:
    return cooling_until(which) is None and used(which) < cap(which)


def take(which: str) -> bool:
    """Count one request on this session; False (nothing counted) when it is cooling or spent."""
    if not available(which):
        return False
    key = (_jar(which), local_today())
    _USED[key] = _USED.get(key, 0) + 1
    return True


def cool(which: str, why: str = "rate-limited") -> None:
    hours = _setting_int("linkedin_cooldown_hours", DEFAULT_COOLDOWN_HOURS)
    _COOL[_jar(which)] = datetime.now(timezone.utc) + timedelta(hours=hours)
    logger.warning(f"LinkedIn {which} session {why}: left alone for {hours} h")


def status() -> dict:
    """{mock|main: {used, cap, cooling_until}} for the current account's sessions."""
    out = {}
    for which in (MOCK, MAIN):
        until = cooling_until(which)
        out[which] = {"used": used(which), "cap": cap(which),
                      "cooling_until": until.isoformat() if until else None}
    return out


def reset_all() -> None:
    """Tests: forget every count and cooldown."""
    _USED.clear()
    _COOL.clear()
