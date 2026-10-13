"""The app's one time zone. Day boundaries, cron schedules, the Postgres session
and the dates the UI shows all use APP_TZ, so "today" means the same thing
everywhere. Set TZ in .env to change it; default America/New_York."""
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "America/New_York"


def _zone_name() -> str:
    name = (os.getenv("APP_TZ") or os.getenv("TZ") or DEFAULT_TZ).strip().lstrip(":")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return DEFAULT_TZ
    return name


APP_TZ_NAME = _zone_name()
APP_TZ = ZoneInfo(APP_TZ_NAME)


def local_now() -> datetime:
    return datetime.now(APP_TZ)


def local_today() -> date:
    return local_now().date()


def day_start(d: date | None = None) -> datetime:
    """Midnight of `d` (default today) in APP_TZ, as an aware UTC datetime for DB filters."""
    d = d or local_today()
    return datetime.combine(d, time.min, tzinfo=APP_TZ).astimezone(timezone.utc)


def day_bounds(d: date | None = None) -> tuple[datetime, datetime]:
    d = d or local_today()
    return day_start(d), day_start(d + timedelta(days=1))
