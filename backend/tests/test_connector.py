"""The connector (backend/connector): /mcp, its tokens, the OAuth sign-in and the tools."""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.accounts.context import use_account
from backend.connector import tokens
from backend.models.db import Application, Contact, Job, Persona, Resume
from backend.tests.r4_support import API_KEY, client  # noqa: F401 (fixture)
from backend.tests.test_mt_auth import _member, _set_pin, _switch, browser, owner  # noqa: F401 (fixtures)

BASE = "http://testserver"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


def _acc(account) -> dict:
    return {"id": account.id, "name": account.name, "pin_hash": account.pin_hash}


def _bearer(db, account, scope="read") -> dict:
    return {"Authorization": "Bearer " + tokens.make_access(db, _acc(account), scope, "test")}


def _rpc(c, headers, method, params=None, msg_id=1):
    return c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": msg_id, "method": method,
                                                 "params": params or {}})


def _call(c, headers, name, args=None):
    """(parsed result or error text, isError)."""
    r = _rpc(c, headers, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    text = result["content"][0]["text"]
    return (text if result["isError"] else json.loads(text)), result["isError"]


def _seed(db, account_id, tag="a"):
    with use_account(account_id):
        job = Job(external_id=f"ext-{tag}", content_hash=f"h-{tag}", company=f"Co-{tag}", title=f"Engineer {tag}",
                  url=f"https://x.example/{tag}", source="test", status="applied", best_cv_score=81.4,
                  description="Build things. " * 10)
        db.add(job)
        db.flush()
        app = Application(job_id=job.id, status="applied", notes="first note")
        lead = Contact(name=f"Lead {tag}", company=f"Co-{tag}", title="EM", status="new", score=70,
                       linkedin_url=f"https://www.linkedin.com/in/lead-{tag}", why="hiring", job_id=job.id)
        resume = Resume(name=f"General {tag}", is_base=True,
                        json_data={"header": {"name": f"Person {tag}"}, "summary": f"Summary {tag}"})
        db.add_all([app, lead, resume, Persona(contact={"name": f"Person {tag}"},
                                               networking={"pitch": f"pitch {tag}"})])
        db.commit()
        return {"job": str(job.id), "app": str(app.id), "lead": str(lead.id), "resume": str(resume.id)}


# ── /mcp: auth ───────────────────────────────────────────────────────────────

def test_no_token_is_401_and_points_at_the_oauth_metadata(browser, owner):
    browser.headers.pop("X-API-Key", None)
    r = browser.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"'
    meta = browser.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == f"{BASE}/mcp" and meta["authorization_servers"] == [BASE]
    server = browser.get("/.well-known/oauth-authorization-server").json()
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert server["token_endpoint_auth_methods_supported"] == ["none"]
    assert server["registration_endpoint"] == f"{BASE}/oauth/register"


def test_the_install_key_and_an_account_api_token_do_not_open_mcp(browser, test_db, owner):
    from backend.accounts import auth as acct_auth
    api_token = acct_auth.make_api_token(test_db, _acc(owner))
    for headers in ({"X-API-Key": API_KEY}, {"Authorization": f"Bearer {API_KEY}"},
                    {"Authorization": f"Bearer {api_token}"}, {"X-Account-Token": api_token}):
        assert _rpc(browser, headers, "tools/list").status_code == 401
    # nor does a connector token open the dashboard API
    browser.headers.pop("X-API-Key", None)
    assert browser.get("/api/jobs", headers=_bearer(test_db, owner)).status_code == 401


def test_a_token_dies_with_revoke_and_with_a_pin_change(browser, test_db, owner):
    headers = _bearer(test_db, owner)
    assert _rpc(browser, headers, "ping").json()["result"] == {}
    assert browser.post("/api/connector/revoke").json() == {"revoked": 1}
    r = _rpc(browser, headers, "ping")
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]
    headers = _bearer(test_db, owner)
    assert _rpc(browser, headers, "ping").status_code == 200
    _set_pin(test_db, owner, "1234")
    assert _rpc(browser, headers, "ping").status_code == 401


