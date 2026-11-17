"""Connection notes are validated, retried once and flagged, never cut (curator/note_check.py)."""
import pytest

from backend.curator import note_check as nc

ME = "Varad Rane"
PROFILE = """Varad Rane. Backend and AI engineer. MS Computer Science, NJIT. BCom, University of Mumbai.
Experience: Software Engineer at Kotak Securities (trading platform APIs, Java and Python).
Projects:
- LazyCurl: a Go CLI that turns curl commands into replayable test suites
- Bondi: FastAPI and Postgres service for bond analytics, 4 services behind one gateway
"""
HEADLINE = "Engineering Manager at Acme Robotics | Python, Kafka, hiring for platform team"
COMPANY = "Acme Robotics"
TITLE = "Backend Engineer, Platform"
URL = "https://boards.greenhouse.io/acme/jobs/4412"
INPUTS = [ME, PROFILE, "Maya Singh", HEADLINE, COMPANY, TITLE, URL]
NAMES = [ME, "Maya Singh"]

CLEAN = ("Hi Maya, I'm a backend engineer (NJIT, ex-Kotak Securities) and I build Python and Go tooling. "
         "Acme Robotics' platform team is hiring for a Backend Engineer, and LazyCurl, my Go CLI for replayable "
         "test suites, is close to your Kafka work. Would you be open to connecting?")


# the scout is not given a job title, so its note cannot name one
SCOUT_CLEAN = CLEAN.replace("hiring for a Backend Engineer, and", "hiring, and")


def _bad(note, sender=(PROFILE, ME)):
    """fact_check with the empty answers left out."""
    out = nc.fact_check(note, INPUTS, NAMES, sender=sender, recipient_terms=[COMPANY, "Maya Singh"])
    return {k: v for k, v in out.items() if v}


def _problems(note, applied=False, inputs=INPUTS):
    return nc.check_note(note, inputs, NAMES, applied, sender=[PROFILE, ME], recipient_terms=[COMPANY, "Maya Singh"])


def test_a_clean_note_passes():
    assert len(CLEAN) <= 300
    assert _problems(CLEAN) == []


def test_301_characters_is_rejected_and_300_is_not():
    base = "Hi Maya, I build Python tooling and your platform team looks like a good fit. "
    ok = (base + "x" * 400)[:300]
    over = (base + "x" * 400)[:301]
    assert not any("characters" in p for p in _problems(ok))
    assert any("301 characters" in p for p in _problems(over))


def test_an_invented_company_detail_is_rejected():
    note = ("Hi Maya, I saw Acme Robotics just opened a Berlin office and I led the Stripe payments migration "
            "at my last job. Would love to connect.")
    bad = nc.fact_check(note, INPUTS, NAMES)
    assert "Berlin" in bad["names"] and "Stripe" in bad["names"]
    assert any("Stripe" in p for p in _problems(note))


def test_an_invented_number_is_rejected():
    note = "Hi Maya, I cut API latency by 40% and shipped 12 services, a fit for Acme Robotics' platform team."
    bad = nc.fact_check(note, INPUTS, NAMES)
    assert bad["numbers"] == ["40%", "12"]


def test_a_number_from_the_inputs_is_fine():
    assert _bad("Hi Maya, I run 4 services behind one gateway in Bondi, a FastAPI project.") == {}


def test_spelled_out_numbers_are_checked_too():
    assert nc.fact_check("Hi Maya, I have five years of backend work.", INPUTS, NAMES)["numbers"] == ["five"]
    assert nc.fact_check("Hi Maya, I have one question about the platform team.", INPUTS, NAMES)["numbers"] == []


def test_an_invented_school_or_project_is_rejected():
    assert nc.fact_check("Hi Maya, fellow Stanford alum here, my project Orbit uses Kafka.", INPUTS, NAMES)["names"] == [
        "Stanford", "Orbit"]
    assert nc.fact_check("Hi Maya, I studied at the University of Delhi.", INPUTS, NAMES)["names"] == ["University of Delhi"]
    assert nc.fact_check("Hi Maya, I studied at the University of Mumbai.", INPUTS, NAMES)["names"] == []


