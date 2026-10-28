"""The lead scout (curator/scout.py) and the persona project sync (curator/projects_sync.py):
who a search result or a post turns into, what is searched for, follow-ups, and a whole run with
LinkedIn, SearXNG and the model replaced."""
from datetime import timedelta

import pytest

from backend.models.db import Contact, Persona, Resume, Setting
from backend.tz import local_today

NET = {"pitch": "Backend engineer who ships LLM systems", "github_user": "someone",
       "target_roles": ["backend engineer"], "affinities": ["NJIT"],
       "projects": [{"name": "LazyCurl", "one_liner": "Terminal UI for HTTP APIs", "stack": ["Go"],
                     "domains": ["devtools"], "url": "https://github.com/x/lazycurl"},
                    {"name": "dotfiles", "hidden": True, "domains": ["homelab"]}],
       "projects_synced_at": "2999-01-01T00:00:00+00:00"}


def test_parse_profile_hit_reads_company_from_experience():
    from backend.curator.scout import parse_profile_hit
    hit = parse_profile_hit({"url": "https://www.linkedin.com/in/jane?trk=x",
                             "title": "Jane Doe - Engineering Manager at Acme | LinkedIn",
                             "snippet": "Engineering Manager · Experience: Acme Corp · Location: New York"})
    assert hit["name"] == "Jane Doe" and hit["company"] == "Acme Corp" and hit["url"].endswith("/in/jane")
    assert parse_profile_hit({"url": "https://example.com/x", "title": "x"}) is None


def test_company_from_headline():
    from backend.curator.scout import company_from_headline
    assert company_from_headline("Founder at Tiny AI | ex-Google") == "Tiny AI"
    assert company_from_headline("CTO @ Ramp") == "Ramp"
    assert company_from_headline("Building things") == ""


def test_parse_post_keeps_only_hiring_posts():
    from backend.curator.scout import parse_post
    post = {"author_url": "https://www.linkedin.com/in/bob?x=1", "actor": "Bob Lee\nCTO at Tiny AI\n2d",
            "text": "We're hiring a backend engineer in NYC. DM me.", "source": "linkedin_mock"}
    lead = parse_post(post)
    assert lead["name"] == "Bob Lee" and lead["company"] == "Tiny AI" and lead["url"].endswith("/in/bob")
    assert parse_post({**post, "text": "Great conference this week"}) is None
    assert parse_post({**post, "author_url": "https://www.linkedin.com/company/tiny"}) is None


def test_plan_queries_uses_roles_domains_and_affinities():
    from backend.curator.scout import plan_queries
    q = plan_queries(NET, {"city": "New York"})
    assert any("backend engineer" in x for x in q["posts"])
    assert any("developer tools" in x and "New York" in x for x in q["posts"])
    assert not any("homelab" in x for x in q["posts"] + q["project_fit"])     # hidden projects stay out
    assert q["affinity"] and '"NJIT"' in q["affinity"][0]


def test_follow_ups_become_due(test_db):
    from backend.curator.scout import mark_follow_ups
    today = local_today()
    old = Contact(name="A", company="X", linkedin_url="u1", status="requested", found_on=today - timedelta(days=9))
    fresh = Contact(name="B", company="X", linkedin_url="u2", status="requested", found_on=today - timedelta(days=2))
    connected = Contact(name="C", company="X", linkedin_url="u3", status="connected", found_on=today - timedelta(days=4))
    test_db.add_all([old, fresh, connected])
    test_db.commit()
    assert mark_follow_ups(test_db, today) == 2
    assert old.next_action_at == today and connected.next_action_at == today and fresh.next_action_at is None