# ── /mcp: protocol ───────────────────────────────────────────────────────────

def test_initialize_list_notification_and_unknown_method(browser, test_db, owner):
    headers = _bearer(test_db, owner)
    init = _rpc(browser, headers, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "t", "version": "1"}}).json()["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["capabilities"] == {"tools": {"listChanged": False}}
    assert init["serverInfo"]["name"] == "jobnavigator" and init["instructions"]
    assert _rpc(browser, headers, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"][
        "protocolVersion"] == "2025-11-25"
    r = browser.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""
    assert _rpc(browser, headers, "resources/list").json()["error"]["code"] == -32601
    assert browser.post("/mcp", headers=headers, content=b"{nope").status_code == 400
    assert browser.get("/mcp", headers=headers).status_code == 405
    batch = browser.post("/mcp", headers=headers, json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]).json()
    assert [m["id"] for m in batch] == [1, 2]


def test_a_read_token_sees_no_write_tools_and_cannot_call_them(browser, test_db, owner):
    ids = _seed(test_db, owner.id)
    read, write = _bearer(test_db, owner), _bearer(test_db, owner, "write")
    names = lambda h: {t["name"] for t in _rpc(browser, h, "tools/list").json()["result"]["tools"]}
    assert names(write) - names(read) == {"update_lead", "add_lead", "update_application", "log_note"}
    for tool in _rpc(browser, write, "tools/list").json()["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object" and tool["description"]
        assert tool["annotations"]["readOnlyHint"] == (tool["name"] in names(read))
    text, failed = _call(browser, read, "update_lead", {"lead_id": ids["lead"], "status": "requested"})
    assert failed and "read-only" in text
    with use_account(owner.id):
        assert test_db.query(Contact).one().status == "new"


# ── Tools ────────────────────────────────────────────────────────────────────

def test_tools_answer_for_the_tokens_account_only(browser, test_db, owner):
    mine = _seed(test_db, owner.id, "a")
    member = _member(test_db, name="Other")
    theirs = _seed(test_db, member.id, "b")
    headers = _bearer(test_db, owner)

    overview, _ = _call(browser, headers, "overview")
    assert overview["applications"]["total"] == 1 and overview["leads"]["by_status"] == {"new": 1}
    apps, _ = _call(browser, headers, "list_applications")
    assert [a["company"] for a in apps["applications"]] == ["Co-a"]
    jobs, _ = _call(browser, headers, "search_jobs", {"status": "applied"})
    assert [j["title"] for j in jobs["jobs"]] == ["Engineer a"] and jobs["jobs"][0]["fit"] == 81
    leads, _ = _call(browser, headers, "list_leads")
    assert [l["name"] for l in leads["leads"]] == ["Lead a"]
    resumes, _ = _call(browser, headers, "list_resumes")
    assert [r["name"] for r in resumes["resumes"]] == ["General a"]
    profile, _ = _call(browser, headers, "get_profile")
    assert profile == {"contact": {"name": "Person a"}, "networking": {"pitch": "pitch a"}}

    for name, args in (("get_job", {"job_id": theirs["job"]}), ("get_application", {"application_id": theirs["app"]}),
                       ("get_lead", {"lead_id": theirs["lead"]}), ("get_resume", {"resume": theirs["resume"]}),
                       ("get_resume", {"resume": "General b"})):
        text, failed = _call(browser, headers, name, args)
        assert failed and "Co-b" not in text and "Person b" not in text, (name, text)
    assert _call(browser, _bearer(test_db, member), "get_job", {"job_id": theirs["job"]})[0]["company"] == "Co-b"
    assert _call(browser, headers, "get_job", {"job_id": mine["job"]})[0]["description"].startswith("Build things.")


def test_get_resume_text_and_lean_rows(browser, test_db, owner):
    ids = _seed(test_db, owner.id)
    headers = _bearer(test_db, owner)
    resume, _ = _call(browser, headers, "get_resume", {"resume": "general A"})
    assert "Person a" in resume["text"] and "## Summary" in resume["text"] and "data" not in resume
    assert _call(browser, headers, "get_resume", {"resume": ids["resume"], "format": "json"})[0]["data"][
        "summary"] == "Summary a"
    row = _call(browser, headers, "list_applications")[0]["applications"][0]
    assert "next" not in row and "last_email" not in row       # empty fields are left out
    job, _ = _call(browser, headers, "get_job", {"job_id": ids["job"], "description": False})
    assert "description" not in job


def test_bad_calls_are_tool_errors_the_model_can_read(browser, test_db, owner):
    headers = _bearer(test_db, owner)
    for name, args, needle in (("nope", {}, "Unknown tool"), ("get_job", {}, "Missing required"),
                               ("get_job", {"job_id": "not-an-id"}, "must be an id"),
                               ("overview", {"x": 1}, "Unknown argument"),
                               ("get_profile", {"sections": ["demographics"]}, "sections may be")):
        text, failed = _call(browser, headers, name, args)
        assert failed and needle in text, (name, text)


def test_write_tools_change_the_lead_and_the_application(browser, test_db, owner):
    ids = _seed(test_db, owner.id)
    headers = _bearer(test_db, owner, "write")
    lead, _ = _call(browser, headers, "update_lead", {"lead_id": ids["lead"], "status": "requested",
                                                     "linkedin_note": "Hi!", "talking_points": ["x"]})
    assert (lead["status"], lead["linkedin_note"], lead["talking_points"], bool(lead["last_step"])) == (
        "requested", "Hi!", ["x"], True)
    text, failed = _call(browser, headers, "update_lead", {"lead_id": ids["lead"], "status": "married"})
    assert failed and "status must be one of" in text

    same, _ = _call(browser, headers, "add_lead", {"name": "Someone Else",
                                                  "linkedin_url": "https://www.linkedin.com/in/lead-a/?x=1"})
    assert same["already_a_lead"] and same["id"] == ids["lead"]
    new, _ = _call(browser, headers, "add_lead", {"name": "New Person", "company": "Co-z", "why": "alumni"})
    assert new["status"] == "new" and new["kind"] == "network" and new["source"] == "connector"
    assert "already_a_lead" not in new

    # an agent logs someone it already contacted; a second report moves the lead forward, never back
    sent, _ = _call(browser, headers, "add_lead", {
        "name": "Asha Rao", "company": "Co-q", "linkedin_url": "https://linkedin.com/in/asha-rao/",
        "status": "requested", "contacted_on": "2026-11-10", "linkedin_note": "Hi Asha", "agent": "muse",
        "kind": "alumni", "role": "engineer"})
    assert (sent["status"], sent["source"], sent["kind"], sent["last_step"], sent["linkedin_note"]) == (
        "requested", "agent:muse", "alumni", "2026-11-10", "Hi Asha")
    again, _ = _call(browser, headers, "add_lead", {"name": "Asha R.", "status": "replied", "email": "a@co-q.io",
                                                   "linkedin_url": "https://www.linkedin.com/in/asha-rao?trk=x"})
    assert again["already_a_lead"] and again["id"] == sent["id"]
    assert (again["status"], again["email"], again["name"]) == ("replied", "a@co-q.io", "Asha Rao")
    back, _ = _call(browser, headers, "add_lead", {"name": "Asha Rao", "company": "Co-q", "status": "requested"})
    assert back["id"] == sent["id"] and back["status"] == "replied"
    text, failed = _call(browser, headers, "add_lead", {"name": "X", "status": "married"})
    assert failed and "status must be one of" in text

    app, _ = _call(browser, headers, "update_application", {"application_id": ids["app"], "status": "interview",
                                                           "append_note": "recruiter call", "next": "prep"})
    assert app["status"] == "interview" and app["next"] == "prep"
    assert app["notes"].startswith("first note\n\n[") and app["notes"].endswith("] recruiter call")
    assert app["history"][-1]["to"] == "interview" and app["history"][-1]["by"] == "connector"


# ── Settings API ─────────────────────────────────────────────────────────────

def test_settings_token_is_for_the_callers_own_account(browser, test_db, owner):
    _seed(test_db, owner.id, "a")
    member = _member(test_db, name="Other", pin="4321")
    _seed(test_db, member.id, "b")
    assert _switch(browser, member.id, "4321").status_code == 200
    info = browser.get("/api/connector").json()
    assert info["url"] == f"{BASE}/mcp" and {"overview", "update_lead"} <= {t["name"] for t in info["tools"]}
    made = browser.post("/api/connector/token", json={"scope": "write", "label": "agent"}).json()
    assert made["scope"] == "write" and made["url"] == f"{BASE}/mcp"
    headers = {"Authorization": "Bearer " + made["token"]}
    assert [a["company"] for a in _call(browser, headers, "list_applications")[0]["applications"]] == ["Co-b"]
    assert "add_lead" in {t["name"] for t in _rpc(browser, headers, "tools/list").json()["result"]["tools"]}
    assert browser.post("/api/connector/token", json={"scope": "admin"}).status_code == 422
    # revoking is per account: the owner's token survives the member's revoke
    owner_headers = _bearer(test_db, owner)
    browser.post("/api/connector/revoke")
    assert _rpc(browser, headers, "ping").status_code == 401
    assert _rpc(browser, owner_headers, "ping").status_code == 200


# ── OAuth ────────────────────────────────────────────────────────────────────

def _register(c, uris=(CALLBACK,)):
    return c.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": list(uris)})


def _authorize_params(client_id, **over):
    return {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "st4te",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "scope": "read", **over}


def _sign_in(c):
    assert c.post("/api/auth/set-session", json={"api_key": API_KEY}).status_code == 200
    c.headers.pop("X-API-Key", None)


def _consent(c, params, write=False, decision="allow"):
    page = c.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and "name='consent'" in page.text, page.text
    consent = page.text.split("name='consent' value='")[1].split("'")[0]
    form = {**params, "consent": consent, "decision": decision}
    if write:
        form["write"] = "1"
    return c.post("/oauth/authorize", data=form, follow_redirects=False)


def _exchange(c, client_id, code, **over):
    return c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                        "redirect_uri": CALLBACK, "code_verifier": VERIFIER, **over})


