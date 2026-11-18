"""`/api/hiring-posts`: the Emails screen's data, and the door an outside agent uses to add to it.

An agent that finds fresh LinkedIn "we're hiring" posts sends each one here (or through the
connector tool `add_hiring_post`); the screen lists them for the user to email the poster.
The same rules apply on both paths (backend/hiring_posts.py). Auth is the dashboard's: the
`X-API-Key` header plus `X-Account-Token` naming the account (required once an install has
more than one). Field reference:
docs/HIRING_POSTS.md, or the schema of `HiringPostIn` at /docs.
"""
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import hiring_posts as hp
from backend.models.db import HiringPost, get_db

router = APIRouter(tags=["hiring posts"])
F = hp.FIELDS


class HiringPostIn(BaseModel):
    """One LinkedIn hiring post. Only post_url and text are required; send what you have."""
    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": {
        "post_url": "https://www.linkedin.com/posts/jane-doe_hiring-backend-activity-7263000000000000000-abcd",
        "text": "We're hiring! Acme (YC W26) is looking for a founding backend engineer...",
        "summary": "Acme, a YC W26 fintech, wants a founding backend engineer (Python, Postgres), remote US.",
        "author_name": "Jane Doe", "author_title": "Co-founder & CTO at Acme",
        "author_url": "https://www.linkedin.com/in/jane-doe", "company": "Acme", "email": "jane@acme.dev",
        "roles": ["Founding Backend Engineer"], "location": "Remote (US)", "posted_hours_ago": 5,
        "likes": 42, "comments": 7, "agent": "post-finder"}})

    post_url: str = Field(description=F["post_url"])
    text: str = Field(description=F["text"])
    summary: Optional[str] = Field(None, description=F["summary"])
    author_name: Optional[str] = Field(None, description=F["author_name"])
    author_title: Optional[str] = Field(None, description=F["author_title"])
    author_url: Optional[str] = Field(None, description=F["author_url"])
    company: Optional[str] = Field(None, description=F["company"])
    email: Optional[str] = Field(None, description=F["email"])
    roles: Optional[Union[List[str], str]] = Field(None, description=F["roles"])
    location: Optional[str] = Field(None, description=F["location"])
    posted_at: Optional[str] = Field(None, description=F["posted_at"])
    posted_hours_ago: Optional[float] = Field(None, description=F["posted_hours_ago"])
    likes: Optional[int] = Field(None, description=F["likes"])
    comments: Optional[int] = Field(None, description=F["comments"])
    status: Optional[str] = Field(None, description=F["status"])
    agent: Optional[str] = Field(None, description=F["agent"])


class HiringPostBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    posts: List[dict] = Field(description="Up to 50 posts, each with the fields of a single post.",
                              max_length=50)


def _save(db: Session, data: dict) -> dict:
    try:
        post, created = hp.save_post(db, data)
        db.commit()
    except hp.PostError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    except IntegrityError:          # two agents sent the same post at once: the other one won
        db.rollback()
        post, created = hp.save_post(db, data)
        db.commit()
    return {**hp.to_dict(post), "created": created}


def _post(db: Session, post_id: str) -> HiringPost:
    import uuid
    try:
        pid = uuid.UUID(str(post_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="No such post")
    post = db.query(HiringPost).filter(HiringPost.id == pid).first()
    if post is None:
        raise HTTPException(status_code=404, detail="No such post")
    return post


@router.get("/hiring-posts", summary="List hiring posts (the Emails screen)")
def list_posts(status: Optional[str] = Query(None, description="new, emailed, skipped (comma-separated)"),
               q: Optional[str] = Query(None, description="Company, poster or post text"),
               fresh: bool = Query(False, description="Only posts under a day old"),
               since_hours: Optional[float] = Query(None, gt=0, description="Only posts from the last N hours"),
               limit: int = Query(100, ge=1, le=hp.MAX_ROWS), offset: int = Query(0, ge=0),
               db: Session = Depends(get_db)):
    """Newest first. `counts` is by status over every stored post."""
    try:
        query = hp.query(db, status=status, text=q, since_hours=since_hours, fresh=fresh)
    except hp.PostError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    from sqlalchemy import func
    counts = dict(db.query(HiringPost.status, func.count(HiringPost.id)).group_by(HiringPost.status).all())
    return {"total": query.count(), "counts": {s: counts.get(s, 0) for s in hp.STATUSES},
            "limits": {"max_age_hours": hp.MAX_AGE_HOURS, "max_likes": hp.MAX_LIKES},
            "posts": [hp.to_dict(p) for p in query.offset(offset).limit(limit).all()]}


@router.post("/hiring-posts", summary="Add a hiring post (or update it, matched by post_url)")
def add_post(body: HiringPostIn, db: Session = Depends(get_db)):
    """Stores one post. Sending a post_url that is already stored updates that post with the
    fields sent (likes and comments refresh; blanks never wipe a value) and answers `created: false`.
    `checks` says whether the post is under a day old and under 100 likes; posts outside those
    limits are stored anyway."""
    return _save(db, body.model_dump(exclude_unset=True))


@router.post("/hiring-posts/batch", summary="Add up to 50 hiring posts at once")
def add_posts(body: HiringPostBatch, db: Session = Depends(get_db)):
    """Each post is saved on its own: one bad post does not stop the rest. Answers
    `{saved, created, errors: [{index, post_url, error}], posts}`."""
    saved, errors = [], []
    for i, data in enumerate(body.posts):
        try:
            saved.append(_save(db, data))
        except HTTPException as exc:
            errors.append({"index": i, "post_url": (data or {}).get("post_url") if isinstance(data, dict) else None,
                           "error": exc.detail})
    return {"saved": len(saved), "created": sum(1 for p in saved if p["created"]), "errors": errors, "posts": saved}


@router.get("/hiring-posts/{post_id}", summary="One hiring post")
def get_post(post_id: str, db: Session = Depends(get_db)):
    return hp.to_dict(_post(db, post_id))


@router.patch("/hiring-posts/{post_id}", summary="Change a hiring post (status, summary, email, …)")
def patch_post(post_id: str, body: dict, db: Session = Depends(get_db)):
    """Any field of a post; a blank clears it (post_url and text cannot be cleared)."""
    post = _post(db, post_id)
    try:
        hp.update_post(db, post, body)
        db.commit()
    except hp.PostError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Another post already has that post_url")
    return hp.to_dict(post)


@router.delete("/hiring-posts/{post_id}", summary="Delete a hiring post")
def delete_post(post_id: str, db: Session = Depends(get_db)):
    db.delete(_post(db, post_id))
    db.commit()
    return {"deleted": True}
