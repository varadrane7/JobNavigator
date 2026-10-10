"""Numbers for the AutoApply stats screen: volume, success by submitter, how often a person was
needed, time per application, LLM use, and how many applications got an answer back."""
from datetime import datetime, timedelta, timezone

from backend.autoapply import llm_router, pacing, settings
from backend.autoapply.models import AutoApplyItem, AutoApplyAttempt
from backend.models.db import Application

# Tsenta's published price range per application, for the cost comparison.
TSENTA_COST_PER_APP = (0.022, 0.033)
RESPONDED = ("interview", "offer", "rejected")


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def compute(db, days: int = 14, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cfg = settings.get_all(db)
    since = now - timedelta(days=days)
    tz = pacing._tz(cfg)

    items = db.query(AutoApplyItem).filter(AutoApplyItem.created_at >= since).all()
    by_sub: dict = {}
    for it in items:
        s = by_sub.setdefault(it.submitter or "unknown", {"submitter": it.submitter or "unknown", "total": 0,
                                                          "submitted": 0, "failed": 0, "needs_human": 0,
                                                          "dry_run_ok": 0})
        s["total"] += 1
        if it.status in ("submitted", "failed", "needs_human", "dry_run_ok"):
            s[it.status] += 1
    for s in by_sub.values():
        tried = s["submitted"] + s["failed"] + s["needs_human"]
        s["success_rate"] = _pct(s["submitted"], tried)
        s["needs_human_rate"] = _pct(s["needs_human"], tried)

    per_day: dict = {}
    for it in items:
        if it.status == "submitted" and it.submitted_at:
            t = it.submitted_at if it.submitted_at.tzinfo else it.submitted_at.replace(tzinfo=timezone.utc)
            d = t.astimezone(tz).date().isoformat()
            per_day[d] = per_day.get(d, 0) + 1
    daily = []
    for i in range(days - 1, -1, -1):
        d = (now.astimezone(tz) - timedelta(days=i)).date().isoformat()
        daily.append({"day": d, "submitted": per_day.get(d, 0)})

    submitted_ids = [it.id for it in items if it.status == "submitted"]
    secs = []
    llm_calls = 0
    if submitted_ids:
        for a in db.query(AutoApplyAttempt).filter(AutoApplyAttempt.item_id.in_(submitted_ids),
                                                   AutoApplyAttempt.outcome == "submitted").all():
            llm_calls += a.llm_calls or 0
            if a.started_at and a.finished_at:
                s, f = a.started_at, a.finished_at
                if s.tzinfo is None:
                    s = s.replace(tzinfo=timezone.utc)
                if f.tzinfo is None:
                    f = f.replace(tzinfo=timezone.utc)
                secs.append((f - s).total_seconds())

    app_ids = [it.application_id for it in items if it.application_id]
    answered = 0
    if app_ids:
        answered = db.query(Application).filter(Application.id.in_(app_ids),
                                                Application.status.in_(RESPONDED)).count()

    total_submitted = len(submitted_ids)
    usage = llm_router.usage_summary(db, days)
    prov: dict = {}
    for r in usage:
        p = prov.setdefault(r["provider"], {"provider": r["provider"], "calls": 0, "failures": 0,
                                            "quota_hits": 0, "input_tokens": 0, "output_tokens": 0})
        for k in ("calls", "failures", "quota_hits", "input_tokens", "output_tokens"):
            p[k] += r[k] or 0
    return {
        "days": days,
        "totals": {
            "queued": len(items),
            "submitted": total_submitted,
            "needs_human": sum(1 for i in items if i.status == "needs_human"),
            "failed": sum(1 for i in items if i.status == "failed"),
            "response_rate": _pct(answered, total_submitted),
            "responses": answered,
            "avg_seconds_per_app": round(sum(secs) / len(secs), 1) if secs else None,
            "llm_calls_per_app": round(llm_calls / total_submitted, 2) if total_submitted else None,
        },
        "daily": daily,
        "by_submitter": sorted(by_sub.values(), key=lambda s: -s["total"]),
        "llm": sorted(prov.values(), key=lambda p: -p["calls"]),
        # Subscription and local calls cost no per-call money; Tsenta is the comparison.
        "cost": {"per_app_usd": 0.0, "tsenta_per_app_usd": list(TSENTA_COST_PER_APP),
                 "note": "Calls run on your subscriptions and local models, so there is no per-application charge."},
    }
