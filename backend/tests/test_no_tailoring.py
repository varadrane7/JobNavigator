"""Per-job résumé tailoring is gone, and no seeded prompt may tell a model to make facts up."""
import re

from backend.tests.r4_support import client, _clear_running_state, _no_outbound  # noqa: F401

from backend.seed import DEFAULT_SETTINGS

_INVENT_PERMISSION = re.compile(
    r"\bMAY invent\b|invent(ed)? (realistic|believable|plausible)|plausible star|possibly invented|speculative gap",
    re.I)


def test_no_tailoring_prompt_is_seeded():
    assert not [k for k in DEFAULT_SETTINGS if "tailor_prompt" in k or k == "tailor_auto_quick_score"]


def test_no_seeded_default_permits_invented_facts():
    bad = [k for k, v in DEFAULT_SETTINGS.items() if _INVENT_PERMISSION.search(str(v[0]))]
    assert bad == []


def test_the_tailor_route_and_worker_are_removed():
    import backend.api.routes_resumes as rr
    assert not hasattr(rr, "tailor_resume") and not hasattr(rr, "_tailor_impl")
    paths = {(r.path, tuple(sorted(r.methods or ()))) for r in rr.router.routes}
    assert not [p for p in paths if p[0].endswith("/tailor")]


def test_an_old_tailored_resume_still_lists_and_opens(client, test_db):
    from backend.models.db import Job, Resume
    job = Job(external_id="t1", content_hash="t1c", company="Acme", title="PM", description="d")
    test_db.add(job)
    test_db.flush()
    base = Resume(name="Base", is_base=True, json_data={"summary": "s"})
    test_db.add(base)
    test_db.flush()
    old = Resume(name="Base -> Acme", is_base=False, parent_id=base.id, job_id=job.id,
                 json_data={"summary": "tailored then", "_tailor_context": {"job_description": "x"}})
    test_db.add(old)
    test_db.commit()
    listed = client.get("/api/resumes").json()
    assert str(old.id) in {r["id"] for r in listed}
    got = client.get(f"/api/resumes/{old.id}")
    assert got.status_code == 200 and got.json()["json_data"]["summary"] == "tailored then"
    assert got.json()["is_base"] is False
    assert client.get("/api/resumes/shelf").status_code == 200


def test_removed_tailoring_settings_are_ignored_not_refused(client):
    r = client.patch("/api/settings", json={"tailor_auto_quick_score": "full", "cv_tailor_prompt": "x"})
    assert r.status_code == 200 and r.json()["updated"] == []
    mixed = client.patch("/api/settings", json={"tailor_auto_quick_score": "full", "curate_count": "7"})
    assert mixed.status_code == 200 and mixed.json()["updated"] == ["curate_count"]
    assert client.patch("/api/settings", json={"totally_made_up_key": "1"}).status_code == 400


def test_cleanup_removes_the_tailoring_settings_for_every_account(test_db):
    from backend.models.db import Account, Setting
    from backend.seed import cleanup_removed_settings
    member = Account(name="M", role="member", color="#2e9e6b")
    test_db.add(member)
    test_db.commit()
    for aid in (None, member.id):
        test_db.add_all([Setting(key="cv_tailor_prompt", value="old", account_id=aid),
                         Setting(key="tailor_auto_quick_score", value="light", account_id=aid),
                         Setting(key="curate_count", value="9", account_id=aid)])
    test_db.commit()
    cleanup_removed_settings(test_db)
    keys = [r.key for r in test_db.query(Setting).all()]
    assert "cv_tailor_prompt" not in keys and "tailor_auto_quick_score" not in keys
    assert keys.count("curate_count") == 2


def test_persona_api_reports_the_drafts_flag_and_never_stores_it(client, test_db):
    from backend.accounts.settings import put_setting
    got = client.get("/api/persona").json()
    assert got["networking"]["outreach_drafts_enabled"] is True
    put_setting(test_db, "outreach_drafts_enabled", "false")
    test_db.commit()
    got = client.get("/api/persona").json()
    assert got["networking"]["outreach_drafts_enabled"] is False
    # a client echoing the block back: accepted, and the flag is not written into the persona JSON
    r = client.patch("/api/persona", json={"networking": {**got["networking"], "pitch": "p"}})
    assert r.status_code == 200
    from backend.models.db import Persona
    test_db.expire_all()
    stored = test_db.query(Persona).first().networking
    assert stored.get("pitch") == "p" and "outreach_drafts_enabled" not in stored
    assert r.json()["networking"]["outreach_drafts_enabled"] is False
