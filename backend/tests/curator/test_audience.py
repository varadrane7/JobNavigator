"""Who a person is to the sender (curator/audience.py): read from evidence only, never guessed."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.curator import audience as aud
from backend.curator import invites
from backend.models.db import Contact
from backend.tz import local_today

PERSONA = SimpleNamespace(
    contact={"first_name": "Sam", "last_name": "Rivera", "current_company": "Currentco"},
    resume_content={
        "education": [{"school": "New Jersey Institute of Technology", "degree": "MS, Computer Science"},
                      {"school": "University of Mumbai", "degree": "BE, Electronics"}],
        "experience": [{"title": "Developer", "company": "Currentco", "date": "Feb 2026 - Present"},
                       {"title": "TA", "company": "New Jersey Institute of Technology", "date": "Sep 2025 - Dec 2025"},
                       {"title": "Engineer", "company": "Kotak Securities", "date": "Jun 2022 - Aug 2024"}]},
    networking={"affinities": ["New Jersey Institute of Technology", "University of Mumbai", "Kotak Securities",
                               "Stanford University", "Initech"]})
AFF = aud.affiliations(PERSONA)


def test_affiliations_come_from_the_profile_only():
    assert AFF.schools == ["New Jersey Institute of Technology", "University of Mumbai"]
    assert AFF.employers == ["Kotak Securities"]          # not the current employer, not a school; affinities add nothing
    empty = aud.affiliations(None)
    assert empty.schools == [] and empty.employers == []
    assert aud.detect("Engineer at Acme", "Kotak Securities NJIT", empty) == "engineer"


@pytest.mark.parametrize("headline,card,expected", [
    ("Engineering Manager at Acme", "Past: Engineer at Kotak Securities", "ex_colleague"),
    ("Software Engineer at Acme | NJIT '21", "", "alum"),
    ("Recruiter at Acme", "MS, New Jersey Institute of Technology", "alum"),
    ("Backend Engineer at Acme", "Mumbai University", "alum"),
    ("Backend Engineer at Acme", "University of Mumbai", "alum"),
    ("Backend Engineer at Acme and Kotak Securities", "", "ex_colleague"),
    ("Technical Recruiter at Acme", "", "recruiter"),
    ("Senior Software Engineer at Acme", "", "engineer"),
    ("Engineering Manager at Acme", "", "hiring_manager"),
    ("Co-founder and CTO at Acme", "", "hiring_manager"),
    # no guessing: near misses and absence of evidence
    ("Engineer at Acme", "Kotak Mahindra Bank", "engineer"),
    ("Engineer at Acme", "University of Michigan", "engineer"),
    ("Engineer at Acme", "Mumbai", "engineer"),
    ("Engineer at Acme", "New Jersey Transit", "engineer"),
    ("Vice President", "", None),
    ("", "", None),
])
def test_audience_from_card_text(headline, card, expected):
    assert aud.detect(headline, card, AFF) == expected


def test_a_post_lead_is_a_post_unless_it_is_a_decision_maker_or_shares_a_school():
    assert aud.detect("Head of Talent at Acme", "", AFF, from_post=True) == "post"
    assert aud.detect("Engineering Manager at Acme", "", AFF, from_post=True) == "hiring_manager"
    assert aud.detect("Engineering Manager at Acme", "NJIT", AFF, from_post=True) == "alum"


def test_scout_never_reads_a_posts_text_for_schools():
    from backend.curator.scout import with_audience
    c = {"headline": "Head of Talent at Acme", "lead_kind": "hiring_post", "evidence": "We hire NJIT grads at Kotak Securities"}
    assert with_audience(c, AFF)["audience"] == "post"
    c = {"headline": "Head of Talent at Acme", "lead_kind": "alumni", "evidence": "Experience: Kotak Securities"}
    assert with_audience(c, AFF)["audience"] == "ex_colleague"


def _lead(db, name, audience=None, score=None, due=None, found=None):
    c = Contact(name=name, company="Acme", linkedin_url=name, status="new", audience=audience, score=score,
                next_action_at=due or local_today(), found_on=found or local_today())
    db.add(c)
    db.commit()
    return c


def test_send_today_and_carry_over_order_by_audience_then_score_then_age(test_db):
    from backend.accounts.settings import put_setting
    put_setting(test_db, "invites_daily_cap", "2")
    test_db.commit()
    day = local_today()
    _lead(test_db, "high-score-no-audience", None, 99)
    _lead(test_db, "recruiter", "recruiter", 50)
    _lead(test_db, "alum-low", "alum", 10)
    _lead(test_db, "alum-high", "alum", 80)
    _lead(test_db, "alum-high-older", "alum", 80, due=day - timedelta(days=3))
    _lead(test_db, "colleague", "ex_colleague", 5)
    plan = invites.plan(test_db)
    names = lambda ids: [test_db.get(Contact, i).name for i in ids]
    assert names(plan["send"]) == ["alum-high-older", "alum-high"]            # same audience and score: older first
    assert names(plan["carried"]) == ["alum-low", "colleague", "recruiter", "high-score-no-audience"]


def test_audience_is_in_the_api_and_the_tools(api_client, test_db):
    from backend.connector import tools
    c = _lead(test_db, "A", "alum", 5)
    row = api_client.get("/api/contacts").json()[0]
    assert row["audience"] == "alum"
    assert tools.list_leads(test_db, {})["leads"][0]["audience"] == "alum"
    assert tools.list_leads(test_db, {"audience": "recruiter"})["total"] == 0
    assert tools.get_lead(test_db, {"lead_id": str(c.id)})["audience"] == "alum"
    out = tools.add_lead(test_db, {"name": "Z", "audience": "post"})
    assert out["audience"] == "post"
    with pytest.raises(tools.ToolError):
        tools.add_lead(test_db, {"name": "Y", "audience": "friend"})


def _persona(schools=(), employers=(), affinities=()):
    return SimpleNamespace(
        contact={}, networking={"affinities": list(affinities)},
        resume_content={"education": [{"school": x, "degree": "BS"} for x in schools],
                        "experience": [{"title": "Engineer", "company": x, "date": "2020 - 2022"} for x in employers]})


def test_affiliations_and_the_sender_block_have_one_source():
    from backend.curator import drafting
    block = drafting.build_sender(PERSONA).facts
    for name in AFF.schools + AFF.employers:
        assert name in block                                  # nothing is matched that the model was never given
    assert "Stanford" not in block and "Initech" not in block
    aff = aud.affiliations(PERSONA)
    assert aud.detect("Engineer at Acme", "Stanford University", aff) == "engineer"
    assert aud.detect("Engineer at Acme", "Past: Engineer at Initech", aff) == "engineer"


GIT = aud.affiliations(_persona(["Georgia Institute of Technology", "Birla Institute of Technology and Science"]))


@pytest.mark.parametrize("headline,card,expected", [
    ("Engineer at Acme, working with git and bits and bytes", "", "engineer"),
    ("Engineer at Acme", "GIT hooks, SIT tests", "engineer"),
    ("Engineer at Acme", "git", "engineer"),
    ("Engineer at Acme | BITS Pilani '19", "", "alum"),
    ("Engineer at Acme", "Georgia Institute of Technology", "alum"),
    ("Engineer at Acme", "iit", "engineer"),
])
def test_initials_never_match_ordinary_words(headline, card, expected):
    assert aud.detect(headline, card, GIT) == expected


def test_short_initials_are_not_used_on_their_own():
    aff = aud.affiliations(_persona(["Indian Institute of Technology"]))
    assert aud.detect("Engineer at Acme", "IIT", aff) == "engineer"
    assert aud.detect("Engineer at Acme", "Indian Institute of Technology", aff) == "alum"


KOTAK_SEC = aud.affiliations(_persona(employers=["Kotak Securities"]))
KOTAK_MAH = aud.affiliations(_persona(employers=["Kotak Mahindra Bank"]))
TATA = aud.affiliations(_persona(employers=["Tata Technologies"]))


@pytest.mark.parametrize("aff,card,expected", [
    (KOTAK_SEC, "Past: Analyst at Kotak Securities Ltd.", "ex_colleague"),
    (KOTAK_SEC, "Kotak Mahindra Bank", "engineer"),
    (KOTAK_SEC, "Kotak", "engineer"),
    (KOTAK_MAH, "Kotak Securities", "engineer"),
    (KOTAK_MAH, "Kotak Mahindra Bank, Inc.", "ex_colleague"),
    (TATA, "Tata Consultancy Services", "engineer"),
    (TATA, "Tata Motors", "engineer"),
    (TATA, "Tata Technologies Limited", "ex_colleague"),
])
def test_employers_match_on_the_full_name(aff, card, expected):
    assert aud.detect("Software Engineer at Acme", card, aff) == expected


@pytest.mark.parametrize("headline,card,expected", [
    ("Campus Recruiter at New Jersey Institute of Technology", "", "recruiter"),
    ("Professor at NJIT", "", None),
    ("Engineer at Acme", "Current: Teaching Assistant at New Jersey Institute of Technology", "engineer"),
    ("Engineer at Acme", "Current: Admissions Staff, NJIT", "engineer"),
    ("Engineer at Acme", "Past: Teaching Assistant at New Jersey Institute of Technology", "alum"),
    ("Engineer at Acme", "Education: MS Computer Science, NJIT", "alum"),
    ("Engineer at Acme | NJIT '21", "", "alum"),
    ("Software Engineer at NJIT", "", "engineer"),
    ("Engineer at Acme", "Experience: New Jersey Institute of Technology", "engineer"),
    ("Engineer at Acme", "Location: Newark, New Jersey Institute of Technology", "engineer"),
])
def test_a_school_that_is_their_employer_is_not_alum_evidence(headline, card, expected):
    assert aud.detect(headline, card, AFF) == expected


def test_initials_and_context_windows_use_the_same_folded_text():
    # characters that fold away (accents, symbols) used to shift the initials offset against the staff window
    aff = aud.affiliations(_persona(["Birla Institute of Technology and Science"]))
    plain = "Campus recruiter, BITS"
    odd = "Café ★★★★★★★★★★ Étudianté Campus recruiter, BITS"
    assert aud.detect("Engineer at Acme", plain, aff) == "engineer"        # staff, not a student
    assert aud.detect("Engineer at Acme", odd, aff) == "engineer"
    assert aud.detect("Engineer at Acme", "Café ★★★★★★ BITS Pilani", aff) == "alum"