def test_oauth_sign_in_end_to_end(browser, test_db, owner):
    _seed(test_db, owner.id)
    reg = _register(browser)
    assert reg.status_code == 201 and reg.json()["token_endpoint_auth_method"] == "none"
    client_id = reg.json()["client_id"]
    _sign_in(browser)
    params = _authorize_params(client_id)
    back = _consent(browser, params, write=True)
    assert back.status_code == 302 and back.headers["location"].startswith(CALLBACK + "?")
    query = parse_qs(urlsplit(back.headers["location"]).query)
    assert query["state"] == ["st4te"]
    code = query["code"][0]

    assert _exchange(browser, client_id, code, code_verifier="w" * 64).json()["error"] == "invalid_grant"
    assert _exchange(browser, client_id, code, redirect_uri=CALLBACK + "/x").json()["error"] == "invalid_grant"
    other = _register(browser, [CALLBACK + "2"]).json()["client_id"]
    assert _exchange(browser, other, code).json()["error"] == "invalid_grant"

    tok = _exchange(browser, client_id, code)
    assert tok.status_code == 200 and tok.headers["cache-control"] == "no-store"
    tok = tok.json()
    assert (tok["token_type"], tok["scope"], tok["expires_in"]) == ("Bearer", "write", 3600)
    assert _exchange(browser, client_id, code).json()["error"] == "invalid_grant"        # one use

    browser.cookies.clear()
    headers = {"Authorization": "Bearer " + tok["access_token"]}
    assert "update_lead" in {t["name"] for t in _rpc(browser, headers, "tools/list").json()["result"]["tools"]}
    assert _call(browser, headers, "overview")[0]["applications"]["total"] == 1
    assert _rpc(browser, {"Authorization": "Bearer " + tok["refresh_token"]}, "ping").status_code == 401

    again = browser.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                               "refresh_token": tok["refresh_token"]})
    assert again.status_code == 200 and again.json()["scope"] == "write"
    assert _rpc(browser, {"Authorization": "Bearer " + again.json()["access_token"]}, "ping").status_code == 200
    wrong = browser.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": other,
                                               "refresh_token": tok["refresh_token"]})
    assert (wrong.status_code, wrong.json()["error"]) == (400, "invalid_grant")
    with use_account(owner.id):
        tokens.revoke_all(test_db, owner.id)
    dead = browser.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                              "refresh_token": tok["refresh_token"]})
    assert (dead.status_code, dead.json()["error"]) == (400, "invalid_grant")