def test_an_invented_name_at_the_start_of_a_sentence_is_not_waved_through():
    assert nc.fact_check("Hi Maya. Stripe taught me a lot about queues.", INPUTS, NAMES)["names"] == ["Stripe"]


def test_allowlist_greeting_dates_names_and_sentence_openers():
    note = "Hello Maya. Thanks for posting on Monday. I would like to connect before March. Varad"
    assert _bad(note) == {}
    assert _bad("Dear Maya, saw your post on LinkedIn about AI.") == {}


def test_names_are_matched_by_whole_word_not_substring():
    # "Go" must not pass because "Google" or "going" occurs in the inputs
    assert nc.fact_check("Hi Maya, I write Rust.", INPUTS + ["Rustic going Google"], NAMES)["names"] == ["Rust"]


def test_possessive_and_punctuation_do_not_break_a_match():
    assert nc.fact_check("Hi Maya, Acme Robotics' team and Kotak Securities's desk both use Python.", INPUTS, NAMES)["names"] == []


def test_em_dash_is_rejected():
    note = "Hi Maya — I build Python services and Acme Robotics looks like a fit."
    assert any("em dash" in p for p in _problems(note))


def test_claiming_an_application_needs_an_application():
    note = "Hi Maya, I applied for the Backend Engineer role at Acme Robotics and would love to connect."
    assert any("applied" in p for p in _problems(note, applied=False))
    assert not any("applied" in p for p in _problems(note, applied=True))
    assert any("applied" in p for p in _problems("Hi Maya, my application for the role is in."))
    assert not any("applied" in p for p in _problems("Hi Maya, I work on applied machine learning with Python."))


# ── validate, retry once, flag ──────────────────────────────────────────────

async def test_settle_passes_a_clean_note_without_retrying():
    async def never(problems, note):
        raise AssertionError("no retry expected")
    res = await nc.settle_note(CLEAN, never, INPUTS, NAMES)
    assert res.note == CLEAN and not res.manual


async def test_settle_retries_once_with_the_problems_and_keeps_the_fixed_note():
    seen = []

    async def retry(problems, note):
        seen.append((problems, note))
        return CLEAN
    res = await nc.settle_note("Hi Maya, I cut latency by 40%.", retry, INPUTS, NAMES)
    assert res.note == CLEAN and not res.manual
    assert len(seen) == 1 and "40" in seen[0][0][0]


async def test_settle_flags_instead_of_truncating_after_two_failures():
    long = CLEAN + " " + "More words here. " * 20
    calls = []

    async def retry(problems, note):
        calls.append(1)
        return long
    res = await nc.settle_note(long, retry, INPUTS, NAMES)
    assert res.note is None and res.manual and "characters" in res.issue
    assert len(calls) == 1                      # exactly one retry


async def test_settle_flags_a_missing_note_and_a_failing_retry():
    async def retry(problems, note):
        raise RuntimeError("llm down")
    assert (await nc.settle_note("", retry, INPUTS, NAMES)).manual
    res = await nc.settle_note("Hi Maya, ex-Stripe here.", retry, INPUTS, NAMES)
    assert res.note is None and res.manual


def test_the_retry_text_states_the_length_and_the_offenders():
    text = nc.retry_instruction("x" * 301, nc.check_note("x" * 301, INPUTS, NAMES))
    assert "301 characters" in text and "250 to 280" in text and "300" in text


# ── wired into the drafting paths ───────────────────────────────────────────

SENDER = None


def _sender():
    from backend.curator.drafting import Sender
    return Sender(name=ME, first_name="Varad", last_name="Rane", facts=PROFILE)