async def test_run_saves_scored_leads_once(test_db, monkeypatch):
    from backend.curator import scout, li_budget
    import backend.curator.linkedin_people as lpm
    test_db.add(Persona(id=1, contact={"first_name": "V", "city": "New York"}, networking=NET,
                        work_auth={"requires_sponsorship_future": True}))
    test_db.add(Resume(name="General", is_base=True, json_data={"summary": "Backend engineer"}))
    test_db.add(Setting(key="scout_daily_leads", value="2"))
    test_db.commit()

    class FakeLI:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def search_posts(self, q):
            return [{"author_url": "https://www.linkedin.com/in/bob", "actor": "Bob Lee\nCTO at Tiny AI",
                     "text": "We're hiring backend engineers", "source": "linkedin_mock"}]

        async def search(self, kw, country=None, network=None):
            return [{"url": "https://www.linkedin.com/in/bob", "degree": 2, "name": "Bob Lee",
                     "headline": "CTO at Tiny AI", "source": "linkedin_mock"}]

    async def fake_search(q, n=10, **kw):
        return [{"url": "https://www.linkedin.com/in/amy", "title": "Amy Wu - Founder at DevCo | LinkedIn",
                 "snippet": "Founder · Experience: DevCo · Location: New York"},
                {"url": "https://www.linkedin.com/in/zed", "title": "Zed - Student | LinkedIn", "snippet": ""}]

    async def fake_ask_json(prompt, system, purpose, max_tokens=1500, job_id=None):
        score = 90 if "Bob Lee" in prompt else 70 if "Amy Wu" in prompt else 10
        return {"score": score, "kind": "founder", "why": "hiring", "talking_points": ["a", "b"],
                "project": "LazyCurl", "note": "Hi"}

    monkeypatch.setattr(lpm, "LinkedInPeople", FakeLI)
    monkeypatch.setattr(scout, "search", fake_search)
    monkeypatch.setattr("backend.curator.llm.ask_json", fake_ask_json)
    monkeypatch.setattr(li_budget, "available", lambda which: True)

    s = await scout.run_network_scout(sync_projects=False)
    assert s["saved"] == 2 and not s["errors"]
    rows = {c.name: c for c in test_db.query(Contact).all()}
    assert set(rows) == {"Bob Lee", "Amy Wu"}
    assert rows["Bob Lee"].lead_kind == "hiring_post" and rows["Bob Lee"].degree == 2 and rows["Bob Lee"].score == 90
    assert rows["Amy Wu"].project_ref == "LazyCurl" and rows["Amy Wu"].next_action_at == local_today()
    # a second run finds the same people and saves nobody twice
    s2 = await scout.run_network_scout(sync_projects=False)
    assert s2["saved"] == 0 and test_db.query(Contact).count() == 2


def test_merge_project_keeps_user_edits_and_hides_dotfiles():
    from backend.curator.projects_sync import merge_project
    repo = {"name": "lazycurl", "html_url": "u", "pushed_at": "2026-01-01", "description": "TUI"}
    edited = {"name": "lazycurl", "one_liner": "mine", "edited": True}
    assert merge_project(edited, repo, {"one_liner": "theirs"}) is edited
    out = merge_project(None, repo, {"one_liner": "A TUI", "stack": ["Go"], "trivial": False})
    assert out["one_liner"] == "A TUI" and out["hidden"] is False and out["source"] == "github"
    assert merge_project(None, {**repo, "name": "dotfiles"}, None)["hidden"] is True


def test_networking_brief_lists_shown_projects():
    from backend.curator.projects_sync import networking_brief
    b = networking_brief(NET)
    assert "Pitch: Backend engineer" in b and "LazyCurl" in b and "dotfiles" not in b


def test_parse_post_reads_the_2026_card_text():
    from backend.curator.scout import parse_post
    post = {"author_url": "https://www.linkedin.com/in/diana", "source": "linkedin_mock",
            "actor": "Diana Romero\n• 3rd+\nTalent Acquisition Lead at Acme AI | SaaS Hiring\nView my services\nOct 21",
            "text": "We're hiring a Python backend engineer in New York."}
    lead = parse_post(post)
    assert lead["name"] == "Diana Romero" and lead["company"] == "Acme AI"
    assert lead["headline"].startswith("Talent Acquisition Lead")
