"""`outreach_drafts_enabled`: a per-profile switch for LLM-drafted notes, emails and talking points.
On (the default) nothing changes; off skips only the drafting, people are still found and stored."""
import pytest

from backend.accounts.context import use_account
from backend.accounts.settings import put_setting
from backend.models.db import Account


from backend.curator.drafting import Sender

SENDER = Sender(name="Me", facts="Name: Me")


@pytest.fixture
def asks(monkeypatch):
    import backend.curator.people as people
    calls = []

    async def fake_ask(prompt, system, purpose, max_tokens=1500, job_id=None):
        calls.append(purpose)
        return "Subject: Hello\n\nBody" if purpose.endswith("_email") else "Hi there"
    monkeypatch.setattr("backend.curator.llm.ask", fake_ask)
    return calls


async def test_drafts_default_on(test_db, asks):
    import backend.curator.people as people
    assert people.drafts_enabled() is True
    d = await people.draft({"name": "A", "headline": "EM"}, "Acme", {"id": 1, "title": "SWE"}, SENDER)
    n = await people.draft_network({"name": "A", "headline": "EM"}, "Acme", SENDER)
    assert d["note"] == "Hi there" and n["email_draft"] == "Subject: Hello\n\nBody"
    assert asks == ["curator_outreach_draft", "curator_outreach_draft_email", "curator_network_draft",
                    "curator_network_draft_email"]


async def test_drafts_off_skips_the_llm_and_does_not_error(test_db, asks):
    import backend.curator.people as people
    put_setting(test_db, "outreach_drafts_enabled", "false")
    test_db.commit()
    assert people.drafts_enabled() is False
    d = await people.draft({"name": "A", "headline": "EM"}, "Acme", {"id": 1, "title": "SWE"}, SENDER)
    n = await people.draft_network({"name": "A", "headline": "EM"}, "Acme", SENDER)
    assert d == {"note": None, "email_draft": None} and n == {"note": None, "email_draft": None}
    assert asks == []


async def test_the_switch_is_per_profile(test_db, asks):
    import backend.curator.people as people
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    member = Account(name="International", role="member", color="#2e9e6b")
    test_db.add(member)
    test_db.commit()
    put_setting(test_db, "outreach_drafts_enabled", "false", account_id=member.id)   # the member's own row
    test_db.commit()
    with use_account(owner.id):
        assert people.drafts_enabled() is True
    with use_account(member.id):
        assert people.drafts_enabled() is False
        assert (await people.draft({"name": "A"}, "Acme", {}, SENDER))["note"] is None
    assert asks == []


async def test_scout_scoring_asks_for_no_draft_text_when_off(test_db, monkeypatch):
    import backend.curator.llm as llm
    import backend.curator.scout as scout
    seen = []

    async def fake_ask(prompt, system, purpose, max_tokens=1500, job_id=None):
        seen.append(prompt)
        return {"score": 80, "kind": "engineer", "why": "w", "talking_points": ["x"], "note": "n", "project": "P"}
    monkeypatch.setattr(llm, "ask_json", fake_ask)
    cand = {"name": "A", "headline": "h", "lead_kind": "alumni", "url": "u"}
    on = await scout.score_lead(cand, "brief")
    off = await scout.score_lead(cand, "brief", drafts=False)
    assert on["talking_points"] == ["x"] and "talking_points" in seen[0]
    assert off["score"] == 80 and off["why"] == "w" and off["project_ref"] == "P"
    assert off["talking_points"] == []
    assert '"note"' not in seen[0] and '"note"' not in seen[1] and "talking_points" not in seen[1]


def test_the_scout_prompt_takes_work_authorization_from_the_profile():
    import backend.curator.scout as scout
    assert "STEM OPT" not in scout._PROMPT and "F-1" not in scout._PROMPT
    assert "Work authorization" in scout._PROMPT


def test_the_switch_is_a_curator_setting_that_defaults_on():
    from backend.accounts.settings import SETTING_SECTIONS
    from backend.seed import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["outreach_drafts_enabled"][0] == "true"
    flat = str(SETTING_SECTIONS)
    assert "outreach_drafts_enabled" in flat