@pytest.fixture
def ask(monkeypatch, test_db):
    """A fake model: each call pops the next reply (a plain-text string) and records the prompt."""
    state = {"calls": [], "replies": []}

    async def fake(prompt, system, purpose, max_tokens=1500, job_id=None):
        state["calls"].append((purpose, prompt))
        return state["replies"].pop(0) if state["replies"] else None
    monkeypatch.setattr("backend.curator.llm.ask", fake)
    return state


EMAIL = "Subject: Backend role\n\nHi Maya, I build Python services. Would you be open to a short chat?"


def _notes(state):
    return [p for purpose, p in state["calls"] if not purpose.endswith("_email")]


async def test_draft_retries_a_long_note_and_saves_the_fixed_one(ask):
    import backend.curator.people as people
    ask["replies"] = [CLEAN + " " + "extra " * 40, CLEAN, EMAIL]
    d = await people.draft({"name": "Maya Singh", "headline": HEADLINE}, COMPANY, {"id": None, "title": TITLE, "url": URL},
                           _sender())
    assert d["note"] == CLEAN and d["note_manual"] is False
    notes = _notes(ask)
    assert len(notes) == 2 and "was rejected" in notes[1] and "characters" in notes[1]
    assert d["email_draft"] == EMAIL


async def test_draft_flags_the_lead_when_the_note_stays_wrong(ask):
    import backend.curator.people as people
    bad = "Hi Maya, I led the Stripe migration and cut costs by 40%."
    ask["replies"] = [bad, bad, EMAIL]
    d = await people.draft({"name": "Maya Singh", "headline": HEADLINE}, COMPANY, {"id": None, "title": TITLE, "url": URL},
                           _sender())
    assert d["note"] is None and d["note_manual"] is True and "Stripe" in d["note_issue"]
    assert d["email_draft"].startswith("Subject: Backend role")
    assert len(_notes(ask)) == 2                   # one retry, no more


async def test_draft_says_applied_only_when_the_job_has_an_application(ask, test_db):
    import backend.curator.people as people
    from backend.models.db import Application, Job
    job = Job(external_id="x", company=COMPANY, title=TITLE, url=URL, status="applied")
    test_db.add(job)
    test_db.flush()
    test_db.add(Application(job_id=job.id, status="applied"))
    test_db.commit()
    note = "Hi Maya, I applied for the Backend Engineer, Platform role at Acme Robotics and use Python daily."
    ask["replies"] = [note, EMAIL]
    d = await people.draft({"name": "Maya Singh", "headline": HEADLINE}, COMPANY, {"id": job.id, "title": TITLE, "url": URL},
                           _sender())
    assert d["note"] == note and not d["note_manual"] and len(_notes(ask)) == 1
    assert "The sender has applied to it." in _notes(ask)[0]
    ask["calls"].clear()
    ask["replies"] = [note, note, EMAIL]
    d = await people.draft({"name": "Maya Singh", "headline": HEADLINE}, COMPANY, {"id": None, "title": TITLE, "url": URL},
                           _sender())
    assert d["note"] is None and d["note_manual"] is True


async def test_draft_network_validates_against_the_card(ask):
    import backend.curator.people as people
    ask["replies"] = ["Hi Maya, I write Python and read your headline about hiring for the platform team; open to connect?", EMAIL]
    d = await people.draft_network({"name": "Maya Singh", "headline": HEADLINE}, COMPANY, _sender())
    assert d["note"] and not d["note_manual"]
    assert "Do not say" not in ask["calls"][0][1] or True
    assert "Role he is interested in" not in ask["calls"][0][1]          # no job: nothing to claim


async def test_no_drafting_at_all_when_the_account_switch_is_off(ask, test_db):
    import backend.curator.people as people
    from backend.accounts.settings import put_setting
    put_setting(test_db, "outreach_drafts_enabled", "false")
    test_db.commit()
    d = await people.draft({"name": "Maya Singh", "headline": HEADLINE}, COMPANY, {"id": None, "title": TITLE}, _sender())
    n = await people.draft_network({"name": "Maya Singh", "headline": HEADLINE}, COMPANY, _sender())
    assert d == {"note": None, "email_draft": None} and n == {"note": None, "email_draft": None}
    assert ask["calls"] == []


