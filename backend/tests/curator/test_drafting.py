"""Audience-aware drafting (curator/drafting.py): the sender facts, the prompt, and the validator seeing
exactly what the model saw."""
from types import SimpleNamespace

import pytest

from backend.curator import audience as aud
from backend.curator import drafting, note_check

PERSONA = SimpleNamespace(
    contact={"first_name": "Sam", "last_name": "Rivera", "email": "sam@example.com", "phone": "5550100"},
    work_auth={"work_auth_type": "F-1 OPT (post-completion), valid through Aug 8, 2027; eligible for 24-month STEM OPT extension",
               "requires_sponsorship_future": True},
    resume_content={
        "summary": "Backend engineer with a very long summary that is not part of the block",
        "education": [{"school": "Example Tech", "degree": "MS, Computer Science - GPA 3.90 / 4.00"}],
        "experience": [{"title": "Developer", "company": "Currentco", "date": "Feb 2026 - Present"},
                       {"title": "Engineer", "company": "Oldco", "date": "Jun 2022 - Aug 2024"}],
        "projects": [{"name": "Alpha", "description": "Python, Docker", "bullets": ["Builds a thing. It is quick."],
                      "url": "https://example.com/alpha"},
                     {"name": "Beta", "description": "Go", "bullets": ["Another thing"]},
                     {"name": "Gamma", "description": "Rust", "bullets": ["Third thing"]}]},
    networking={"pitch": "Pitch with 2M users", "visa_note": "Will need H-1B sponsorship later", "projects": []})


def test_sender_facts_hold_only_what_the_profile_stores():
    s = drafting.build_sender(PERSONA)
    f = s.facts
    assert "Name: Sam Rivera" in f and "MS, Computer Science, Example Tech" in f
    assert "Developer at Currentco (current)" in f and "Engineer at Oldco (past)" in f
    assert "Alpha" in f and "Beta" in f and "Gamma" not in f          # at most two projects
    assert "3.90" not in f and "GPA" not in f                          # no grade to tempt a number
    for absent in ("Kotak", "NJIT", "Fieldworker", "FieldWorker", "sam@example.com", "5550100", "2M", "Pitch",
                   "F-1", "OPT", "H-1B", "visa", "sponsor", "Headline"):
        assert absent not in f, absent
    assert any("headline" in g for g in s.gaps)                        # a missing headline is listed, not invented


def test_a_stored_headline_is_used_and_no_gap_is_listed():
    p = SimpleNamespace(**{**PERSONA.__dict__, "contact": {**PERSONA.contact, "headline": "Backend Engineer | Python"}})
    s = drafting.build_sender(p)
    assert "Headline: Backend Engineer | Python" in s.facts and not any("headline" in g for g in s.gaps)


def test_no_profile_gives_an_empty_block_and_a_gap():
    s = drafting.build_sender(None)
    assert s.facts == "" and s.gaps


SENDER = drafting.build_sender(PERSONA)
CARD = {"headline": "Staff Engineer at Acme", "location": "New York, NY", "past_text": "Oldco"}


@pytest.fixture
def capture(monkeypatch):
    """A fake model, and a spy on the validator: what the prompt held and what the validator was given."""
    seen = {"prompts": [], "inputs": [], "kinds": []}

    async def fake_ask(prompt, system, purpose, max_tokens=1500, job_id=None):
        seen["prompts"].append((prompt, system))
        return "Hi there, plain text."
    real = note_check.check_note

    def spy(note, inputs, names=(), job_applied=False, sender=(), recipient_terms=(), kind="note"):
        seen["inputs"].append(list(inputs))
        seen["kinds"].append(kind)
        return real(note, inputs, names, job_applied, sender, recipient_terms, kind)
    monkeypatch.setattr("backend.curator.llm.ask", fake_ask)
    monkeypatch.setattr(note_check, "check_note", spy)
    return seen


@pytest.mark.parametrize("who,marker", [
    ("alum", "an alum of one of the sender's schools"), ("ex_colleague", "past employer"), ("engineer", "an engineer on the team"),
    ("recruiter", "a recruiter"), ("hiring_manager", "wrote a post about a role"), ("post", "wrote a hiring post"),
    (None, "nothing in the request says how they relate"),
])
async def test_each_audience_gets_its_block_and_the_validator_sees_the_prompt_inputs(capture, who, marker):
    brief = drafting.Brief(sender=SENDER, name="Pat Lee", card=drafting.card_text(CARD),
                           evidence="We are hiring a platform engineer" if who in ("hiring_manager", "post") else "",
                           company="Acme", job_title="Platform Engineer", audience=who,
                           from_post=who in ("hiring_manager", "post"))
    await drafting.draft_text("note", brief, "t")
    prompt, system = capture["prompts"][0]
    assert marker in prompt
    assert capture["inputs"][0] == brief.inputs()
    for text in brief.inputs():                      # the validator got nothing the model did not read, and vice versa
        assert text in prompt
    assert "Name: Sam Rivera" in prompt and system == drafting.SYSTEM
    if who in ("hiring_manager", "post"):
        assert "The hiring post they wrote:" in prompt


