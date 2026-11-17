"""LinkedIn's numeric ids for companies and schools, for facet searches (`li_entity_ids`, install-wide).

An id is learned for free or not at all: from the table, from links on a page the curator already loaded
(`ids_from_links`, fed by LinkedInPeople after each search page), or seeded here from values checked by hand in
a real browser on 2026-11-17 (.course/linkedin-facets.md). Nothing in this module loads a LinkedIn page: a
company with no known id is searched by keyword, as before, and its id is picked up from that page when the
links on it carry one. A stored id is never overwritten.
"""
import logging
import re
from urllib.parse import unquote

from backend.curator import names
from backend.models.db import LiEntityId, SessionLocal
from backend.tz import local_today

logger = logging.getLogger("jobnavigator.curator.entity_ids")

COMPANY, SCHOOL = "company", "school"

# Checked in a real browser on 2026-11-17. Schools and the past employer are only stored when the persona
# names them; Stripe is a target company, stored as is. Kotak Securities: 110647982 is the id in the company link
# of the owner's own experience entry (/in/me/details/experience/); the 5633 first tried belongs to the "Kotak Neo"
# page that /company/kotak-securities/ redirects to, and is not used.
VERIFIED = {
    SCHOOL: {"new jersey institute of technology": "165198", "university of mumbai": "15093732"},
    COMPANY: {"stripe": "2135371"},
}
VERIFIED_PAST_EMPLOYERS = {"kotak securities": "110647982"}

_HREF_PARAM = re.compile(r"(current|past)company=([^&]*)|schoolfilter=([^&]*)", re.I)
_HREF_COMPANY = re.compile(r"/(?:company|school)/(\d+)(?:/|$|\?)")


def _key(name) -> str:
    return names.company_key(name)


def lookup(kind: str, name: str, db=None) -> str | None:
    """The stored id for `name`, or None. Never loads anything."""
    k = _key(name)
    if not k:
        return None
    own = db is None
    db = db or SessionLocal()
    try:
        row = db.get(LiEntityId, (kind, k))
        return row.li_id if row else None
    finally:
        if own:
            db.close()


def remember(kind: str, name: str, li_id: str, db=None) -> bool:
    """Store `li_id` for `name` unless one is already stored. True when a row was added."""
    k, li_id = _key(name), str(li_id or "").strip()
    if not k or not li_id.isdigit():
        return False
    own = db is None
    db = db or SessionLocal()
    try:
        if db.get(LiEntityId, (kind, k)) is not None:
            return False
        db.add(LiEntityId(kind=kind, name_key=k, li_id=li_id, resolved_on=local_today()))
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.warning(f"could not store LinkedIn id for {name!r}: {e}")
        return False
    finally:
        if own:
            db.close()


def seed(aff=None, db=None) -> int:
    """Store the verified ids that apply (idempotent; returns how many rows were added). Schools and past
    employers only when the persona's affiliations (curator/audience.Affiliations) name them."""
    added = 0
    own = db is None
    db = db or SessionLocal()
    try:
        for name, li_id in VERIFIED[COMPANY].items():
            added += remember(COMPANY, name, li_id, db)
        if aff is not None:
            for school in getattr(aff, "schools", []) or []:
                li_id = VERIFIED[SCHOOL].get(_key(school))
                if li_id:
                    added += remember(SCHOOL, school, li_id, db)
            for emp in getattr(aff, "employers", []) or []:
                li_id = VERIFIED_PAST_EMPLOYERS.get(_key(emp))
                if li_id:
                    added += remember(COMPANY, emp, li_id, db)
    finally:
        if own:
            db.close()
    return added


def ids_for(kind: str, wanted: list, db=None) -> list:
    """[(name, id)] for the names in `wanted` that have a stored id, in order."""
    out = []
    for n in wanted or []:
        li_id = lookup(kind, n, db)
        if li_id:
            out.append((n, li_id))
    return out


def _found(items: list) -> list:
    """[(kind, name, id, explicit)] from anchors [{href, text}] read off a page that is already loaded. A link counts
    when it carries one numeric id in a currentCompany / pastCompany / schoolFilter facet or points at
    /company/<digits>/ or /school/<digits>/, and has a label to name it by. `explicit` is True for a link to the
    company or school page itself: the strongest evidence a page gives of whose id it is."""
    out = []
    for it in items or []:
        href, text = unquote(str(it.get("href") or "")), " ".join(str(it.get("text") or "").split())
        if not text or len(text) > 80:
            continue
        m = _HREF_PARAM.search(href)
        ids = re.findall(r"\d+", (m.group(2) or m.group(3) or "")) if m else []
        if m and len(ids) == 1:
            out.append((SCHOOL if m.group(3) is not None else COMPANY, text, ids[0], False))
            continue
        m = _HREF_COMPANY.search(href)
        if m:
            out.append((SCHOOL if "/school/" in href else COMPANY, text, m.group(1), True))
    return out


def ids_from_links(items: list) -> list:
    """[(kind, name, id)] for the links _found reads."""
    return [(k, t, i) for k, t, i, _ in _found(items)]


def _hand_verified(kind: str, li_id: str) -> bool:
    return li_id in set(VERIFIED[kind].values()) or (kind == COMPANY and li_id in set(VERIFIED_PAST_EMPLOYERS.values()))


def learn(items: list, wanted: set) -> int:
    """Store the ids on a loaded page whose label is one of the `wanted` company keys. No page load.
    When the table already holds a different id for the name, the conflict is logged; an explicit link to the
    company's own page then replaces an id that was only harvested or seeded unconfirmed, never a hand-verified one."""
    n = 0
    for kind, text, li_id, explicit in _found(items):
        k = _key(text)
        if k not in wanted:
            continue
        have = lookup(kind, text)
        if have and have != li_id:
            logger.warning(f"LinkedIn id conflict for {text!r}: stored {have}, page shows {li_id}"
                           + (" (explicit company link)" if explicit else ""))
            if explicit and not _hand_verified(kind, have):
                n += replace(kind, text, li_id)
            continue
        n += remember(kind, text, li_id)
    return n


def replace(kind: str, name: str, li_id: str) -> bool:
    k = _key(name)
    db = SessionLocal()
    try:
        row = db.get(LiEntityId, (kind, k))
        if row is None or not str(li_id).isdigit():
            return False
        row.li_id, row.resolved_on = str(li_id), local_today()
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.warning(f"could not correct LinkedIn id for {name!r}: {e}")
        return False
    finally:
        db.close()
