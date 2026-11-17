"""Follow-ups: two nudges at day 4 and day 9 after the first contact, then a wait, then stop.

A lead with an open request (`requested`) or a message sent (`messaged`) and no answer is due for follow-up n
when n-1 follow-ups have been sent and `followup_days[n-1]` days have passed since the first contact. After
`followup_max` follow-ups the lead waits `followup_close_after_days` for a reply; then it is closed as no
reply: status `skip` (the pipeline's "dropped" status, the one the People screen's "No reply, drop" button
sets) with `message_stage` "closed_no_reply", and it stops appearing as due. A lead whose first contact is
older than the last follow-up day plus the close wait is closed the same way without further follow-ups
(that is how leads already open before this schedule existed enter it, with no flood on the first day).
A reply, an acceptance or any other move out of requested/messaged stops the sequence (acceptance leads to
the after-acceptance message instead, due the day it is accepted). Sending is by hand, so a follow-up is
recorded with `record_sent`; it never touches `invited_on`, so follow-ups do not count against the invite cap.

Reading never writes: `info`, `is_due` and `due_leads` are pure, so the Due list and the read-only tools
compute the state themselves; only `mark_due` (the scheduled scout job) writes the due date and closes leads.

Settings (per profile):
  followup_max               follow-ups per lead, default 2, 0 turns them off, at most 5; anything else counts as 2
  followup_days              days after the first contact when each is due, default "4,9": positive whole numbers
                             that rise strictly; anything else counts as the default. If there are fewer days than
                             followup_max, only that many follow-ups happen.
  followup_close_after_days  wait after the last follow-up before closing, default 5 (0 to 60; anything else counts as 5)
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple, Optional

from sqlalchemy import or_

from backend.models.db import Contact, utcnow
from backend.tz import APP_TZ, local_today

logger = logging.getLogger("jobnavigator.curator.followups")

DEFAULT_MAX = 2
DEFAULT_DAYS = (4, 9)
DEFAULT_CLOSE_AFTER = 5
CEILING = 5
MAX_DAY = 60
ACTIVE = ("requested", "messaged")        # statuses with a first contact made and no answer yet
CLOSED = "closed_no_reply"


class Schedule(NamedTuple):
    count: int          # follow-ups per lead (already cut to the number of days)
    days: tuple         # days after the first contact
    close_after: int    # days to wait after the last follow-up before closing
    daily_limit: int = 8   # follow-ups the Due list shows in one day, so leads open before this schedule do not arrive at once


DEFAULT_DAILY_LIMIT = 8
DEFAULT_SCHEDULE = Schedule(DEFAULT_MAX, DEFAULT_DAYS, DEFAULT_CLOSE_AFTER, DEFAULT_DAILY_LIMIT)


def parse_max(raw) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX
    return DEFAULT_MAX if value < 0 else min(value, CEILING)


def parse_days(raw) -> tuple:
    """"4,9" -> (4, 9). Anything that is not positive whole numbers rising strictly, each at most 60, is the default."""
    try:
        days = tuple(int(x.strip()) for x in str(raw).split(","))
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    if not days or any(d <= 0 or d > MAX_DAY for d in days) or any(b <= a for a, b in zip(days, days[1:])):
        return DEFAULT_DAYS
    return days


def parse_close_after(raw) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_CLOSE_AFTER
    return value if 0 <= value <= MAX_DAY else DEFAULT_CLOSE_AFTER


def parse_daily_limit(raw) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT
    return value if 1 <= value <= 100 else DEFAULT_DAILY_LIMIT


def schedule(db) -> Schedule:
    """The follow-up schedule in force for the current profile: the number is cut to the days given."""
    from backend.accounts.settings import get_setting
    fmax = parse_max(get_setting(db, "followup_max"))
    days = parse_days(get_setting(db, "followup_days"))
    return Schedule(min(fmax, len(days)), days, parse_close_after(get_setting(db, "followup_close_after_days")),
                    parse_daily_limit(get_setting(db, "followup_daily_limit")))


def local_date(dt: Optional[datetime]) -> Optional[date]:
    """The app-time-zone calendar day of a stored timestamp (naive ones are UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(APP_TZ).date()


def anchor(c: Contact) -> Optional[date]:
    """The day the follow-ups count from: the request or message that started the sequence."""
    if c.contacted_on:
        return c.contacted_on
    if c.status == "requested" and c.invited_on:
        return c.invited_on
    return local_date(c.last_action_at) or c.found_on


def info(c: Contact, today: Optional[date] = None, sched: Optional[Schedule] = None) -> Optional[dict]:
    """The follow-up state of a lead, or None when it is not in a sequence (not requested/messaged, or
    follow-ups are off). {"n", "of", "due", "due_on", "label"} while follow-ups remain;
    {"waiting": True, "closes_on"} after the last one until the close wait is over; {"closed": True} after."""
    if c.status not in ACTIVE:
        return None
    count, days, close_after = (sched or DEFAULT_SCHEDULE)[:3]
    if count <= 0:
        return None
    start = anchor(c)
    if start is None:
        return None
    today = today or local_today()
    sent = c.follow_ups_sent or 0
    if sent >= count:
        closes_on = (c.follow_up_sent_on or start + timedelta(days=days[count - 1])) + timedelta(days=close_after)
        if today >= closes_on:
            return {"n": count, "of": count, "due": False, "closed": True, "label": f"{count} follow-ups sent, no reply"}
        return {"n": count, "of": count, "due": False, "waiting": True, "closes_on": closes_on.isoformat(),
                "label": f"{count} follow-ups sent, closes {closes_on.isoformat()}"}
    if (today - start).days > days[-1] + close_after:     # open since before this schedule, long past its last day
        return {"n": sent + 1, "of": count, "due": False, "closed": True, "label": "no reply, too old to follow up"}
    due_on = start + timedelta(days=days[sent])
    due = today >= due_on
    n = sent + 1
    return {"n": n, "of": count, "due": due, "due_on": due_on.isoformat(),
            "label": f"follow-up {n} of {count} due" if due else f"follow-up {n} of {count} on {due_on.isoformat()}"}


def is_due(c: Contact, today: Optional[date] = None, sched: Optional[Schedule] = None) -> bool:
    """Whether the lead belongs on today's Due list. Pure."""
    today = today or local_today()
    if c.status in ("skip", "referral"):
        return False
    if c.status in ACTIVE:
        i = info(c, today, sched)
        if i is not None and (i.get("closed") or i.get("waiting")):
            return False
        if i is not None and i["due"]:
            return True
    if c.next_action_at is not None and c.next_action_at <= today:
        return True
    return c.status == "connected" and c.next_action_at is None      # accepted: the first message is due now


def due_leads(db, today: Optional[date] = None, order=None) -> list:
    """The leads due today, read-only, in `order` (invites.due_order(db) by default). Follow-ups (leads waiting on a
    reply) are shown up to `followup_daily_limit` a day, counting those already recorded today, so that leads open
    before the schedule existed come up over several days, not all at once; they are never held back by the invite cap."""
    from backend.curator import invites
    today = today or local_today()
    sched = schedule(db)
    q = (db.query(Contact).filter(Contact.status.notin_(("skip", "referral")))
         .filter(or_(Contact.status.in_(ACTIVE + ("connected",)), Contact.next_action_at <= today)))
    rows = [c for c in q.order_by(*(order or invites.due_order(db, today))).all() if is_due(c, today, sched)]
    room = max(0, sched.daily_limit - db.query(Contact).filter(Contact.follow_up_sent_on == today).count())
    out = []
    for c in rows:
        if c.status in ACTIVE:
            if room <= 0:
                continue
            room -= 1
        out.append(c)
    return out


def next_stage(c: Contact, sched: Optional[Schedule] = None) -> Optional[str]:
    """The stage of the text this lead needs next: note, after_acceptance, follow_up_<n>, or closed_no_reply
    (None while waiting out the close wait: nothing is left to send)."""
    if c.status == "new":
        return "note"
    if c.status == "connected":
        return "after_acceptance"
    if c.status in ACTIVE:
        i = info(c, sched=sched)
        if i is None or i.get("waiting"):
            return None
        return CLOSED if i.get("closed") else f"follow_up_{i['n']}"
    return None


def on_status_change(c: Contact, new_status: Optional[str], on: Optional[date] = None, sched: Optional[Schedule] = None) -> None:
    """Call when `c` has just moved to `new_status` (all three write paths: PATCH, the MCP update_lead, log_lead).
    Entering requested or messaged starts a sequence (the first contact is `on`, default today, the count of
    follow-ups sent starts again, and the text sent is remembered: the note, or the drafted message); acceptance,
    a reply and the rest stop it."""
    if new_status in ACTIVE:
        c.contacted_on = on or local_today()
        c.follow_ups_sent = 0
        c.follow_up_sent_on = None
        c.last_sent_text = (c.note if new_status == "requested" else c.message_draft) or None
    if new_status in ACTIVE or new_status in ("connected", "new"):
        c.message_draft = None            # a draft written for the earlier stage is stale
    c.message_stage = next_stage(c, sched)


def previous_text(c: Contact) -> Optional[str]:
    """The text last actually sent to this lead, which a follow-up refers to: the one recorded when the request or
    message went out or the last follow-up was recorded; for a request sent before this was recorded, the note."""
    if c.last_sent_text:
        return c.last_sent_text
    if c.status == "requested" and not (c.follow_ups_sent or 0) and c.note:
        return c.note
    return None


def record_sent(c: Contact, sched: Optional[Schedule] = None, today: Optional[date] = None) -> int:
    """The owner sent follow-up n. Counts it, clears what was due, remembers the text sent when it was drafted
    here; returns follow-ups sent. A second call the same day is the same follow-up (a double click or a retry)
    and changes nothing. Raises ValueError when the lead is not in a sequence, none is left, or none is due yet."""
    today = today or local_today()
    i = info(c, today, sched)
    sent = c.follow_ups_sent or 0
    if sent >= 1 and c.follow_up_sent_on == today:
        return sent
    if i is None:
        raise ValueError("this lead is not waiting for a follow-up")
    if i.get("closed") or i.get("waiting"):
        raise ValueError("all follow-ups were already sent" if i.get("waiting") or sent else "too old to follow up")
    if not i["due"]:
        raise ValueError(f"follow-up {i['n']} is not due until {i['due_on']}")
    c.follow_ups_sent = sent + 1
    c.follow_up_sent_on = today
    c.last_action_at = utcnow()
    c.next_action_at = None
    if c.message_draft:
        c.last_sent_text = c.message_draft
    c.message_draft = None
    c.message_stage = next_stage(c, sched)
    return c.follow_ups_sent


def close(c: Contact) -> None:
    c.status = "skip"
    c.message_stage = CLOSED
    c.message_draft = None
    c.next_action_at = None


def mark_due(db, today: Optional[date] = None, commit: bool = True) -> int:
    """The scheduled pass (the lead scout's run) and the only place that writes: closes leads that have had every
    follow-up and the close wait, and puts a lead whose follow-up has come due (or that accepted and has not been
    messaged) on today's list. Returns how many are due."""
    today = today or local_today()
    sched = schedule(db)
    n = 0
    for c in db.query(Contact).filter(Contact.status.in_(ACTIVE + ("connected",))).all():
        if c.status == "connected":       # accepted: the first message is due now, whatever the follow-up days are
            if c.next_action_at is None:
                c.next_action_at = today
                n += 1
            continue
        i = info(c, today, sched)
        if i is None or i.get("waiting"):
            continue
        if i.get("closed"):
            close(c)
            continue
        if i["due"]:
            n += 1
            if c.next_action_at is None or c.next_action_at > today:
                c.next_action_at = today
    if commit:
        db.commit()
    return n