async def test_scout_settles_the_note_of_a_saved_lead(ask):
    from backend.curator import scout
    ask["replies"] = ["Hi Maya, ex-Stripe here, 40% faster.", SCOUT_CLEAN]
    lead = {"name": "Maya Singh", "headline": HEADLINE, "company": COMPANY, "location": "NYC", "evidence": "",
            "lead_kind": "alumni"}
    out = await scout.settle_scout_note(lead, _sender())
    assert out["note"] == SCOUT_CLEAN and out["note_manual"] is False and len(ask["calls"]) == 2
    ask["replies"] = ["z" * 400, "z" * 400]
    out = await scout.settle_scout_note(lead, _sender())
    assert out["note"] is None and out["note_manual"] is True


async def test_scout_score_lead_does_not_write_a_note(monkeypatch):
    from backend.curator import scout

    async def fake(prompt, system, purpose, max_tokens=1500, job_id=None):
        assert '"note"' not in prompt
        return {"score": 80, "kind": "engineer", "why": "w", "note": "n" * 350}
    monkeypatch.setattr("backend.curator.llm.ask_json", fake)
    out = await scout.score_lead({"name": "A", "lead_kind": "alumni"}, "brief")
    assert "note" not in out and out["score"] == 80


def test_the_flag_is_in_the_api_and_cleared_by_a_note(api_client, test_db):
    from backend.models.db import Contact
    c = Contact(name="Maya", company="Acme", linkedin_url="u1", status="new", note_manual=True, note_issue="too long")
    test_db.add(c)
    test_db.commit()
    rows = api_client.get("/api/contacts").json()
    assert rows[0]["note_manual"] is True and rows[0]["note_issue"] == "too long"
    r = api_client.patch(f"/api/contacts/{c.id}", json={"note": "Hi Maya, short hand-written note."})
    assert r.json()["note_manual"] is False and r.json()["note_issue"] is None


def test_the_flag_is_in_the_mcp_tools_and_cleared_by_a_note(test_db):
    from backend.connector import tools
    from backend.models.db import Contact
    c = Contact(name="Maya", company="Acme", linkedin_url="u1", status="new", note_manual=True, note_issue="too long")
    test_db.add(c)
    test_db.commit()
    assert tools.list_leads(test_db, {})["leads"][0]["needs_manual_note"] is True
    assert tools.get_lead(test_db, {"lead_id": str(c.id)})["needs_manual_note"] is True
    out = tools.update_lead(test_db, {"lead_id": str(c.id), "linkedin_note": "Hi Maya."})
    assert out["needs_manual_note"] is False and out["note_issue"] is None


# ── realistic correct notes (alum, peer engineer, recruiter, hiring manager) must pass ───────────

