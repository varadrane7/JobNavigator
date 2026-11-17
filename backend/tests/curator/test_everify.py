"""E-Verify: shown, never used (curator/everify.py)."""
import pytest

from backend.curator import everify
from backend.models.db import CompanyEverify, Job


@pytest.mark.parametrize("text", [
    "Acme participates in E-Verify.", "We participate in the E-Verify program.", "Acme is an E-Verify employer.",
    "We use E-Verify to confirm work eligibility.", "The company uses E-Verify.", "L3Harris Technologies is an E-Verify Employer.",
    "Equal opportunity employer. Acme is enrolled in E-Verify.", "We are a participant in E-Verify",
    "Acme participates in Everify", "*This organization participates in E-Verify.", "DSHS uses E-Verify.",
    "For US roles: Xometry participates in E-Verify and files Form I-9.", "As a federal contractor, we participate in E-Verify.",
])
def test_statements_of_participation_are_detected(text):
    assert everify.detect(text) == "yes"


@pytest.mark.parametrize("text", [
    "", None, "We verify your references.", "Acme does not participate in E-Verify.", "We do not use E-Verify.",
    "This employer is not an E-Verify employer.", "We may participate in E-Verify in the future.",
    "If we participate in E-Verify you will be asked for documents.", "Candidates must be eligible; E-Verify may apply.",
    "Learn about E-Verify at the DHS website.", "No E-Verify employer. Visa sponsorship is not available.",
    "You will complete Form I-9 and E-Verify after an offer.", "Verify your email address.",
    # staffing agencies and generalities: not a statement that THIS employer participates
    "Our client participates in E-Verify.", "Our clients are E-Verify employers.", "We place candidates with employers that participate in E-Verify.",
    "Many employers participate in E-Verify.", "The client is an E-Verify employer.", "Some of our partners use E-Verify.",
    "Staffing on behalf of a company that participates in E-Verify.", "E-Verify Employer", "An e-verify participating employer.",
    "Applicants participate in E-Verify after the offer.", "Candidates use E-Verify.",
])
def test_bare_negative_conditional_and_unrelated_mentions_are_not(text):
    assert everify.detect(text) is None


def test_the_job_text_does_not_cancel_a_negation_in_another_sentence():
    assert everify.detect("We do not sponsor. Acme participates in E-Verify.") == "yes"
    assert everify.detect("Acme participates in E-Verify. We do not sponsor visas.") == "yes"


def test_manual_status_needs_a_source_and_a_valid_value(test_db):
    with pytest.raises(ValueError):
        everify.set_status(test_db, "Acme", "maybe", "x")
    with pytest.raises(ValueError):
        everify.set_status(test_db, "Acme", "no", "  ")
    with pytest.raises(ValueError):
        everify.set_status(test_db, "", "no", "x")
    out = everify.set_status(test_db, "Acme", "NO", "DHS employer search, 2026-11")
    test_db.commit()
    assert out["status"] == "no" and everify.flag(test_db, "acme") == "no"


def test_unknown_is_the_default_and_no_is_never_automatic(test_db):
    assert everify.flag(test_db, "Nobody", "We never mention it") == "unknown"
    assert everify.flag(test_db, "Nobody", "Acme participates in E-Verify.") == "yes"       # shown, nothing written
    assert test_db.query(CompanyEverify).count() == 0
    assert everify.note_job(test_db, "Acme", "Not a mention at all") is False
    assert test_db.query(CompanyEverify).count() == 0


def test_a_detection_is_recorded_and_never_overwrites_a_hand_set_row(test_db):
    assert everify.note_job(test_db, "Acme", "Acme participates in E-Verify.") is True
    test_db.commit()
    row = test_db.get(CompanyEverify, "acme")
    assert row.status == "yes" and row.source == "job description" and row.checked_on
    everify.set_status(test_db, "Acme", "no", "their HR told me")
    test_db.commit()
    assert everify.note_job(test_db, "Acme", "Acme participates in E-Verify.") is False
    assert test_db.get(CompanyEverify, "acme").status == "no"
    assert everify.flag(test_db, "Acme", "Acme participates in E-Verify.") == "no"        # the owner's word stands


def _job(db, i, company, desc):
    db.add(Job(external_id=str(i), company=company, title="Engineer", url=f"https://x/{i}", description=desc, status="new"))


def test_backfill_is_a_dry_run_unless_applied(test_db):
    _job(test_db, 1, "Acme", "Acme participates in E-Verify.")
    _job(test_db, 2, "Acme", "Another Acme job, no mention.")
    _job(test_db, 3, "Beta", "We do not use E-Verify.")
    _job(test_db, 4, "Gamma", "Gamma is an E-Verify employer.")
    test_db.commit()
    out = everify.backfill(test_db)
    assert out == {"scanned": 4, "jobs_matching": 2, "companies_matching": 2, "companies_new": 2, "jobs_covered": 3}
    assert test_db.query(CompanyEverify).count() == 0
    everify.backfill(test_db, apply=True)
    assert {r.name_key: r.status for r in test_db.query(CompanyEverify).all()} == {"acme": "yes", "gamma": "yes"}
    assert everify.flag(test_db, "Acme", "Another Acme job, no mention.") == "yes"          # the flag is the company's