def test_consent_needs_the_dashboard_session_and_gives_that_account(browser, test_db, owner):
    member = _member(test_db, name="Other", pin="4321")
    _seed(test_db, member.id, "b")
    client_id = _register(browser).json()["client_id"]
    params = _authorize_params(client_id)

    browser.headers.pop("X-API-Key", None)                      # a browser that never signed in
    page = browser.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and "name='consent'" not in page.text and "Continue" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    posted = browser.post("/oauth/authorize", data={**params, "decision": "allow", "consent": "x"},
                          follow_redirects=False)
    assert posted.status_code == 400 and "location" not in posted.headers

    browser.headers["X-API-Key"] = API_KEY
    assert _switch(browser, member.id, "4321").status_code == 200
    _sign_in(browser)
    back = _consent(browser, params)
    code = parse_qs(urlsplit(back.headers["location"]).query)["code"][0]
    tok = _exchange(browser, client_id, code).json()
    assert tok["scope"] == "read"
    headers = {"Authorization": "Bearer " + tok["access_token"]}
    assert [a["company"] for a in _call(browser, headers, "list_applications")[0]["applications"]] == ["Co-b"]

    denied = _consent(browser, params, decision="deny")
    assert parse_qs(urlsplit(denied.headers["location"]).query) == {"error": ["access_denied"], "state": ["st4te"]}


