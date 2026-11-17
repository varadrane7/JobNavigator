"""The daily cap on LinkedIn connection requests.

Invites are sent by hand in LinkedIn, so nothing here refuses to record one. The cap decides how
many new leads the People screen and the Telegram nag put in front of the user as "send today"
(`remaining`), and a request recorded beyond it is accepted and reported as "over cap by N".

"Sent today" is the number of leads whose `invited_on` is today (the app's local day). `invited_on`
is set once, the first time a lead moves to `requested`, by `mark_invited`, which all three write
paths use: PATCH /contacts/{id}, the MCP `update_lead` tool, and `log_lead`.
"""
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import case

from backend.curator import audience
from backend.models.db import Contact
from backend.tz import local_today

DEFAULT_CAP = 8
MAX_CAP = 12


def clamp_cap(raw) -> int:
    """The setting's value as a usable cap: invalid, zero or negative -> 8; above 12 -> 12."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_CAP
    if value <= 0:
        return DEFAULT_CAP
    return min(value, MAX_CAP)


def cap(db) -> int:
    from backend.accounts.settings import get_setting
    return clamp_cap(get_setting(db, "invites_daily_cap"))


def mark_invited(contact: Contact, new_status: Optional[str], on: Optional[date] = None) -> bool:
    """Call when `contact` is moved to `new_status`. Sets `invited_on` (to `on`, default today) the
    first time the lead becomes `requested`; never changes it afterwards. Returns True when set."""
    if new_status == "requested" and contact.invited_on is None:
        contact.invited_on = on or local_today()
        return True
    return False


def sent_today(db, today: Optional[date] = None) -> int:
    return db.query(Contact).filter(Contact.invited_on == (today or local_today())).count()


def state(db, today: Optional[date] = None) -> dict:
    """{sent_today, cap, remaining, over_cap_by}: what the People chip, the nag and the tools show."""
    today = today or local_today()
    limit, sent = cap(db), sent_today(db, today)
    return {"sent_today": sent, "cap": limit, "remaining": max(0, limit - sent), "over_cap_by": max(0, sent - limit)}


DEFAULT_AGEING_DAYS = 7


def ageing_days(db) -> int:
    """Setting due_ageing_days: a lead due longer than this comes ahead of newer ones. Invalid or zero -> 7."""
    from backend.accounts.settings import get_setting
    try:
        value = int(str(get_setting(db, "due_ageing_days")).strip())
    except (TypeError, ValueError):
        return DEFAULT_AGEING_DAYS
    return value if value > 0 else DEFAULT_AGEING_DAYS


# The one order of the due list: a lead that has been due longer than the ageing days first (so a low score or an
# unknown audience cannot wait for ever), then the owner's audience priority (alumni, former colleagues, engineers,
# recruiters, people who posted, hiring managers, unknown last: curator/audience.py), then the best score, then the
# lead that has waited longest, ending on id so the order is stable. The People screen, the MCP tools, the nag and
# the "send today" choice all use it. Follow-ups are never held back by the invite cap: it only splits the new leads.
def due_order(db=None, today: Optional[date] = None) -> tuple:
    today = today or local_today()
    cutoff = today - timedelta(days=ageing_days(db) if db is not None else DEFAULT_AGEING_DAYS)
    return (case((Contact.next_action_at <= cutoff, 0), else_=1).asc(),
            audience.order_expr(Contact.audience).asc(), Contact.score.desc().nullslast(),
            Contact.next_action_at.asc(), Contact.found_on.asc().nullslast(), Contact.created_at.asc(), Contact.id.asc())


def due_new_leads(db, today: Optional[date] = None) -> list:
    """New leads waiting for a first request, in the order they should be sent (due_order)."""
    today = today or local_today()
    return (db.query(Contact)
            .filter(Contact.status == "new", Contact.next_action_at.isnot(None), Contact.next_action_at <= today)
            .order_by(*due_order(db, today)).all())


def plan(db, today: Optional[date] = None) -> dict:
    """Which due new leads are "send today" and which carry over. Nothing is written: the carried
    leads keep their due date, so tomorrow they come up again, still in priority order."""
    st = state(db, today)
    leads = due_new_leads(db, today)
    send = leads[: st["remaining"]]
    carried = leads[st["remaining"]:]
    return {**st, "send": [c.id for c in send], "carried": [c.id for c in carried]}