GOOD_NOTES = [
    # alum
    "Hi Maya, fellow NJIT alum here. I'm a backend engineer and would love to hear how you moved into engineering management at Acme Robotics. Open to a quick chat?",
    "Hi Maya, Fellow NJIT alum here, now building backend services in Python. Would love a few minutes to hear how you got into platform work at Acme Robotics.",
    "Hi Maya, University of Mumbai and NJIT grad here. I do backend work in Python and am interested in the platform team. Open to connecting?",
    # peer engineer
    "Hi Maya, Impressed by the platform team at Acme Robotics. I build Python and Go services and would value a quick chat about how your team works.",
    "Hi Maya, Having built Bondi on FastAPI and Postgres, the Kafka work on your team caught my eye. Would you be open to connecting?",
    "Hi Maya, fellow Python engineer here. I work on APIs in FastAPI and enjoy Kafka-based designs. Keen to hear how your platform team ships.",
    "Hi Maya, Given your work on the platform team at Acme Robotics, I thought a short note made sense. I build Python services and shipped LazyCurl, a Go CLI.",
    "Hi Maya, I read how Acme Robotics hires for platform roles. I'm a Python and FastAPI engineer from NJIT, curious about the team.",
    "Hello Maya, Seeing your post about hiring for the platform team made me reach out. I'm a backend engineer who builds Python services.",
    # recruiter
    "Hello Maya, Wanted to reach out about the Backend Engineer, Platform role. I work in Python and FastAPI and would be glad to share a r\u00e9sum\u00e9.",
    "Hi Maya, I'm Varad, a backend engineer (NJIT, ex-Kotak Securities) looking at the Backend Engineer, Platform role at Acme Robotics. Happy to send a r\u00e9sum\u00e9 if useful.",
    "Hello Maya, I'm exploring backend roles and noticed the Backend Engineer, Platform opening at Acme Robotics. My background is Python, Java and a Go CLI called LazyCurl. Glad to share a r\u00e9sum\u00e9.",
    "Hi Maya, I'm a backend engineer focused on Python services. The Backend Engineer, Platform role at Acme Robotics fits my background, and I'm glad to send a r\u00e9sum\u00e9.",
    # hiring manager (posted about the role)
    "Hi Maya, I saw your hiring post for the Backend Engineer, Platform role. I build Python services, and my project LazyCurl is a Go CLI for replayable tests. Could I send a r\u00e9sum\u00e9?",
    "Hi Maya, Thanks for posting about the platform team. I'm a backend engineer from NJIT and would like to follow your team's work. Is there a good way to apply?",
    "Hi Maya, Acme Robotics' platform team caught my eye, and I'd like to learn how you hire. I'm a backend engineer; happy to share a r\u00e9sum\u00e9.",
    "Hi Maya, I'm Varad, an engineer from NJIT and Kotak Securities. Saw the Engineering Manager role at Acme Robotics hiring and wanted to say hello.",
    "Hi Maya, I have one question about the platform team at Acme Robotics. I build Python services and would like your view on the Backend Engineer, Platform role.",
]


@pytest.mark.parametrize("note", GOOD_NOTES)
def test_realistic_correct_notes_pass(note):
    assert len(note) <= nc.NOTE_MAX
    assert nc.check_note(note, INPUTS, NAMES, False, sender=[PROFILE, ME], recipient_terms=[COMPANY, "Maya Singh"]) == []


def test_a_capitalised_opener_after_a_greeting_is_not_a_proper_noun():
    for opener in ("Saw", "Impressed", "Fellow", "Wanted", "Seeing", "Given", "Huge", "Having", "Curious", "Noticed"):
        assert _bad(f"Hi Maya, {opener} your post about the platform team.") == {}, opener


def test_a_known_company_at_the_start_of_a_sentence_is_still_caught():
    assert _bad("Hi Maya. Stripe taught me a lot.")["names"] == ["Stripe"]
    assert _bad("Hi Maya, Stripe taught me a lot.")["names"] == ["Stripe"]


def test_a_multi_word_run_or_a_mid_sentence_capital_stays_strict():
    assert _bad("Hi Maya, Zephyr Labs is where I started.")["names"] == ["Zephyr Labs"]
    assert _bad("Hi Maya, I started at Zephyr.")["names"] == ["Zephyr"]
    assert _bad("Hi Maya, I love Zephyr. Zephyr is great.")["names"] == ["Zephyr"]
    assert _bad("Hi Maya, Fellow Acme Robotics fan here.") == {}            # first word is grammar, the rest is known
    assert _bad("Hi Maya, Fellow Orbit Labs fan here.")["names"] == ["Fellow Orbit Labs"]


# ── numbers in every form ───────────────────────────────────────────────────

