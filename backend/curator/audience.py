"""Who a person is to the sender: the audience a message is written for.

`Contact.audience` is one of alum, ex_colleague, engineer, recruiter, hiring_manager, post, set from
evidence only, and left empty when nothing says:

  alum          the card or profile text names one of the sender's schools
  ex_colleague  it names one of the sender's past employers (a shared employer, not proof that they overlapped)
  engineer / recruiter / hiring_manager   the headline, read by `people.classify` (founders count as hiring_manager)
  post          the lead came from a hiring post and is not one of the above (a post's text is not the
                person's own, so it is never read for schools or employers)

The schools and employers come from the stored profile (the Persona: education, experience, and the
networking "affinities"), never from strings in this file. The order used by the People screen, the
"send today" choice and the tools follows the owner's priority: alumni, then former colleagues, then
engineers, recruiters, people who posted, hiring managers (who only get a message when they posted).
"""
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy import case

from backend.curator import names

AUDIENCES = ("alum", "ex_colleague", "engineer", "recruiter", "hiring_manager", "post")
PRIORITY = {"alum": 0, "ex_colleague": 1, "engineer": 2, "recruiter": 3, "post": 4, "hiring_manager": 5}
NO_AUDIENCE_RANK = len(PRIORITY)
LABEL = {"alum": "Alum", "ex_colleague": "Ex-colleague", "engineer": "Engineer", "recruiter": "Recruiter",
         "hiring_manager": "Hiring manager", "post": "Hiring post"}

_SCHOOL_WORD = re.compile(r"\b(university|institute|college|school|academy|polytechnic)\b", re.I)
_STOP = {"of", "the", "and", "for", "at", "in"}
# a school named as where the person WORKS (staff, faculty, a campus recruiter) is not where they studied
_STAFF = r"(?:staff|faculty|professor|lecturer|instructor|adjunct|recruiter|admissions|career services|alumni relations|dean|administrator)"


def rank(audience: Optional[str]) -> int:
    return PRIORITY.get(audience or "", NO_AUDIENCE_RANK)


def order_expr(column):
    """SQL ordering expression: audience priority, unknown audience last."""
    return case(*[(column == a, r) for a, r in PRIORITY.items()], else_=NO_AUDIENCE_RANK)


def _fold(text: str) -> str:
    from backend.analyzer.location import fold
    return fold(text)


def _fold_cased(text: str) -> str:
    """fold() without the lower-casing: same length as fold(text), so an offset found in one is valid in the other."""
    import unicodedata
    ascii_only = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", ascii_only).strip()


def _phrase(words: Iterable[str]) -> Optional[re.Pattern]:
    words = [w for w in words if w]
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(map(re.escape, words)) + r"(?![a-z0-9])")


def _school_patterns(name: str) -> list:
    """Ways a card writes `name` (matched on folded text): as stored, and "Mumbai University" for
    "University of Mumbai"."""
    words = re.findall(r"[a-z0-9]+", _fold(name))
    out = [_phrase(words)]
    if len(words) >= 3 and words[0] == "university" and words[1] == "of":
        out.append(_phrase(words[2:] + ["university"]))
    return [p for p in out if p]


def _acronym(name: str) -> Optional[str]:
    """The initials of a school's significant words when there are at least four (New Jersey Institute of
    Technology -> NJIT). Shorter ones collide with ordinary words and are not used on their own."""
    sig = [w for w in re.findall(r"[A-Za-z0-9]+", name) if w.lower() not in _STOP]
    ini = "".join(w[0] for w in sig).upper()
    return ini if len(sig) >= 4 and len(ini) >= 4 else None


