"""Turn a board's "posted" value into an aware datetime for Job.posted_at.

Boards give an ISO timestamp (freehire), a bare date (JobSpy/LinkedIn), or a phrase
("3 hours ago", jobright). A bare date carries no time, so a date equal to the day we
found the job uses the discovery time; an older date means the job was up before we
saw it and gets noon of that day in APP_TZ.
"""
import re
from datetime import date, datetime, time, timedelta, timezone

from backend.tz import APP_TZ

_REL = re.compile(r"(\d+)\s*(minute|min|hour|hr|day|week|month)s?\b", re.I)
_UNIT = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800, "month": 2592000}


def parse_posted(value, discovered: datetime | None = None) -> datetime | None:
    now = discovered or datetime.now(timezone.utc)
    if value is None:
        return None
    try:  # pandas NaT / NaN
        if value != value:
            return None
    except Exception:
        pass
    if hasattr(value, "to_pydatetime"):          # pandas Timestamp
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=APP_TZ)
        return min(dt.astimezone(timezone.utc), now)
    if isinstance(value, date):
        if value >= now.astimezone(APP_TZ).date():
            return now
        return datetime.combine(value, time(12), tzinfo=APP_TZ).astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    low = text.lower()
    if low in ("just now", "today", "now") or "just posted" in low:
        return now
    if low.startswith("yesterday"):
        return now - timedelta(days=1)
    m = _REL.search(low)
    if m and "ago" in low:
        return now - timedelta(seconds=int(m.group(1)) * _UNIT[m.group(2).lower()])
    try:
        iso = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if len(text) <= 10:
        return parse_posted(iso.date(), now)
    return parse_posted(iso, now)