@pytest.mark.parametrize("who", ["hiring_manager", "post"])
async def test_a_hiring_manager_without_a_post_is_written_to_generically(capture, who):
    # a profile snippet is evidence, but it is not a post: only a lead found through post search gets the post wording
    brief = drafting.Brief(sender=SENDER, name="Pat Lee", card="Engineering Manager at Acme", company="Acme",
                           evidence="Engineering Manager · Experience: Acme · Location: New York", audience=who, from_post=False)
    await drafting.draft_text("note", brief, "t")
    prompt = capture["prompts"][0][0]
    assert "nothing in the request says how they relate" in prompt and "wrote a post" not in prompt
    assert "The profile snippet they were found from:" in prompt and "The hiring post they wrote:" not in prompt


@pytest.mark.parametrize("kind", ["message", "follow_up", "email"])
async def test_other_kinds_use_their_own_validator_limits(capture, kind):
    brief = drafting.Brief(sender=SENDER, name="Pat Lee", card="Staff Engineer at Acme", company="Acme",
                           previous="Hi Pat, earlier note" if kind == "follow_up" else "", audience="alum")
    await drafting.draft_text(kind, brief, "t")
    assert capture["kinds"][0] == kind
    if kind == "follow_up":
        assert "Hi Pat, earlier note" in capture["prompts"][0][0] and "Hi Pat, earlier note" in capture["inputs"][0]


def test_no_work_authorisation_wording_in_any_prompt():
    for who in list(aud.AUDIENCES) + [None]:
        for kind in note_check.KINDS:
            brief = drafting.Brief(sender=SENDER, name="Pat Lee", card="Staff Engineer at Acme", company="Acme",
                                   job_title="Platform Engineer", evidence="hiring", previous="x", audience=who)
            text = brief.prompt(kind) + drafting.SYSTEM
            for banned in ("STEM", "OPT", "F-1", "H-1B", "valid through", "Aug 8", "2027", "holder"):
                assert banned not in text, (who, kind, banned)
    assert "applied" not in drafting.KIND_BLOCKS["email"]            # the email prompt no longer says he applied


def test_the_prompt_says_he_applied_only_when_he_did():
    b = drafting.Brief(sender=SENDER, name="P", company="Acme", job_title="Role", applied=False)
    assert "The sender has not applied to it." in b.prompt("note")
    assert "The sender has applied to it." in drafting.Brief(sender=SENDER, name="P", company="Acme", job_title="Role",
                                                     applied=True).prompt("note")
    assert "applied" not in drafting.Brief(sender=SENDER, name="P", company="Acme").prompt("note").replace(
        "Do not say the sender applied", "")


def test_plain_takes_off_labels_quotes_and_json():
    assert drafting.plain('"Hi Pat."') == "Hi Pat."
    assert drafting.plain("Note: Hi Pat.") == "Hi Pat."
    assert drafting.plain('{"note": "Hi Pat."}') == "Hi Pat."
    assert drafting.plain("  ") is None


# ── the validators for the other texts ───────────────────────────────────────

INPUTS = ["Name: Sam Rivera", "Pat Lee", "Staff Engineer at Acme", "Acme"]


def _p(text, kind="note", applied=False):
    return note_check.check_note(text, INPUTS, ["Sam Rivera", "Pat Lee"], applied, ["Name: Sam Rivera"], ["Acme", "Pat Lee"], kind)


def test_length_limits_per_kind():
    ok = "Hi Pat, short question about the team at Acme."
    assert _p(ok + " x" * 300, "note") and _p(ok, "note") == []
    assert _p("a " * 301, "message") and not _p("a " * 290, "message")
    assert _p("a " * 201, "follow_up") and not _p("a " * 150, "follow_up")
    body = "Subject: A chat\n\n" + "word " * 151
    assert any("words" in x for x in _p(body, "email"))
    assert any("Subject" in x for x in _p("no subject line here", "email"))
    assert any("subject is" in x for x in _p("Subject: " + "one two three four five six seven eight nine\n\nHi Pat", "email"))
    assert _p("Subject: A chat about Acme\n\nHi Pat, quick question about the team. Sam", "email") == []