def test_authorize_never_redirects_to_a_foreign_address(browser, owner):
    client_id = _register(browser).json()["client_id"]
    _sign_in(browser)
    for params in (_authorize_params(client_id, redirect_uri="https://evil.example/cb"),
                   _authorize_params("made-up-client"), _authorize_params(client_id + "x")):
        r = browser.get("/oauth/authorize", params=params, follow_redirects=False)
        assert r.status_code == 400 and "location" not in r.headers
    # a consent made for one request does not approve another
    page = browser.get("/oauth/authorize", params=_authorize_params(client_id))
    consent = page.text.split("name='consent' value='")[1].split("'")[0]
    swapped = browser.post("/oauth/authorize", follow_redirects=False, data={
        **_authorize_params(client_id, code_challenge="A" * 43), "consent": consent, "decision": "allow"})
    assert swapped.status_code == 400
    # PKCE is required
    plain = browser.get("/oauth/authorize", params=_authorize_params(client_id, code_challenge_method="plain"),
                        follow_redirects=False)
    assert plain.status_code == 302 and "error=invalid_request" in plain.headers["location"]


@pytest.mark.parametrize("uris, ok", [
    ([CALLBACK], True), (["http://localhost:3118/callback"], True), (["http://127.0.0.1/cb"], True),
    (["http://evil.example/cb"], False), (["javascript:alert(1)"], False), ([], False),
    (["https://a.example/cb#frag"], False), ("https://a.example/cb", False),
])
def test_register_accepts_https_and_loopback_only(browser, owner, uris, ok):
    r = browser.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": uris})
    assert r.status_code == (201 if ok else 400)