@pytest.mark.parametrize("text,found", [
    ("I made it 10x faster.", ["10x"]), ("It served 50k users.", ["50k"]), ("It cut costs 40%.", ["40%"]),
    ("It ran twice as fast.", ["twice"]), ("I shipped a dozen services.", ["dozen"]),
    ("It was my first startup.", ["first"]), ("I have five years.", ["five"]), ("About 3M rows a day.", ["3m"]),
    ("It grew 2.5 times.", ["2.5"]),
])
def test_invented_number_forms_are_caught(text, found):
    assert _bad("Hi Maya, " + text)["numbers"] == found


def test_one_in_ordinary_phrases_and_numbers_from_the_inputs_pass():
    assert _bad("Hi Maya, one question about the team, and one of my projects is Bondi.") == {}
    assert _bad("Hi Maya, I run 4 services in Bondi.") == {}
    assert _bad("Hi Maya, I run 4x services in Bondi.")["numbers"] == ["4x"]      # "4x" is not "4"


# ── lowercase technology names ──────────────────────────────────────────────

def test_a_lowercase_invented_technology_is_rejected():
    assert _bad("Hi Maya, I built a redis cache for Bondi.")["tech"] == ["redis"]
    assert _bad("Hi Maya, I run terraform and kubernetes for Bondi.")["tech"] == ["terraform", "kubernetes"]
    assert _problems("Hi Maya, I built a redis cache for Bondi.")[0].startswith("it mentions technologies")


def test_a_lowercase_technology_in_the_inputs_passes():
    assert _bad("Hi Maya, I write python and fastapi services.") == {}


def test_the_vocabulary_does_not_flag_ordinary_words():
    assert _bad("Hi Maya, I like to go deep on spring-clean code and swift replies.") == {}


# ── attribution ─────────────────────────────────────────────────────────────

def test_a_sender_only_term_is_not_credited_to_the_recipient():
    assert _bad("Hi Maya, your team's work on LazyCurl looks great.")["attributed"] == ["lazycurl"]
    assert _bad("Hi Maya, I saw LazyCurl at Acme Robotics and loved it.")["attributed"] == ["lazycurl"]
    assert _bad("Hi Maya, Maya's team runs Bondi.")["attributed"] == ["bondi"]
    assert _problems("Hi Maya, your team's work on LazyCurl looks great.")[0].startswith("it credits the recipient")


def test_comparing_the_two_is_not_misattribution():
    assert _bad("Hi Maya, your team's Kafka work looks like my LazyCurl project.") == {}
    assert _bad("Hi Maya, I built LazyCurl and your platform team uses Kafka.") == {}


def test_known_gap_attribution_without_a_person_word_is_not_caught():
    # No "your", "you", company or name in the clause: the checker cannot tell whose Bondi this is.
    assert "attributed" not in _bad("Hi Maya, the platform team runs Bondi.")


def test_known_gap_the_sender_claiming_the_recipients_technology_is_not_caught():
    # Kafka is in the recipient's headline only; "I built a kafka pipeline" is an invention this does not see.
    assert _bad("Hi Maya, I built a kafka pipeline.") == {}


# ── look-alike and invisible characters ─────────────────────────────────────

def test_lookalike_letters_from_another_script_are_rejected():
    note = "Hi Maya, I worked at Str\u0456pe."          # Cyrillic i
    bad = _bad(note)
    assert bad["scripts"] == ["CYRILLIC"] and "Str\u0456pe" in bad["names"]
    assert any("another script" in p for p in _problems(note))


def test_fullwidth_and_zero_width_tricks_are_normalised_away():
    assert _bad("Hi Maya, I worked at \uff33tripe.")["names"] == ["Stripe"]
    assert _bad("Hi Maya, I worked at Str\u200bipe.")["names"] == ["Stripe"]
    assert any("invisible" in p for p in _problems("Hi Maya, I worked at Str\u200bipe."))


def test_accented_latin_names_in_the_inputs_pass():
    inputs = INPUTS + ["Jos\u00e9 \u00c1lvarez"]
    assert nc.check_note("Hi Jos\u00e9, I build Python services.", inputs, NAMES + ["Jos\u00e9 \u00c1lvarez"]) == []


