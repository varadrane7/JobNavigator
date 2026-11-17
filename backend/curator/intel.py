"""What the curator knows about a company from local data only: how often its recruiters have answered the
owner's own applications, and the company's email format (domain + name pattern, as stored in company_intel by
earlier versions) for guessing a work address. Nothing here goes to the web.
"""
import re

from backend.models.db import Application, CompanyIntel, Job, SessionLocal


def name_key(name: str) -> str:
    return (name or "").strip().lower()


def make_email(first: str, last: str, pattern: str | None, domain: str | None) -> str | None:
    if not (pattern and domain and first):
        return None
    f = re.sub(r"[^a-z]", "", first.lower())
    l = re.sub(r"[^a-z]", "", (last or "").lower())
    if not f or ("last" in pattern and not l):
        return None
    local = (pattern.replace("first", "\0F").replace("last", "\0L")
             .replace("\0F", f).replace("\0L", l))
    # single-letter forms: f / l
    if pattern in ("flast", "f.last", "f_last", "f-last"):
        local = f[0] + pattern[1:-4] + l
    elif pattern == "lastf":
        local = l + f[0]
    elif pattern == "firstl":
        local = f + l[0]
    return f"{local}@{domain}"


# ── own history ─────────────────────────────────────────────────────────────
def own_reply_rates(db) -> dict:
    """{company name_key: (reply_rate, applications)} from your own applications."""
    rows = (db.query(Job.company, Application.status, Application.last_email_received,
                     Application.status_transitions)
            .join(Application, Application.job_id == Job.id).all())
    agg: dict = {}
    for company, status, last_email, transitions in rows:
        key = name_key(company)
        if not key:
            continue
        replied = bool(last_email) or status in ("interview", "offer") or any(
            (t or {}).get("source") == "email" for t in (transitions or []))
        n, r = agg.get(key, (0, 0))
        agg[key] = (n + 1, r + int(replied))
    return {k: (r / n, n) for k, (n, r) in agg.items()}


def get_intel(companies: list[str]) -> dict:
    """{name_key: {"domain", "email_pattern"}} for the companies that have a stored email format."""
    keys = {name_key(c) for c in companies if name_key(c)}
    if not keys:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(CompanyIntel).filter(CompanyIntel.name_key.in_(list(keys))).all()
        return {r.name_key: {"domain": r.domain, "email_pattern": r.email_pattern} for r in rows}
    finally:
        db.close()
