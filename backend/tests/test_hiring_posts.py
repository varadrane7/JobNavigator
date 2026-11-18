"""Hiring posts: the Emails screen's store, fed by outside agents over REST and the connector."""
from datetime import timedelta

from backend.models.db import HiringPost, utcnow
from backend.tests.r4_support import API_KEY, client  # noqa: F401 (fixture)
from backend.tests.test_connector import _bearer, _call
from backend.tests.test_mt_auth import browser, owner  # noqa: F401 (fixtures)

URL = "https://www.linkedin.com/posts/jane-doe_hiring-activity-7263"


def _post(**kw):
    return {"post_url": URL, "text": "We're hiring a founding engineer at Acme. DM me.", **kw}


def test_add_then_resend_updates_the_same_post(browser, test_db):
    r = browser.post("/api/hiring-posts", json=_post(
        summary="Acme wants a founding engineer.", author_name="Jane Doe", company="Acme",
        roles="Founding Engineer, Backend", posted_hours_ago=5, likes=40, agent="post-finder"))
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["created"] and first["status"] == "new" and first["roles"] == ["Founding Engineer", "Backend"]
    assert first["checks"]["under_a_day"] is True and first["checks"]["under_100_likes"] is True
    assert 4.9 < first["checks"]["age_hours"] < 5.1

    # same post, query string and trailing slash differ: one row, likes refresh, blanks never wipe
    again = browser.post("/api/hiring-posts", json=_post(post_url=URL + "/?utm_source=share", likes=130,
                                                         email="jane@acme.dev", summary="")).json()
    assert not again["created"] and again["id"] == first["id"]
    assert (again["likes"], again["email"], again["summary"]) == (130, "jane@acme.dev", "Acme wants a founding engineer.")
    assert again["checks"]["under_100_likes"] is False
    assert test_db.query(HiringPost).count() == 1


def test_bad_posts_are_422_with_a_reason(browser):
    cases = [({"text": "x"}, "post_url"), (_post(text=""), "text is required"),
             (_post(post_url="linkedin.com/x"), "full http(s) link"), (_post(likes=-1), "0 or more"),
             (_post(posted_at="yesterday"), "ISO 8601"), (_post(status="done"), "status must be one of"),
             (_post(email="nope"), "does not look like"),
             (_post(posted_at=(utcnow() + timedelta(days=2)).isoformat()), "future")]
    for body, why in cases:
        r = browser.post("/api/hiring-posts", json=body)
        assert r.status_code == 422, (body, r.text)
        assert why in str(r.json()["detail"]), (why, r.json())
    assert browser.post("/api/hiring-posts", json=_post(surprise=1)).status_code == 422


def test_batch_saves_the_good_ones_and_reports_the_bad(browser):
    r = browser.post("/api/hiring-posts/batch", json={"posts": [
        _post(), _post(post_url=URL + "-2", posted_at="2026-11-18T09:30:00Z"), {"post_url": URL + "-3"}]})
    body = r.json()
    assert (body["saved"], body["created"]) == (2, 2)
    assert body["errors"] == [{"index": 2, "post_url": URL + "-3", "error": "text is required"}]


def test_list_filter_patch_delete(browser, test_db):
    old = browser.post("/api/hiring-posts", json=_post(post_url=URL + "-old", posted_hours_ago=40, company="Oldco")).json()
    new = browser.post("/api/hiring-posts", json=_post(posted_hours_ago=2, company="Acme")).json()
    rows = browser.get("/api/hiring-posts").json()
    assert [p["id"] for p in rows["posts"]] == [new["id"], old["id"]]
    assert rows["counts"] == {"new": 2, "emailed": 0, "skipped": 0}
    assert rows["limits"] == {"max_age_hours": 24, "max_likes": 100}
    assert [p["id"] for p in browser.get("/api/hiring-posts?fresh=true").json()["posts"]] == [new["id"]]
    assert [p["id"] for p in browser.get("/api/hiring-posts?q=oldco").json()["posts"]] == [old["id"]]

    r = browser.patch(f"/api/hiring-posts/{new['id']}", json={"status": "emailed", "email": "", "summary": "s"})
    assert (r.json()["status"], r.json()["email"], r.json()["summary"]) == ("emailed", None, "s")
    assert browser.patch(f"/api/hiring-posts/{new['id']}", json={"text": ""}).status_code == 422
    assert [p["id"] for p in browser.get("/api/hiring-posts?status=emailed").json()["posts"]] == [new["id"]]
    assert browser.get("/api/hiring-posts?status=bogus").status_code == 422

    assert browser.delete(f"/api/hiring-posts/{old['id']}").json() == {"deleted": True}
    assert browser.get(f"/api/hiring-posts/{old['id']}").status_code == 404
    assert browser.get("/api/hiring-posts/not-an-id").status_code == 404


def test_connector_tools_add_list_and_update(browser, test_db, owner):
    read, write = _bearer(test_db, owner, "read"), _bearer(test_db, owner, "write")
    text, failed = _call(browser, read, "add_hiring_post", _post())
    assert failed and "read-only" in text

    added, failed = _call(browser, write, "add_hiring_post", _post(
        summary="Acme wants a founding engineer.", roles=["Founding Engineer"], likes=12, posted_hours_ago=3,
        company="Acme", author_name="Jane Doe", agent="post-finder"))
    assert not failed and added["status"] == "new" and added["agent"] == "post-finder"
    assert "already_stored" not in added
    same, _ = _call(browser, write, "add_hiring_post", _post(likes=20))
    assert same["already_stored"] and same["id"] == added["id"] and same["likes"] == 20

    rows, _ = _call(browser, read, "list_hiring_posts", {"fresh": True})
    assert rows["total"] == 1 and rows["posts"][0]["company"] == "Acme" and rows["posts"][0]["author"] == "Jane Doe"

    done, _ = _call(browser, write, "update_hiring_post", {"post_id": added["id"], "status": "emailed"})
    assert done["status"] == "emailed"
    text, failed = _call(browser, write, "add_hiring_post", {"post_url": URL, "text": "x", "likes": "lots"})
    assert failed and "whole number" in text