@pytest.mark.parametrize("text", [
    "Hi Pat, I hope this finds you well.", "Hi Pat, your work is impressive.", "Hi Pat, I love your work.",
    "Hi Pat, two!! Marks!", "Hi Pat \U0001F600", "Hi Pat—quick question.",
    "Hi Pat, I am on F-1 OPT and need sponsorship.", "Hi Pat, I need a visa.", "Hi Pat, STEM OPT holder here.",
])
def test_style_and_work_authorisation_rules_apply_to_every_kind(text):
    for kind in ("note", "message", "follow_up"):
        assert _p(text, kind), (kind, text)


def test_an_unapplied_email_cannot_say_he_applied():
    assert _p("Subject: A chat\n\nHi Pat, I applied for the role at Acme.", "email")
    assert _p("Subject: A chat\n\nHi Pat, I applied for the role at Acme.", "email", applied=True) == []


async def test_a_failing_email_is_not_saved(monkeypatch):
    replies = ["Subject: A chat\n\nHi Pat, I need a visa.", "Subject: A chat\n\nHi Pat, I need visa sponsorship."]

    async def fake(prompt, system, purpose, max_tokens=1500, job_id=None):
        return replies.pop(0)
    monkeypatch.setattr("backend.curator.llm.ask", fake)
    brief = drafting.Brief(sender=SENDER, name="Pat Lee", card="Staff Engineer at Acme", company="Acme")
    res = await drafting.draft_text("email", brief, "t")
    assert res.note is None and res.manual and "visa" in res.issue.lower()


async def test_drafting_is_skipped_when_the_switch_is_off(test_db, monkeypatch):
    from backend.accounts.settings import put_setting
    from backend.curator import people
    called = []

    async def fake(*a, **k):
        called.append(1)
        return "x"
    monkeypatch.setattr("backend.curator.llm.ask", fake)
    put_setting(test_db, "outreach_drafts_enabled", "false")
    test_db.commit()
    assert await people.draft({"name": "A"}, "Acme", {"title": "t"}, SENDER) == {"note": None, "email_draft": None}
    assert await people.draft_network({"name": "A"}, "Acme", SENDER) == {"note": None, "email_draft": None}
    assert called == []


def test_the_fixed_prompt_states_nothing_about_the_sender_and_uses_no_gendered_pronouns():
    import re
    text = drafting.SYSTEM + " ".join(drafting.AUDIENCE_BLOCKS.values()) + " ".join(drafting.KIND_BLOCKS.values())
    assert not re.search(r"\b(?:he|his|him|himself|she|her)\b", text, re.I)
    assert "backend" not in drafting.SYSTEM.lower() and "full-time" not in drafting.SYSTEM and "early in" not in drafting.SYSTEM
    prompt = drafting.Brief(sender=SENDER, name="P", company="Acme", job_title="Role", previous="x").prompt("follow_up")
    assert not re.search(r"\b(?:he|his|him)\b", prompt.split("About the sender")[0] + prompt.split(SENDER.facts)[1], re.I)
    # a stored headline is the only description of the sender, and it travels in the facts block the validator sees
    p = SimpleNamespace(**{**PERSONA.__dict__, "contact": {**PERSONA.contact, "headline": "Data Engineer | Go"}})
    assert "Headline: Data Engineer | Go" in drafting.build_sender(p).facts


# ── POST /contacts/{id}/draft: gating, previous text, stale drafts ──────────────

@pytest.fixture
def drafting_env(api_client, test_db, monkeypatch):
    from backend.models.db import Persona
    test_db.add(Persona(id=1, contact={"first_name": "Sam", "last_name": "Rivera"}, resume_content=PERSONA.resume_content,
                        networking={}))
    test_db.commit()
    replies = []
    prompts = []

    async def fake(prompt, system, purpose, max_tokens=1500, job_id=None):
        prompts.append(prompt)
        return replies.pop(0) if replies else "Thanks for connecting, Pat. What was your path to the team at Acme?"
    monkeypatch.setattr("backend.curator.llm.ask", fake)
    return SimpleNamespace(client=api_client, db=test_db, replies=replies, prompts=prompts)


