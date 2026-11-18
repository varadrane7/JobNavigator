"""Hiring posts: fresh LinkedIn "we're hiring" posts sent in by an outside agent.

The Emails screen lists them so the user can email the poster directly. JobNavigator does
not look for these posts and does not summarise them: an agent finds a post, writes the
summary, and sends both here through `POST /api/hiring-posts` or the connector tool
`add_hiring_post`. Both go through `save_post`, so they accept the same fields and give the
same errors. The fields are documented in FIELDS (and docs/HIRING_POSTS.md).

The tactic the screen serves: a post under a day old with under 100 likes, answered by a
short email to the poster before the pile builds. Those two limits are guidance, not
rules: a post outside them is stored all the same and flagged in `checks`.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import or_

from backend.models.db import HiringPost, utcnow

MAX_AGE_HOURS = 24        # "less than a day old"
MAX_LIKES = 100           # "under 100 likes"
STATUSES = ("new", "emailed", "skipped")
MAX_TEXT = 20000
MAX_SUMMARY = 2000
MAX_ROWS = 200

# name -> what to send. The single source for the API model, the tool schema and the docs.
FIELDS = {
    "post_url": "Required. The post's own link, e.g. https://www.linkedin.com/posts/jane-doe_hiring-activity-7263… "
                "or https://www.linkedin.com/feed/update/urn:li:activity:7263…. The query string is dropped. "
                "It identifies the post: sending the same URL again updates that post instead of adding a copy.",
    "text": f"Required. The post's full text, as written, up to {MAX_TEXT} characters. Not a summary.",
    "summary": f"Optional. Your short summary (2-4 sentences, up to {MAX_SUMMARY} characters): who is hiring, "
               "for what, where, and anything that matters for an email (stack, seniority, visa, remote).",
    "author_name": "Optional but expected. The poster's name, as shown on the post.",
    "author_title": "Optional. The poster's headline, e.g. 'Founder & CEO at Acme'.",
    "author_url": "Optional. The poster's LinkedIn profile URL.",
    "company": "Optional but expected. The company that is hiring.",
    "email": "Optional. The poster's email address, if you found one (from the post, the company site or a "
             "pattern you verified). Leave it out rather than guess.",
    "roles": "Optional. The role titles the post hires for, as a list of strings (a comma-separated string "
             "is accepted too).",
    "location": "Optional. Where the roles are, e.g. 'Remote (US)', 'Bangalore'.",
    "posted_at": "When the post went up: an ISO 8601 date-time, e.g. 2026-11-18T09:30:00Z (no time zone = UTC). "
                 "Send this or posted_hours_ago; it is how the screen tells whether the post is under a day old.",
    "posted_hours_ago": "Alternative to posted_at: the post's age in hours as LinkedIn shows it ('5h' -> 5, "
                        "'1d' -> 24). Converted to posted_at at the time of the call.",
    "likes": f"Optional. Reaction count when you saw it (whole number, 0 or more). Under {MAX_LIKES} is the target.",
    "comments": "Optional. Comment count when you saw it (whole number, 0 or more).",
    "status": "Optional. new (default), emailed (the user wrote to the poster), or skipped. "
              "Agents adding posts should leave it out.",
    "agent": "Optional. Your name, so the user can see who added the post, e.g. 'post-finder'.",
}
REQUIRED = ("post_url", "text")
_STR_FIELDS = ("author_name", "author_title", "author_url", "company", "email", "location", "agent")


class PostError(ValueError):
    """What was wrong with a post, worded for the caller."""


def _clean_url(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PostError(f"{what} is required")
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise PostError(f"{what} must be a full http(s) link, got {value!r}")
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/") or "/", "", ""))


def _int(value, what: str) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise PostError(f"{what} must be a whole number")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise PostError(f"{what} must be a whole number, got {value!r}")
    if n < 0 or n != float(value):
        raise PostError(f"{what} must be a whole number, 0 or more")
    return n


def _when(data: dict) -> Optional[datetime]:
    raw, hours = data.get("posted_at"), data.get("posted_hours_ago")
    if raw not in (None, ""):
        if isinstance(raw, datetime):
            dt = raw
        else:
            try:
                dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
            except ValueError:
                raise PostError(f"posted_at must be an ISO 8601 date-time like 2026-11-18T09:30:00Z, got {raw!r}")
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        if dt > utcnow() + timedelta(minutes=10):
            raise PostError("posted_at is in the future")
        return dt
    if hours not in (None, ""):
        try:
            h = float(hours)
        except (TypeError, ValueError):
            raise PostError(f"posted_hours_ago must be a number of hours, got {hours!r}")
        if h < 0 or isinstance(hours, bool):
            raise PostError("posted_hours_ago must be 0 or more")
        return utcnow() - timedelta(hours=h)
    return None


def _roles(value) -> Optional[list]:
    if value in (None, "", []):
        return None
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
        raise PostError("roles must be a list of strings")
    out = [r.strip() for r in value if r.strip()]
    return out or None


def _text(value, what: str, limit: int, required=False) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise PostError(f"{what} is required")
        return None
    if not isinstance(value, str):
        raise PostError(f"{what} must be a string")
    value = value.strip()
    if len(value) > limit:
        raise PostError(f"{what} is {len(value)} characters; the limit is {limit}")
    return value


def clean(data: dict, partial: bool = False) -> dict:
    """The fields of `data` checked and normalised, keyed by column. `partial` (an update)
    leaves out what was not sent; otherwise post_url and text are required."""
    if not isinstance(data, dict):
        raise PostError("a post must be a JSON object")
    unknown = [k for k in data if k not in FIELDS]
    if unknown:
        raise PostError(f"Unknown field(s) {', '.join(unknown)}; a post takes: {', '.join(FIELDS)}")
    out = {}
    if not partial or "post_url" in data:
        out["post_url"] = _clean_url(data.get("post_url"), "post_url")
    if not partial or "text" in data:
        out["text"] = _text(data.get("text"), "text", MAX_TEXT, required=True)
    if "summary" in data:
        out["summary"] = _text(data["summary"], "summary", MAX_SUMMARY)
    for key in _STR_FIELDS:
        if key in data:
            out[key] = _text(data[key], key, 500)
    if out.get("author_url"):
        out["author_url"] = _clean_url(out["author_url"], "author_url")
    if out.get("email") and ("@" not in out["email"] or " " in out["email"]):
        raise PostError(f"email does not look like an address: {out['email']!r}")
    if "roles" in data:
        out["roles"] = _roles(data["roles"])
    if "posted_at" in data or "posted_hours_ago" in data:
        out["posted_at"] = _when(data)
    for key in ("likes", "comments"):
        if key in data:
            out[key] = _int(data[key], key)
    if "status" in data and data["status"] not in (None, ""):
        if data["status"] not in STATUSES:
            raise PostError("status must be one of " + ", ".join(STATUSES))
        out["status"] = data["status"]
    return out


def save_post(db, data: dict) -> tuple:
    """(post, created). A post already stored under the same post_url is updated with the
    fields sent (a blank never wipes a stored value; status is kept unless sent)."""
    fields = clean(data)
    post = db.query(HiringPost).filter(HiringPost.post_url == fields["post_url"]).first()
    created = post is None
    if created:
        post = HiringPost(status="new")
        db.add(post)
    for key, value in fields.items():
        if value is not None or created:
            setattr(post, key, value)
    if not post.status:
        post.status = "new"
    post.updated_at = utcnow()
    db.flush()
    return post, created


def update_post(db, post: HiringPost, data: dict) -> HiringPost:
    """Change what was sent; here a blank clears a field (except post_url and text)."""
    for key, value in clean(data, partial=True).items():
        setattr(post, key, value)
    post.updated_at = utcnow()
    db.flush()
    return post


def checks(post: HiringPost) -> dict:
    """Is the post inside the tactic's limits? None = not known (no date or no count)."""
    age = None
    if post.posted_at is not None:
        posted = post.posted_at if post.posted_at.tzinfo else post.posted_at.replace(tzinfo=timezone.utc)
        age = (utcnow() - posted).total_seconds() / 3600
    return {"age_hours": round(age, 1) if age is not None else None,
            "under_a_day": (age < MAX_AGE_HOURS) if age is not None else None,
            "under_100_likes": (post.likes < MAX_LIKES) if post.likes is not None else None}