def test_scores_are_identical_with_and_without_the_flag(test_db, monkeypatch):
    """Nothing in scoring reads the flag or the phrase: rank() gives the same score, parts and order either way."""
    from datetime import datetime, timezone
    from backend.curator import run, scoring
    import inspect
    assert "verify" not in inspect.getsource(scoring).lower() and "everify" not in inspect.getsource(run.rank).lower()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(run, "_resume_names", lambda: {})
    cfg = {"weights": None, "target_min": 100000, "target_max": 150000, "countries": ["US"], "ignore_h1b": False}

    def job(i, company, desc):
        return {"id": i, "company": company, "title": "Backend Engineer", "url": "", "description": desc, "posted_at": now,
                "best_cv_score": 70 + i, "best_cv": None, "lca": 40, "approval": 0.8, "jd_flag": False,
                "salary_min": 120000, "salary_max": 160000, "source": "direct", "loc_country": "US"}

    def ranked():
        jobs = [job(1, "Acme", "Build APIs. Acme participates in E-Verify."), job(2, "Beta", "Build APIs."),
                job(3, "Gamma", "Build APIs. We do not use E-Verify.")]
        return [(j["id"], j["score"], j["parts"]) for j in run.rank(jobs, {}, {}, cfg, now)]
    plain = ranked()
    for company, status in (("Acme", "yes"), ("Beta", "no"), ("Gamma", "unknown")):
        everify.set_status(test_db, company, status, "test")
    test_db.commit()
    assert ranked() == plain
    # and the same jobs without the sentence score the same as with it
    bare = [job(1, "Acme", "Build APIs."), job(2, "Beta", "Build APIs."), job(3, "Gamma", "Build APIs.")]
    assert [(j["id"], j["score"]) for j in run.rank(bare, {}, {}, cfg, now)] == [(i, s_) for i, s_, _p in plain]


def test_the_flag_is_on_job_and_lead_payloads_and_settable_by_api_and_tool(api_client, test_db):
    from backend.connector import tools
    from backend.models.db import Contact
    _job(test_db, 9, "Acme", "Acme participates in E-Verify.")
    _job(test_db, 10, "Other", "Nothing.")
    test_db.add(Contact(name="P", company="Other", linkedin_url="p", status="new"))
    test_db.commit()
    body = api_client.get("/api/jobs").json()
    rows = body["jobs"] if isinstance(body, dict) else body
    assert {j["company"]: j["everify"] for j in rows} == {"Acme": "yes", "Other": "unknown"}
    assert api_client.get("/api/contacts").json()[0]["everify"] == "unknown"
    r = api_client.put("/api/everify", json={"company": "Other", "status": "no", "source": "asked HR"})
    assert r.status_code == 200 and r.json()["status"] == "no"
    assert api_client.put("/api/everify", json={"company": "Other", "status": "no", "source": ""}).status_code == 422
    assert api_client.get("/api/contacts").json()[0]["everify"] == "no"
    assert api_client.get("/api/everify").json()[0]["source"] == "asked HR"
    out = tools.set_everify(test_db, {"company": "Third", "status": "yes", "source": "their careers page"})
    assert out["status"] == "yes"
    with pytest.raises(tools.ToolError):
        tools.set_everify(test_db, {"company": "Third", "status": "sometimes", "source": "x"})
    assert {j["company"]: j["everify"] for j in tools.search_jobs(test_db, {})["jobs"]}["Acme"] == "yes"


def test_a_hand_set_row_of_any_status_wins_over_detection(test_db):
    text = "Acme participates in E-Verify."
    assert everify.flag(test_db, "Acme", text) == "yes"
    everify.set_status(test_db, "Acme", "unknown", "checked the DHS list: not there")
    test_db.commit()
    assert everify.flag(test_db, "Acme", text) == "unknown"          # the owner cleared a false yes
    assert everify.flags_for(test_db, ["Acme"]) == {"acme": "unknown"}
    everify.set_status(test_db, "Acme", "yes", "their careers page")
    test_db.commit()
    assert everify.flag(test_db, "Acme", "no mention") == "yes"


def test_company_names_are_one_key_the_way_the_app_reads_them(test_db):
    assert everify.name_key("Stripe, Inc.") == everify.name_key("Stripe") == everify.name_key(" STRIPE ") == "stripe"
    assert everify.name_key("Tata Technologies") != everify.name_key("Tata Consultancy Services")
    everify.set_status(test_db, "Stripe, Inc.", "yes", "their page")
    test_db.commit()
    assert everify.flag(test_db, "Stripe") == "yes" and everify.flag(test_db, "stripe llc") == "yes"


def test_note_job_survives_a_race_and_never_raises(test_db, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    text = "Acme participates in E-Verify."
    real_get = test_db.get
    state = {"n": 0}

    def racing_get(model, key, *a, **k):
        state["n"] += 1
        if state["n"] == 1 and model.__name__ == "CompanyEverify":
            # another run inserts the row between our read and our insert
            test_db.add(CompanyEverify(name_key=key, status="yes", source="job description"))
            test_db.flush()
            return None
        return real_get(model, key, *a, **k)
    monkeypatch.setattr(test_db, "get", racing_get)
    assert everify.note_job(test_db, "Acme", text) in (True, False)
    test_db.commit()
    assert test_db.query(CompanyEverify).count() == 1
    assert everify.note_job(test_db, "Acme", text) is True                 # and again: still one row
    assert test_db.query(CompanyEverify).count() == 1


def test_setting_by_hand_is_owner_only(api_client, test_db):
    from backend.models.db import Account
    from backend.connector import tools
    member = Account(name="International", role="member", color="#2e9e6b")
    test_db.add(member)
    test_db.commit()
    from backend.accounts.context import use_account
    with use_account(member.id):
        try:
            tools.set_everify(test_db, {"company": "Acme", "status": "yes", "source": "x"})
            allowed = True
        except tools.ToolError as e:
            allowed = "owner" not in str(e).lower()
    assert allowed is False
    assert tools.set_everify(test_db, {"company": "Acme", "status": "yes", "source": "x"})["status"] == "yes"