@dataclass
class Affiliations:
    schools: list = field(default_factory=list)       # names as the profile has them (the sender-facts block's education)
    employers: list = field(default_factory=list)     # past employers in the sender-facts block, never the current one
    _school_rx: list = field(default_factory=list, repr=False)
    _employer_rx: list = field(default_factory=list, repr=False)

    def __post_init__(self):
        self._school_rx = [(n, _school_patterns(n), _acronym(n)) for n in self.schools]
        self._employer_rx = [(n, _phrase(names.words(n))) for n in self.employers]

    def _school_line(self, line: str, kind: str) -> Optional[str]:
        """`kind` is "current" (a current position), "past" (a past position) or "plain" (headline, snippet)."""
        if kind == "current":
            return None                                 # they work there now: staff, not a student
        folded = _fold(line)
        cased = _fold_cased(line)                       # same offsets as `folded`, capitals kept for the initials
        for n, pats, ini in self._school_rx:
            spans = [m.span() for p in pats for m in [p.search(folded)] if m]
            if ini:
                m = re.search(r"(?<![A-Za-z0-9])" + ini + r"(?![A-Za-z0-9])", cased)   # uppercase standalone token only
                if m:
                    spans.append(m.span())
            for start, end in spans:
                before, after = folded[max(0, start - 14):start], folded[end:end + 40]
                if re.search(_STAFF + r"\W*$", before) or re.search(r"^\W*" + _STAFF, after):
                    continue                            # "campus recruiter, <school>": works there
                if kind == "plain" and re.search(r"(?:\bat|\bfor|@)\s*$", before) \
                        and not re.search(r"\b(?:studied|student|alum\w*|graduate|ms|bs|ma|phd|msc|bsc|mba)\b", before):
                    continue                            # "Engineer at <school>": works there
                return n
        return None

    def school_in(self, text: str) -> Optional[str]:
        """The sender's school this card names as where the person studied (or once was), not where they work.
        Lines: "Current: ..." and "Experience: ..." are current positions; "Location: ..." is ignored."""
        for line in re.split(r"[\n\u00b7|]+", text or ""):
            line = line.strip()
            if not line or re.match(r"location\s*:", line, re.I):
                continue
            kind = ("current" if re.match(r"(?:current|experience)\s*:", line, re.I)
                    else "past" if re.match(r"past\s*:", line, re.I) else "plain")
            found = self._school_line(line, kind)
            if found:
                return found
        return None

    def employer_in(self, text: str) -> Optional[str]:
        """A past employer of the sender the card names, by its full name (legal suffixes aside)."""
        folded = _fold(re.sub(r"(?im)^\s*location\s*:.*$", "", text or ""))
        return next((n for n, p in self._employer_rx if p and p.search(folded)), None)


def _same(a: str, b: str) -> bool:
    return bool(a and b) and names.company_key(a) == names.company_key(b)


def entries(persona) -> tuple:
    """(schools, jobs) exactly as the sender-facts block lists them (curator/drafting.py builds its education and
    experience lines from this, and affiliations() below from the same): schools [{"school", "degree"}], jobs
    [{"title", "company", "current"}]. Only what the persona's education and experience hold; nothing else."""
    rc = persona.resume_content if persona is not None and isinstance(persona.resume_content, dict) else {}
    one = lambda v: " ".join(str(v or "").split())
    schools = [{"school": one(e.get("school")), "degree": one(e.get("degree"))}
               for e in rc.get("education") or [] if isinstance(e, dict) and one(e.get("school"))]
    jobs = [{"title": one(e.get("title")), "company": one(e.get("company")),
             "current": bool(re.search(r"present|current", str(e.get("date") or ""), re.I))}
            for e in rc.get("experience") or [] if isinstance(e, dict) and one(e.get("title")) and one(e.get("company"))]
    return schools, jobs


def affiliations(persona) -> Affiliations:
    """Schools and past employers the audience may be matched on: those in the sender-facts block, no others
    (networking "affinities" are not used). The current employer is left out."""
    if persona is None:
        return Affiliations()
    schools, jobs = entries(persona)
    names_ = list(dict.fromkeys(s["school"] for s in schools))
    employers = []
    for j in jobs:
        if j["current"] or any(_same(j["company"], s) for s in names_):
            continue
        employers.append(j["company"])
    cur = [j["company"] for j in jobs if j["current"]]
    employers = [n for n in dict.fromkeys(employers) if not any(_same(n, c) for c in cur)]
    return Affiliations(names_, employers)


def detect(headline: str, card_text: str = "", aff: Optional[Affiliations] = None, from_post: bool = False) -> Optional[str]:
    """The audience for a person, from evidence. `card_text` is what the person's own card or profile
    snippet says (headline, current and past lines); a hiring post's text does not belong in it."""
    from backend.curator.people import classify
    text = " \n".join(x for x in (headline, card_text) if x)
    if aff is not None:
        if aff.school_in(text):
            return "alum"
        if aff.employer_in(text):
            return "ex_colleague"
    kind = classify(headline)
    if from_post:
        return "hiring_manager" if kind in ("hiring_manager", "founder") else "post"
    if kind == "engineer":
        return "engineer"
    if kind == "recruiter":
        return "recruiter"
    if kind in ("hiring_manager", "founder"):
        return "hiring_manager"
    return None


def load_affiliations(db=None) -> Affiliations:
    """Affiliations of the current account's persona."""
    from backend.accounts.owned import get_persona
    from backend.models.db import SessionLocal
    own = db is None
    db = db or SessionLocal()
    try:
        return affiliations(get_persona(db, create=False))
    finally:
        if own:
            db.close()