def test_a_loopback_client_may_come_back_on_any_port(browser, owner):
    client_id = _register(browser, ["http://localhost:3118/callback"]).json()["client_id"]
    _sign_in(browser)
    ok = _authorize_params(client_id, redirect_uri="http://localhost:50123/callback")
    assert "name='consent'" in browser.get("/oauth/authorize", params=ok).text
    bad = _authorize_params(client_id, redirect_uri="http://localhost:50123/other")
    assert browser.get("/oauth/authorize", params=bad).status_code == 400


# ── POST /api/contacts: the same upsert for scripts and agents with an API token ──

def test_rest_contacts_adds_one_or_many_and_upserts(browser, test_db, owner):
    ids = _seed(test_db, owner.id)
    one = browser.post("/api/contacts", json={"name": "Priya N", "company": "Co-r", "status": "messaged",
                                              "email_draft": "Hello", "agent": "muse", "job_id": ids["job"]})
    assert one.status_code == 200, one.text
    row = one.json()
    assert (row["created"], row["status"], row["source"], row["job_id"], row["lead_kind"], row["email_draft"]) == (
        True, "messaged", "agent:muse", ids["job"], "job", "Hello")
    assert row["last_action_at"]

    many = browser.post("/api/contacts", json=[
        {"name": "Priya N", "company": "co-r", "status": "requested", "title": "EM"},
        {"name": "Second Person", "linkedin_url": "https://www.linkedin.com/in/second/"}])
    assert many.status_code == 200, many.text
    first, second = many.json()
    assert (first["created"], first["id"], first["status"], first["title"]) == (False, row["id"], "messaged", "EM")
    assert (second["created"], second["status"], second["source"]) == (True, "new", "api")

    bad = browser.post("/api/contacts", json=[{"name": "Fine"}, {"name": "Bad", "status": "married"}])
    assert bad.status_code == 422 and "contact 1 (Bad)" in bad.json()["detail"]
    assert test_db.query(Contact).filter(Contact.name == "Fine").count() == 0
    assert browser.post("/api/contacts", json={"name": "Z", "job_id": "999999"}).status_code == 422
    assert browser.post("/api/contacts", json={"name": "Z", "pin": "1"}).status_code == 422


def test_insights_and_the_journal(browser, test_db, owner):
    ids = _seed(test_db, owner.id)
    headers = _bearer(test_db, owner, "write")
    out, failed = _call(browser, headers, "insights")
    assert not failed and out["applications"] == 1 and out["window"] == "all time"
    assert out["by_lane"][0]["applied"] == 1 and out["by_source"][0]["heard_pct"] in (0, 100)
    assert out["flags"]["with_a_contacted_person_at_the_company"] == 0
    _call(browser, headers, "update_lead", {"lead_id": ids["lead"], "status": "requested"})
    again, _ = _call(browser, headers, "insights", {"since_days": 3650})
    assert again["window"] == "last 3650 days" and sum(r["contacted"] for r in again["outreach"]["by_role"].values()) == 1

    saved, _ = _call(browser, headers, "log_note", {"note": "max years 6 -> 5", "topic": "curator", "agent": "claude"})
    assert saved["saved"] and saved["topic"] == "curator"
    notes, _ = _call(browser, headers, "journal", {"query": "max years"})
    assert notes["total"] == 1 and notes["entries"][0] == {**notes["entries"][0], "by": "claude", "note": "max years 6 -> 5"}
    text, failed = _call(browser, _bearer(test_db, owner), "log_note", {"note": "x"})
    assert failed and "read-only" in text