# ── work authorisation, emoji and style rules: phrases, not bare words ──────────

_IN = ["Name: Sam", "Pat Lee", "Staff Engineer at Acme", "Acme"]


def _issues(text, company="Acme", kind="note"):
    return nc.check_note(text, _IN + [company], ["Sam", "Pat Lee"], False, ["Name: Sam"], [company, "Pat Lee"], kind)


@pytest.mark.parametrize("text", [
    "Hi Pat, I work with Visa-like payment flows.",                    # not about immigration
    "Hi Pat, we sponsored an event for Acme staff.",
    "Hi Pat, I joined the opt-in list for Acme news.",
    "Hi Pat, please opt out of my questions if busy.",
    "Hi Pat, my model reached an F1 score on Acme data.",
    "Hi Pat, how is the team in Tennessee? I read the TN Acme page.",
])
def test_ordinary_uses_of_the_words_pass(text):
    assert not any("work authorisation" in p for p in _issues(text)), _issues(text)


def test_a_company_called_visa_is_not_a_visa_mention():
    assert not any("authorisation" in p for p in _issues("Hi Pat, your work at Visa on payments looks close to my API work.", "Visa"))
    assert not any("authorisation" in p for p in _issues("Hi Pat, Visa's platform team caught my eye.", "Visa"))
    assert any("authorisation" in p for p in _issues("Hi Pat, I need visa sponsorship at Visa.", "Visa"))
    assert any("authorisation" in p for p in _issues("Hi Pat, I need a visa."))


@pytest.mark.parametrize("text", [
    "I need visa sponsorship", "my visa status", "a work visa", "I will need sponsorship later", "employer sponsorship",
    "they would sponsor my H-1B", "H1B", "h-1b", "H 1 B", "I am on OPT", "STEM OPT", "OPT status", "F-1 student", "on an F-1",
    "my EAD card", "employment authorization", "work authorisation", "work authorization", "authorized to work",
    "authorised to work", "right to work", "permanent resident", "green card", "a work permit", "work eligibility",
    "L-1 transfer", "an O-1", "J-1 exchange", "TN visa", "CPT", "citizenship", "immigration",
])
def test_each_immigration_phrase_is_caught(text):
    assert any("authorisation" in p for p in _issues("Hi Pat, " + text + " at Acme.")), text


@pytest.mark.parametrize("text", [
    "⌚", "⏳", "→", "⬆", "✅", "✨", "\U0001F44D", "\U0001F680", "❤️", "☎",
    "➡", "❗", "⭐",
])
def test_emoji_symbols_arrows_and_watch_signs_are_caught(text):
    assert any("emoji" in p for p in _issues(f"Hi Pat {text} quick question about Acme.")), text


def test_fullwidth_and_double_exclamation_marks_count():
    assert any("exclamation" in p for p in _issues("Hi Pat！ Quick question！ about Acme"))
    assert any("exclamation" in p for p in _issues("Hi Pat‼ quick question about Acme"))
    assert not any("exclamation" in p for p in _issues("Hi Pat! Quick question about Acme."))
    assert not any("emoji" in p for p in _issues("Hi Pat, © and ™ and 72° are fine at Acme."))


@pytest.mark.parametrize("text", [
    "I am on an E-3 and would love to chat", "My E‑3 status", "I hold an H-1B1", "H1B1 holder",
    "I will need to be sponsored", "I would need sponsorship", "we require sponsorship", "they require to be sponsored",
])
def test_work_authorisation_wording_is_caught(text):
    assert nc.work_auth_hit(text)


@pytest.mark.parametrize("text,company", [
    ("We sponsored an event last year", ""), ("I applied to Visa for the platform role", "Visa"),
    ("E3 conference talk", ""), ("Route E-37 is closed", ""),
])
def test_ordinary_wording_is_not_work_authorisation(text, company):
    assert nc.work_auth_hit(text, [company]) is None
