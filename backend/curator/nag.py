"""The nag: Telegram messages through the day that count what the user has actually done today
and say it bluntly. The user asked for this ("I work best on spite"), so the tone is harsh on
purpose; it is never about anything but the work.

Each message reads today's numbers for the current account: curated picks left, applications
sent, connection requests sent, People actions due, and the 7-day streak. Targets are
`nag_apply_target` and `nag_request_target`; `nag_calendar` (JSON {"YYYY-MM-DD": "text"}) adds
the day's planned post or task (the online presence plan). Scheduled by `nag_cron`.
"""
import json
import logging
import os
import random
from datetime import timedelta

from backend.models.db import Application, Contact, Job, SessionLocal
from backend.tz import day_start, local_now, local_today

logger = logging.getLogger("jobnavigator.nag")

MORNING = [
    "Morning. {picks} picks are waiting and you have sent {apps}. 462 applications got you one interview, so don't treat today like a day off.",
    "{picks} jobs on Today and {due} people due on People. Nobody is going to refer someone they've never heard from.",
    "New day, same scoreboard: {apps} applications, {reqs} requests. The people who get hired this month are the ones who show up this morning.",
]
MIDDAY_BEHIND = [
    "Half the day is gone. {apps}/{apply_t} applied, {reqs}/{req_t} requests. Someone with your résumé and a referral is ahead of you right now.",
    "{apps} applications by now. That isn't a job search, it's a hobby. Open Today and do the next one.",
    "You have {picks} picks left and {due} people due. Every hour you don't send them, the posting gets 50 more applicants.",
]
EVENING_BEHIND = [
    "{apps}/{apply_t} applied, {reqs}/{req_t} requests. Your visa clock is still running even when you aren't.",
    "Tonight's count: {apps} applications, {reqs} requests. A single interview in three months says the old way failed. The new way only works if you do it.",
    "Still behind: {left_apps} applications and {left_reqs} requests short. Close whatever you're watching and finish the list.",
]
DONE = [
    "Targets hit: {apps} applied, {reqs} requests. Fine. One day isn't a habit; do it again tomorrow.",
    "{apps} applications and {reqs} requests. That's the bar, not an achievement. Streak: {streak}/7 days.",
]
NIGHT = [
    "Final count for {date}: {apps} applied, {reqs} requests, {due} people still due. Streak {streak}/7. Tomorrow starts at 9.",
]


def stats(today=None) -> dict:
    today = today or local_today()
    start = day_start(today)
    db = SessionLocal()
    try:
        picks = db.query(Job).filter(Job.curated_on == today, Job.status == "saved").count()
        apps = db.query(Application).filter(Application.applied_at >= start).count()
        reqs = db.query(Contact).filter(Contact.last_action_at >= start,
                                        Contact.status.in_(("requested", "connected", "messaged", "replied", "referral"))).count()
        due = db.query(Contact).filter(Contact.next_action_at.isnot(None), Contact.next_action_at <= today,
                                       Contact.status.notin_(("skip", "referral"))).count()
        week = day_start(today - timedelta(days=6))
        days = {a.applied_at.date() for a in db.query(Application.applied_at).filter(Application.applied_at >= week)}
        days |= {c.last_action_at.date() for c in db.query(Contact.last_action_at).filter(Contact.last_action_at >= week)}
        return {"picks": picks, "apps": apps, "reqs": reqs, "due": due, "streak": len(days)}
    finally:
        db.close()


def _settings() -> dict:
    from backend.accounts.settings import get_setting
    db = SessionLocal()
    try:
        def i(k, d):
            try:
                return int(get_setting(db, k) or d)
            except ValueError:
                return d
        try:
            cal = json.loads(get_setting(db, "nag_calendar") or "{}")
        except ValueError:
            cal = {}
        return {"apply_t": i("nag_apply_target", 10), "req_t": i("nag_request_target", 5),
                "calendar": cal if isinstance(cal, dict) else {},
                "url": (get_setting(db, "oidc_public_url") or os.environ.get("OIDC_PUBLIC_URL") or "").rstrip("/")}
    finally:
        db.close()


def compose(s: dict, cfg: dict, hour: int, today, rng=random) -> str:
    v = {**s, "apply_t": cfg["apply_t"], "req_t": cfg["req_t"], "date": today.strftime("%b %d"),
         "left_apps": max(0, cfg["apply_t"] - s["apps"]), "left_reqs": max(0, cfg["req_t"] - s["reqs"])}
    done = s["apps"] >= cfg["apply_t"] and s["reqs"] >= cfg["req_t"]
    if hour < 11:
        pool = MORNING
    elif hour >= 21:
        pool = NIGHT
    elif done:
        pool = DONE
    elif hour < 16:
        pool = MIDDAY_BEHIND
    else:
        pool = EVENING_BEHIND
    lines = [rng.choice(pool).format(**v)]
    task = cfg["calendar"].get(today.isoformat())
    if task and hour < 21:
        lines.append(f"Also today: {task}")
    if cfg["url"] and not done and hour < 21:
        lines.append(f"{cfg['url']}/today · {cfg['url']}/people")
    return "\n".join(lines)


async def send_nag(hour: int | None = None) -> str | None:
    """Send one nag for the current account; returns the text, or None when Telegram is off."""
    from backend.notifier import telegram as tg
    if not tg.feature_on("telegram_nag"):
        return None
    chat_id, token = tg.resolve_target()
    if not chat_id:
        return None
    today = local_today()
    text = compose(stats(today), _settings(), local_now().hour if hour is None else hour, today)
    import html
    await tg._send_message(chat_id, html.escape(text, quote=False), **tg._bot(token))
    return text