def to_dict(post: HiringPost) -> dict:
    iso = lambda d: d.isoformat() if d else None
    return {"id": str(post.id), "post_url": post.post_url, "text": post.text, "summary": post.summary,
            "author_name": post.author_name, "author_title": post.author_title, "author_url": post.author_url,
            "company": post.company, "email": post.email, "roles": post.roles or [], "location": post.location,
            "posted_at": iso(post.posted_at), "likes": post.likes, "comments": post.comments,
            "status": post.status, "agent": post.agent, "added_at": iso(post.created_at),
            "updated_at": iso(post.updated_at), "checks": checks(post)}


def query(db, status=None, text=None, since_hours=None, fresh=False):
    """Posts newest first (by posted time, else by when they were added)."""
    q = db.query(HiringPost)
    if status:
        wanted = [s.strip() for s in str(status).split(",") if s.strip()]
        bad = [s for s in wanted if s not in STATUSES]
        if bad:
            raise PostError("status must be one of " + ", ".join(STATUSES))
        q = q.filter(HiringPost.status.in_(wanted))
    if text and str(text).strip():
        like = f"%{str(text).strip()}%"
        q = q.filter(or_(HiringPost.company.ilike(like), HiringPost.author_name.ilike(like),
                         HiringPost.text.ilike(like), HiringPost.summary.ilike(like)))
    hours = MAX_AGE_HOURS if fresh else since_hours
    if hours:
        cut = utcnow() - timedelta(hours=float(hours))
        q = q.filter(or_(HiringPost.posted_at >= cut,
                         (HiringPost.posted_at.is_(None)) & (HiringPost.created_at >= cut)))
    return q.order_by(HiringPost.posted_at.desc().nullslast(), HiringPost.created_at.desc(), HiringPost.id.asc())
