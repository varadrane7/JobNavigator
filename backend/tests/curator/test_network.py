"""Decision-maker ("people to know") outreach: who is searched for, at which companies, what the
drafts are given, and the morning Telegram line."""
from datetime import timedelta

import pytest

from backend.models.db import Company, Contact, Persona, Resume, Setting

RESUME = {
    "summary": "Backend engineer, 3 years across trading infrastructure and applied LLM systems.",
    "experience": [{"title": "Software Developer", "company": "FieldWorker", "bullets": ["x"]},
                   {"title": "Software Engineer II", "company": "Kotak Securities"},
                   {"title": "Software Engineer", "company": "Kotak Securities"}],
    "skills": {"Languages": "Python, Go, C#", "Cloud": "AWS, Terraform"},
    "projects": [{"name": "LazyCurl", "description": "Go, Bubble Tea", "url": "https://blog.example.com/lazycurl",
                  "bullets": ["Terminal UI for HTTP APIs with a load-testing engine."]},
                 {"name": "PrivGuard Sentinel", "description": "Python, LLM APIs", "url": "",
                  "bullets": ["LLM pull-request reviewer."]}],
    "education": [{"school": "NJIT", "degree": "MS, Computer Science"}],
}


def test_sender_brief_carries_every_project_and_link():
    from backend.curator.run import sender_brief
    b = sender_brief(RESUME)
    assert "Summary: Backend engineer" in b
    assert "Experience: Software Developer at FieldWorker; Software Engineer II at Kotak Securities; Software Engineer at Kotak Securities" in b
    assert "- LazyCurl (Go, Bubble Tea): Terminal UI for HTTP APIs with a load-testing engine. [https://blog.example.com/lazycurl]" in b
    assert "- PrivGuard Sentinel (Python, LLM APIs): LLM pull-request reviewer." in b
    assert "Skills: Languages: Python, Go, C#; Cloud: AWS, Terraform" in b


def test_me_reads_the_default_resume(test_db):
    r = Resume(name="General", is_base=True, json_data=RESUME)
    test_db.add(r)
    test_db.add(Persona(id=1, contact={"first_name": "Varad", "last_name": "Rane", "city": "Manhattan", "state": "New York"}))
    test_db.flush()
    test_db.add(Setting(key="default_resume_id", value=str(r.id)))
    test_db.commit()
    from backend.curator.run import _me
    name, brief, contact = _me()
    assert name == "Varad Rane" and "LazyCurl" in brief and contact["city"] == "Manhattan"


class _CardLI:
    """A LinkedIn session whose keyword search returns fixed cards."""
    wanted_ids: set = set()

    def __init__(self, cards):
        self.cards, self.queries = cards, []

    async def search(self, kw, country=None, network=None, **k):
        self.queries.append(kw)
        return self.cards


def _card(name, headline, current):
    return {"url": f"https://www.linkedin.com/in/{name.lower().replace(' ', '')}", "name": name, "headline": headline,
            "current_text": current, "location": "New York, NY", "country": "US", "source": "linkedin_main", "degree": 2}


async def test_decision_makers_skip_recruiters_and_engineers():
    import backend.curator.people as people
    li = _CardLI([_card("Rita Recruiter", "Technical Recruiter at Ramp", "Technical Recruiter at Ramp"),
                  _card("Ed Engineer", "Software Engineer at Ramp", "Software Engineer at Ramp"),
                  _card("Maya Manager", "Engineering Manager, Platform at Ramp", "Engineering Manager at Ramp")])
    out = await people.find_decision_makers("Ramp", ["backend", "infra"], li, want=2)
    assert [p["name"] for p in out] == ["Maya Manager"] and out[0]["kind"] == "hiring_manager"
    assert li.queries == ["Ramp engineering manager infrastructure"]      # the first query already found a decision maker


async def test_people_search_without_a_linkedin_session_finds_nobody_and_never_touches_the_web():
    import backend.curator.people as people
    assert await people.find_people("Ramp", "Backend Engineer", None, want=2) == []
    assert await people.find_decision_makers("Ramp", ["backend"], None, want=1) == []


async def test_draft_network_never_claims_an_application(monkeypatch):
    import backend.curator.people as people
    from backend.curator.drafting import Sender
    seen = []

    async def fake_ask(prompt, system, purpose, max_tokens=1500, job_id=None):
        seen.append((purpose, prompt))
        return "n" * 400 if purpose == "curator_network_draft" else "Subject: Backend at Ramp\n\nHi Maya"
    monkeypatch.setattr("backend.curator.llm.ask", fake_ask)
    d = await people.draft_network({"name": "Maya Manager", "headline": "Engineering Manager"}, "Ramp",
                                   Sender(name="Varad Rane", facts="Name: Varad Rane\nProjects:\n- LazyCurl"))
    # a 400-character note is never cut: one retry, then no note and a flag for a hand-written one
    assert d["note"] is None and d["note_manual"] is True and "400 characters" in d["note_issue"]
    assert d["email_draft"] == "Subject: Backend at Ramp\n\nHi Maya"
    prompt = seen[0][1]
    assert "LazyCurl" in prompt and "applied" not in prompt.replace("He has applied", "")
    assert seen[0][0] == "curator_network_draft"


def test_network_companies_order_and_cooldown(test_db):
    from backend.curator.run import network_companies
    from backend.tz import local_today
    test_db.add_all([Company(name="Zeta", active=True, tier=2), Company(name="Alpha", active=True, tier=1),
                     Company(name="Asleep", active=False, tier=1), Company(name="Cooled", active=True, tier=1)])
    test_db.add(Contact(company="Cooled", name="X", linkedin_url="u1", found_on=local_today() - timedelta(days=10)))
    test_db.commit()
    ranked = [{"id": 1, "company": "TopCo"}, {"id": 2, "company": "Kforce"}, {"id": 3, "company": "NearMiss"},
              {"id": 4, "company": "alpha"}]
    out = network_companies(ranked, [ranked[0]], {"company_exclude": ["Kforce"]}, limit=10)
    assert out == ["Alpha", "Zeta", "NearMiss"]
    assert network_companies(ranked, [ranked[0]], {"company_exclude": []}, limit=2) == ["Alpha", "Zeta"]


def test_summary_message_is_counts_only():
    from backend.curator.run import summary_message
    top = [{"company": "Glean", "title": "a"}, {"company": "glean", "title": "b"}, {"company": "Ramp", "title": "c"}]
    msg = summary_message(top, [{}] * 4, [{}] * 2)
    assert "Curated list generated" in msg
    assert "3 jobs to apply · 2 unique companies · 6 people to connect (4 job-linked, 2 network)" in msg
    assert msg.rstrip().endswith("Good luck today!") and "http" not in msg
