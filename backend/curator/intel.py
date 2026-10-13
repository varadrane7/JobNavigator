"""Company research for the curator: funding, layoffs, candidate-reply reputation,
pay band and email format, from a few fixed SearXNG queries plus one small LLM
extraction. Cached in company_intel for INTEL_TTL_DAYS, so a company costs one
lookup a month no matter how many of its jobs show up.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from backend.curator.llm import ask_json
from backend.models.db import Application, CompanyIntel, Job, SessionLocal
from backend.search.searxng import SearchError, format_results, search
from backend.tz import local_today

logger = logging.getLogger("jobnavigator.curator.intel")

INTEL_TTL_DAYS = 30
STAGES = {"pre_seed", "seed", "series_a", "series_b", "series_c", "series_d", "series_e", "series_f",
          "series_g", "public", "bootstrapped", "acquired", "unknown"}


def name_key(name: str) -> str:
    return (name or "").strip().lower()


# ── email format ────────────────────────────────────────────────────────────
_EMAIL = re.compile(r"\b([A-Za-z][A-Za-z._-]{0,30})@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")
_SAMPLE_FIRST = {"jane", "john", "james", "jon", "first"}
_SAMPLE_LAST = {"doe", "smith", "last"}
_SKIP_DOMAINS = ("rocketreach", "leadiq", "snov", "neverbounce", "contactout", "hunter.io",
                 "zoominfo", "apollo", "signalhire", "example.com", "gmail.com", "email.com")


def pattern_from_local(local: str) -> str | None:
    """Name pattern of a sample address's local part ('jdoe' -> 'flast')."""
    l = local.lower()
    for sep, tag in ((".", "."), ("_", "_"), ("-", "-")):
        if sep in l:
            a, _, b = l.partition(sep)
            if a in _SAMPLE_FIRST and b in _SAMPLE_LAST:
                return f"first{tag}last"
            if len(a) == 1 and b in _SAMPLE_LAST:
                return f"f{tag}last"
            if a in _SAMPLE_LAST and b in _SAMPLE_FIRST:
                return f"last{tag}first"
            return None
    if l in _SAMPLE_FIRST:
        return "first"
    if l in _SAMPLE_LAST:
        return "last"
    for f in _SAMPLE_FIRST:
        for s in _SAMPLE_LAST:
            if l == f + s:
                return "firstlast"
            if l == f[0] + s:
                return "flast"
            if l == s + f[0]:
                return "lastf"
            if l == f + s[0]:
                return "firstl"
    return None


def email_format_from_snippets(results: list[dict], company: str) -> tuple[str | None, str | None]:
    """(domain, pattern) from email-format sites' snippets ('most common ... (ex. jdoe@ramp.com)')."""
    ckey = re.sub(r"[^a-z0-9]", "", company.lower())
    found = []
    for r in results:
        text = f"{r.get('title', '')} {r.get('snippet', '')}"
        for m in _EMAIL.finditer(text):
            local, domain = m.group(1), m.group(2).lower()
            if any(s in domain for s in _SKIP_DOMAINS):
                continue
            # the domain should look like the company (ramp.com for Ramp, not ramp.se for "RAMP AB")
            root = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
            if ckey and root and not (root in ckey or ckey.startswith(root) or root.startswith(ckey[:5])):
                continue
            pat = pattern_from_local(local)
            if pat:
                found.append((domain, pat))
    # a US job wants the .com/.io/.ai domain over a country one (ramp.com, not ramp.se)
    found.sort(key=lambda dp: not dp[0].endswith((".com", ".io", ".ai", ".co", ".dev", ".tech")))
    return found[0] if found else (None, None)


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


# ── web research ────────────────────────────────────────────────────────────
_SYSTEM = ("You extract facts about a company from web search snippets for a job seeker. "
           "Use only what the snippets say; use null when they don't say it.")

_PROMPT = """Company: {company}
Current year: {year}

Search results:
{snippets}

Return JSON with exactly these keys:
- "funding_stage": one of {stages}
- "last_raise_year": year of the most recent funding round (int) or null
- "headcount": approximate employees (int) or null
- "recent_layoffs": true if layoffs were reported in the last 12 months, false if the snippets suggest none, null if unknown
- "reply_reputation": 0.0-1.0, how likely applicants hear back (interview-review sites: "never heard back"/"ghosted" = low, quick recruiter replies = high), null if unknown
- "est_base_min", "est_base_max": typical US base salary in USD for a software engineer with ~3 years of experience at this company, ints or null
- "notes": one short sentence a candidate should know (or "")"""


