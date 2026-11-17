"""Reading people from LinkedIn cards, and email guessing from a stored format."""
import pytest

from backend.curator.intel import make_email
from backend.curator.linkedin_people import card_details, parse_card
from backend.curator.people import classify, guess_email, job_areas, names_company, priority, profile_country, same_company


@pytest.mark.parametrize("pat,email", [
    ("flast", "dkatz@ramp.com"), ("first.last", "daniel.katz@ramp.com"), ("first", "daniel@ramp.com"),
    ("firstlast", "danielkatz@ramp.com"), ("lastf", "katzd@ramp.com"), ("f.last", "d.katz@ramp.com"),
])
def test_make_email(pat, email):
    assert make_email("Daniel", "Katz", pat, "ramp.com") == email


def test_make_email_needs_last_name_when_pattern_uses_it():
    assert make_email("Cher", "", "first.last", "x.com") is None
    assert make_email("Cher", "", "first", "x.com") == "cher@x.com"


def test_guess_email_uses_intel():
    assert guess_email("Daniel Katz", {"email_pattern": "flast", "domain": "ramp.com"}) == ("dkatz@ramp.com", 0.6)
    assert guess_email("Daniel Katz", {}) == (None, None)


@pytest.mark.parametrize("headline,kind", [
    ("Co-Founder & CEO at Acme", "founder"),
    ("Engineering Manager, Payments", "hiring_manager"),
    ("Head of Engineering @ Acme", "hiring_manager"),
    ("Senior Technical Recruiter", "recruiter"),
    ("Talent Acquisition Partner", "recruiter"),
    ("Software Engineer", "engineer"),
    ("Account Executive", None),
])
def test_classify(headline, kind):
    assert classify(headline) == kind


def test_priority_orders_hiring_managers_then_recruiters_then_founders():
    assert priority("hiring_manager") == 3 > priority("recruiter") > priority("founder") > priority("engineer")


def test_parse_card():
    assert parse_card("Jane Doe\n• 2nd\nSenior Technical Recruiter at Ramp\nNew York\nConnect") == \
        ("Jane Doe", "Senior Technical Recruiter at Ramp")
    assert parse_card("") == ("", "")


def test_profile_country_from_location_or_subdomain():
    assert profile_country("https://uk.linkedin.com/in/z", None) == "GB"
    assert profile_country("https://ch.linkedin.com/in/x", None) == "CH"
    assert profile_country("https://in.linkedin.com/in/z", "San Francisco Bay Area") == "US"   # moved
    assert profile_country("https://www.linkedin.com/in/z", "20005") == "US"
    assert profile_country("https://www.linkedin.com/in/z", None) is None


@pytest.mark.parametrize("a,b,same", [
    ("Stripe, Inc.", "Stripe", True), ("Ramp Network", "Ramp", False), ("Meta Platforms", "Meta", True),
    ("Scale AI", "Scale AI", True), ("Scale AI", "Scale", False),
])
def test_same_company(a, b, same):
    assert same_company(a, b) is same


def test_names_company():
    assert names_company("Recruiter | Ramp", "Ramp")
    assert names_company("EM at Ramp, ex-Google", "Ramp")
    assert not names_company("Formerly at Ramp, now Brex", "Ramp")
    assert not names_company("Rampart Security", "Ramp")


def test_job_areas():
    assert job_areas("Machine Learning Engineer, Ads") == ["ml"]
    assert "backend" in job_areas("Software Engineer, Backend")
    assert job_areas("Software Engineer") == []


def test_card_details():
    d = card_details("Jane Doe\n• 2nd\nTechnical Recruiter\nNew York, NY\nCurrent: Technical Recruiter at Ramp\nConnect",
                     "Technical Recruiter")
    assert d == {"location": "New York, NY", "country": "US", "current_text": "Technical Recruiter at Ramp", "past_text": None}
    assert card_details("Jo\nEM at Brex\nPast: Engineer at Ramp", "EM at Brex")["past_text"] == "Engineer at Ramp"


def test_card_details_reads_the_untagged_current_position_line():
    """Late 2026 cards state a current job as "<title> at <company> since <when>", no "Current:" tag."""
    card = ("Guy Alster\n• 2nd\nEngineering Leader | Distributed Systems\nRedmond, Washington, United States\nConnect\n"
            "Engineering Manager at Stripe since March 2023, building a credential platform.")
    d = card_details(card, "Engineering Leader | Distributed Systems")
    assert d["current_text"] == "Engineering Manager at Stripe" and d["country"] == "US"
    from backend.curator.people import names_company
    assert names_company(d["current_text"], "Stripe")
    # a headline that happens to say "since" is not a position line
    assert card_details("Jo\nBuilding things since 2010\nConnect", "Building things since 2010")["current_text"] is None
