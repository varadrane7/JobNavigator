"""E-Verify, shown and never used: a yes / no / unknown flag on jobs and leads.

The flag changes no score, no filter and no ordering (curator/scoring.py never reads it; tests/curator/
test_everify.py holds that). It exists so the owner can see which employers say they use E-Verify.

`company_everify(name_key, status, source, checked_on)` holds what is known per company, install-wide:
  * "yes", source "job description": a job's text states the employer participates (`detect`), set by
    `note_job` and by `backfill`
  * anything, source = the owner's own note: set by hand (`set_status`, the PUT /api/everify route, the MCP
    tool `set_everify`). A hand-set row is never overwritten by a detection.
"no" is never set automatically: a job that does not mention E-Verify says nothing about the employer.
A company with no row is "unknown", unless a job's own text states participation, which shows as "yes" on
that job without writing anything.
"""
import logging
import re
from datetime import date
from typing import Optional

from backend.models.db import CompanyEverify
from backend.tz import local_today

logger = logging.getLogger("jobnavigator.curator.everify")

STATUSES = ("yes", "no", "unknown")
JD_SOURCE = "job description"

_EV = r"e[\s‐-―-]?verify"
# The ways a posting states that the employer participates. Applied one sentence at a time.
_YES = [re.compile(p, re.I) for p in (
    # "participates in E-Verify", "we participate in the E-Verify program", "enrolled in E-Verify"
    rf"\b(?:participat(?:e|es|ing)|enrolled|registered|participant)\s+(?:in|with|of)\s+(?:the\s+)?(?:federal\s+|dhs\s+|uscis\s+)?{_EV}\b",
    # "an E-Verify employer", "is an E-Verify participating employer"
    rf"\b{_EV}\s+(?:participating\s+|registered\s+)?(?:employer|participant)\b",
    # "we use E-Verify", "the company utilizes E-Verify", "DSHS uses E-Verify"
    rf"\b(?:uses|utili[sz]es|use|utili[sz]e)\s+(?:the\s+)?{_EV}\b",
)]
# A sentence that denies, doubts or conditions the statement is not a statement that the employer participates;
# nor is one about somebody else (a staffing agency's client, employers in general, "some of our partners").
_NEGATING = re.compile(
    r"\b(?:not|no|never|neither|nor|without|cannot|unable|isn'?t|aren'?t|doesn'?t|don'?t|won'?t|if|whether|unless|"
    r"may|might|could|would|should|except|where required|when required|as required)\b|n't", re.I)
_ABOUT_OTHERS = re.compile(
    r"\b(?:clients?|customers?|partners?|on behalf of|third[- ]party|staffing|vendors?|subsidiar\w+|affiliates?|"
    r"employers|many|some|most|all|other|certain|various|select)\b", re.I)
_SUBJECT_NOUNS = {"we", "us", "company", "employer", "organization", "organisation", "firm", "business", "it", "this"}
_NOT_SUBJECTS = {"applicants", "candidates", "employees", "hires", "you", "everyone", "applicant", "candidate", "that", "which",
                 "who", "and", "or", "but", "also", "then", "to", "that"}
_AUX = {"also", "currently", "is", "are", "will", "an", "a", "the", "proudly", "now", "already", "be", "as", "we"}
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+|\s[••*]\s")


def _has_subject(sentence: str, m) -> bool:
    """The match is said by or about a named employer: "We use", "The company participates", "Acme is an ...",
    "L3Harris Technologies is an ..." (a capitalised name, or we / the company / this employer, just before the verb)."""
    prefix = re.sub(r"<[^>]+>|[*_]", " ", sentence[:m.start()])
    words = re.findall(r"[A-Za-z0-9&.'\-]+", prefix)
    while words and words[-1].lower().strip(".") in _AUX:
        if words[-1].lower() == "we":
            break
        words.pop()
    if not words:
        return False
    last = words[-1].strip(".,'")
    if last.lower() in _NOT_SUBJECTS:
        return False
    return last.lower() in _SUBJECT_NOUNS or last[:1].isupper() or any(ch.isupper() for ch in last[1:])


def detect(description: Optional[str]) -> Optional[str]:
    """"yes" when `description` states that the employer itself participates in E-Verify, else None. A bare mention
    (a heading, a legal footer, a negative or conditional sentence, a requirement on the candidate, a statement about
    a client or about employers in general) is not a statement."""
    text = description or ""
    if "verify" not in text.lower():
        return None
    for sentence in _SENTENCE.split(text):
        if len(sentence) > 600 or not re.search(_EV, sentence, re.I):
            continue
        if _NEGATING.search(sentence) or _ABOUT_OTHERS.search(sentence):
            continue
        for rx in _YES:
            m = rx.search(sentence)
            if m and _has_subject(sentence, m):
                return "yes"
    return None


def name_key(company: Optional[str]) -> str:
    """The app's one reading of a company name (curator/names.py): "Stripe, Inc." and "Stripe" are one key."""
    from backend.curator import names
    return names.company_key(company)