async def _research(company: str) -> dict:
    year = local_today().year
    queries = [
        (f"{company} raises funding round series valuation", None),
        (f"{company} layoffs {year}", "year"),
        (f"{company} interview process glassdoor candidates heard back", None),
        (f"{company} software engineer salary levels.fyi", None),
        (f"{company} email format", None),
    ]
    results = await asyncio.gather(*[search(q, n=6, time_range=tr) for q, tr in queries], return_exceptions=True)
    results = [r if isinstance(r, list) else [] for r in results]
    domain, pattern = email_format_from_snippets(results[4], company)
    snippets = "\n".join(format_results(r, 1400) for r in results[:4] if r)
    data = {}
    if snippets.strip():
        data = await ask_json(
            _PROMPT.format(company=company, year=year, snippets=snippets,
                           stages=", ".join(sorted(STAGES))),
            _SYSTEM, purpose="curator_company_intel", max_tokens=1500) or {}
    sources = [r["url"] for group in results for r in group[:2]]
    return {"data": data, "domain": domain, "pattern": pattern, "sources": sources}


def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float01(v):
    try:
        return max(0.0, min(1.0, float(v))) if v is not None else None
    except (TypeError, ValueError):
        return None


async def get_intel(companies: list[str], concurrency: int = 3) -> dict:
    """{name_key: CompanyIntel-as-dict} for `companies`, researching the stale/missing ones."""
    keys = {name_key(c): c for c in companies if name_key(c)}
    cutoff = datetime.now(timezone.utc) - timedelta(days=INTEL_TTL_DAYS)
    db = SessionLocal()
    try:
        rows = {r.name_key: r for r in db.query(CompanyIntel).filter(CompanyIntel.name_key.in_(list(keys))).all()}
    finally:
        db.close()

    def fresh(r):
        ft = r.fetched_at
        if ft is not None and ft.tzinfo is None:
            ft = ft.replace(tzinfo=timezone.utc)
        return ft and ft > cutoff

    todo = [k for k in keys if k not in rows or not fresh(rows[k])]
    sem = asyncio.Semaphore(concurrency)

    async def one(k):
        async with sem:
            try:
                return k, await _research(keys[k])
            except SearchError as e:
                logger.warning(f"intel for {keys[k]} skipped: {e}")
                return k, None

    found = await asyncio.gather(*[one(k) for k in todo])
    db = SessionLocal()
    try:
        for k, res in found:
            if res is None:
                continue
            d = res["data"]
            row = db.query(CompanyIntel).filter(CompanyIntel.name_key == k).first() or CompanyIntel(name_key=k)
            stage = str(d.get("funding_stage") or "unknown").lower().replace(" ", "_").replace("-", "_")
            row.display_name = keys[k]
            row.domain = res["domain"] or row.domain
            row.email_pattern = res["pattern"] or row.email_pattern
            row.funding_stage = stage if stage in STAGES else "unknown"
            row.last_raise_year = _int(d.get("last_raise_year"))
            row.headcount = _int(d.get("headcount"))
            row.recent_layoffs = d.get("recent_layoffs") if isinstance(d.get("recent_layoffs"), bool) else None
            row.reply_reputation = _float01(d.get("reply_reputation"))
            lo, hi = _int(d.get("est_base_min")), _int(d.get("est_base_max"))
            row.est_base_min = lo if lo and 40000 <= lo <= 500000 else None
            row.est_base_max = hi if hi and 40000 <= hi <= 600000 else None
            row.notes = str(d.get("notes") or "")[:500]
            row.sources = res["sources"]
            row.fetched_at = datetime.now(timezone.utc)
            db.merge(row)
        db.commit()
        rows = {r.name_key: r for r in db.query(CompanyIntel).filter(CompanyIntel.name_key.in_(list(keys))).all()}
        return {k: intel_dict(r) for k, r in rows.items()}
    finally:
        db.close()


def intel_dict(r: CompanyIntel) -> dict:
    return {c: getattr(r, c) for c in ("display_name", "domain", "email_pattern", "funding_stage",
                                       "last_raise_year", "headcount", "recent_layoffs", "reply_reputation",
                                       "est_base_min", "est_base_max", "notes")}