def _contact(db, **kw):
    from backend.models.db import Contact
    from backend.tz import local_today
    base = dict(name="Pat Lee", company="Acme", linkedin_url="p", status="connected", audience="alum",
                title="Staff Engineer at Acme", note="Hi Pat, the sent note", found_on=local_today())
    c = Contact(**{**base, **kw})
    db.add(c)
    db.commit()
    return c


def test_after_acceptance_needs_a_connected_lead(drafting_env):
    for status in ("new", "requested", "messaged", "replied", "skip"):
        c = _contact(drafting_env.db, status=status, linkedin_url=status)
        r = drafting_env.client.post(f"/api/contacts/{c.id}/draft", json={"stage": "after_acceptance"})
        assert r.status_code == 409 and "connected" in r.json()["detail"], status
    assert drafting_env.prompts == []
    ok = _contact(drafting_env.db, linkedin_url="ok")
    r = drafting_env.client.post(f"/api/contacts/{ok.id}/draft", json={"stage": "after_acceptance"})
    assert r.status_code == 200 and r.json()["message_stage"] == "after_acceptance"
    assert "an alum of one of the sender's schools" in drafting_env.prompts[0]


def test_a_follow_up_needs_one_still_to_send_and_an_earlier_sent_text(drafting_env):
    from datetime import timedelta
    from backend.tz import local_today
    today = local_today()
    c = _contact(drafting_env.db, status="connected", linkedin_url="c1")
    assert drafting_env.client.post(f"/api/contacts/{c.id}/draft", json={"stage": "follow_up"}).status_code == 409
    waiting = _contact(drafting_env.db, status="requested", linkedin_url="w", contacted_on=today - timedelta(days=5),
                       invited_on=today - timedelta(days=5), follow_ups_sent=2, follow_up_sent_on=today)
    r = drafting_env.client.post(f"/api/contacts/{waiting.id}/draft", json={"stage": "follow_up"})
    assert r.status_code == 409 and "no follow-up left" in r.json()["detail"]
    nothing = _contact(drafting_env.db, status="messaged", linkedin_url="n", note=None, contacted_on=today - timedelta(days=5))
    r = drafting_env.client.post(f"/api/contacts/{nothing.id}/draft", json={"stage": "follow_up"})
    assert r.status_code == 409 and "No earlier sent text" in r.json()["detail"]
    ok = _contact(drafting_env.db, status="requested", linkedin_url="ok", contacted_on=today - timedelta(days=5),
                  invited_on=today - timedelta(days=5))
    r = drafting_env.client.post(f"/api/contacts/{ok.id}/draft", json={"stage": "follow_up"})
    assert r.status_code == 200 and r.json()["message_stage"] == "follow_up_1"
    assert "Hi Pat, the sent note" in drafting_env.prompts[-1]            # the note that went out


def test_a_follow_up_refers_to_the_last_text_actually_sent_and_the_stage_follows_the_number(drafting_env):
    from datetime import timedelta
    from backend.tz import local_today
    today = local_today()
    c = _contact(drafting_env.db, status="messaged", linkedin_url="m", contacted_on=today - timedelta(days=10),
                 note="Hi Pat, a stale connection note", last_sent_text="Thanks Pat, what was your path?",
                 follow_ups_sent=1, follow_up_sent_on=today - timedelta(days=1), message_stage="follow_up_1")
    r = drafting_env.client.post(f"/api/contacts/{c.id}/draft", json={"stage": "follow_up"})
    assert r.status_code == 200 and r.json()["message_stage"] == "follow_up_2"
    assert "Thanks Pat, what was your path?" in drafting_env.prompts[-1] and "stale connection note" not in drafting_env.prompts[-1]


def test_a_failed_draft_clears_the_old_one(drafting_env):
    c = _contact(drafting_env.db, message_draft="an old draft that now looks current", message_stage="follow_up_1")
    drafting_env.replies[:] = ["I need a visa.", "Still a visa, sponsorship needed."]
    r = drafting_env.client.post(f"/api/contacts/{c.id}/draft", json={"stage": "after_acceptance"})
    assert r.status_code == 409
    drafting_env.db.refresh(c)
    assert c.message_draft is None and c.message_stage == "after_acceptance"


def test_the_people_endpoint_reports_whether_drafts_are_on(api_client, test_db):
    from backend.accounts.settings import put_setting
    assert api_client.get("/api/contacts/invites").json()["drafts_enabled"] is True
    put_setting(test_db, "outreach_drafts_enabled", "false")
    test_db.commit()
    assert api_client.get("/api/contacts/invites").json()["drafts_enabled"] is False