# ── the table ────────────────────────────────────────────────────────────────

def _table(db) -> dict:
    """{name_key: (status, source)}, read once per session."""
    cache = db.info.get("everify")
    if cache is None:
        cache = {r.name_key: (r.status, r.source) for r in db.query(CompanyEverify).all()}
        db.info["everify"] = cache
    return cache


def flag(db, company: Optional[str], description: Optional[str] = None) -> str:
    """yes | no | unknown for a company, and for a job when its description is given: what is on file (a row set by
    hand wins whatever it says, "unknown" included, so a wrong yes can be cleared), else what the posting itself
    states, else unknown. Shown only."""
    row = _table(db).get(name_key(company))
    if row and row[0] in STATUSES and (row[1] != JD_SOURCE or row[0] == "yes"):
        return row[0]
    if detect(description) == "yes":
        return "yes"
    return "unknown"


def flags_for(db, companies) -> dict:
    """{name_key: status} for `companies`, from the table alone (a lead has no posting text)."""
    table = _table(db)
    return {k: (table[k][0] if k in table and table[k][0] in STATUSES else "unknown")
            for k in {name_key(c) for c in companies if c}}


def _write(db, key: str, status: str, source: str, on: date, only_over_detection: bool = False) -> bool:
    """Insert or update a row without ever raising on a race: two runs adding the same company at once is not an
    error (the loser updates the winner's row). With `only_over_detection` an existing row set by hand is left alone."""
    from sqlalchemy.exc import IntegrityError
    for _ in range(2):
        row = db.get(CompanyEverify, key)
        if row is not None:
            if only_over_detection and row.source != JD_SOURCE:
                return False
            row.status, row.source, row.checked_on = status, source, on
            db.info.pop("everify", None)
            return True
        try:
            with db.begin_nested():
                db.add(CompanyEverify(name_key=key, status=status, source=source, checked_on=on))
            db.info.pop("everify", None)
            return True
        except IntegrityError:           # another run inserted it first: loop, and update that row
            db.expire_all()
    return False


def set_status(db, company: str, status: str, source: str, on: Optional[date] = None) -> dict:
    """Set a company's status by hand, with a note saying where it comes from. Does not commit.
    Raises ValueError for a bad status or an empty company or source."""
    status = (status or "").strip().lower()
    key = name_key(company)
    if status not in STATUSES:
        raise ValueError("status must be one of " + ", ".join(STATUSES))
    if not key:
        raise ValueError("company is required")
    source = " ".join((source or "").split())
    if not source:
        raise ValueError("source is required: say where this comes from (the DHS employer search, their careers page, an email)")
    on = on or local_today()
    _write(db, key, status, source[:300], on)
    row_source, checked = source[:300], on
    return {"company": company, "name_key": key, "status": status, "source": row_source, "checked_on": checked.isoformat()}


def listing(db) -> list:
    return [{"name_key": r.name_key, "status": r.status, "source": r.source,
             "checked_on": r.checked_on.isoformat() if r.checked_on else None}
            for r in db.query(CompanyEverify).order_by(CompanyEverify.name_key).all()]


def note_job(db, company: Optional[str], description: Optional[str], on: Optional[date] = None) -> bool:
    """Record a detection from a job: sets "yes" (source "job description") for the company unless the
    owner set it by hand. Does not commit. Returns True when a row was added or refreshed."""
    key = name_key(company)
    if not key or detect(description) != "yes":
        return False
    return _write(db, key, "yes", JD_SOURCE, on or local_today(), only_over_detection=True)


def backfill(db, apply: bool = False) -> dict:
    """Scan the stored job descriptions for E-Verify statements. A dry run unless `apply` (nothing is written,
    nothing committed): {"scanned", "jobs_matching", "companies_matching", "companies_new", "jobs_covered"},
    where jobs_covered counts every stored job of the companies that would be marked yes, since the flag is the
    company's. With `apply` the rows are written (and committed)."""
    from backend.models.db import Job
    scanned, matched = 0, {}
    jobs_by_company = {}
    for company, description in db.query(Job.company, Job.description).yield_per(500):
        key = name_key(company)
        if not key:
            continue
        scanned += 1
        jobs_by_company[key] = jobs_by_company.get(key, 0) + 1
        if detect(description) == "yes":
            matched[key] = matched.get(key, 0) + 1
    table = _table(db)
    fresh = [k for k in matched if k not in table or (table[k][1] == JD_SOURCE and table[k][0] != "yes")]
    out = {"scanned": scanned, "jobs_matching": sum(matched.values()), "companies_matching": len(matched),
           "companies_new": len(fresh), "jobs_covered": sum(jobs_by_company[k] for k in matched)}
    if apply:
        today = local_today()
        for key in matched:
            _write(db, key, "yes", JD_SOURCE, today, only_over_detection=True)
        db.commit()
        db.info.pop("everify", None)
    return out
