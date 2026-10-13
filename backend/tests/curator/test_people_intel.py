"""Parsing search hits into people, and email-format inference."""
import pytest

from backend.curator.intel import email_format_from_snippets, make_email, pattern_from_local
from backend.curator.linkedin_people import parse_card
from backend.curator.people import classify, guess_email, parse_linkedin_hit, priority


@pytest.mark.parametrize("local,pat", [
    ("jdoe", "flast"), ("jane.doe", "first.last"), ("john", "first"), ("JohnSmith", "firstlast"),
    ("j.doe", "f.last"), ("doej", "lastf"), ("jane_doe", "first_last"), ("xyz", None),
])
def test_pattern_from_local(local, pat):
    assert pattern_from_local(local) == pat


@pytest.mark.parametrize("pat,email", [
    ("flast", "dkatz@ramp.com"), ("first.last", "daniel.katz@ramp.com"), ("first", "daniel@ramp.com"),
    ("firstlast", "danielkatz@ramp.com"), ("lastf", "katzd@ramp.com"), ("f.last", "d.katz@ramp.com"),
])
def test_make_email(pat, email):
    assert make_email("Daniel", "Katz", pat, "ramp.com") == email


def test_make_email_needs_last_name_when_pattern_uses_it():
    assert make_email("Cher", "", "first.last", "x.com") is None
    assert make_email("Cher", "", "first", "x.com") == "cher@x.com"


def test_email_format_from_snippets_prefers_com_and_skips_vendor_domains():
    results = [
        {"title": "Ramp Email Format - RocketReach", "snippet": "most common is [first].[last] (ex. jane.doe@ramp.se)"},
        {"title": "Ramp | LeadIQ", "snippet": "email support@leadiq.com. Ramp uses FLast@ramp.com (ex. JDoe@ramp.com)"},
    ]
    assert email_format_from_snippets(results, "Ramp") == ("ramp.com", "flast")
    assert email_format_from_snippets([{"title": "", "snippet": "jane.doe@other.com"}], "Ramp") == (None, None)


def test_guess_email_uses_intel():
    assert guess_email("Daniel Katz", {"email_pattern": "flast", "domain": "ramp.com"}) == ("dkatz@ramp.com", 0.6)
    assert guess_email("Daniel Katz", {}) == (None, None)


def test_parse_linkedin_hit():
    hit = parse_linkedin_hit({"url": "https://www.linkedin.com/in/dkatz?trk=x",
                              "title": "Daniel Katz - Engineering Manager at Ramp | LinkedIn", "snippet": ""}, "Ramp")
    assert hit == {"name": "Daniel Katz", "headline": "Engineering Manager at Ramp", "url": "https://www.linkedin.com/in/dkatz"}
    assert parse_linkedin_hit({"url": "https://ramp.com", "title": "Ramp", "snippet": ""}, "Ramp") is None
    assert parse_linkedin_hit({"url": "https://www.linkedin.com/in/x", "title": "Jo Q - EM at Brex | LinkedIn",
                               "snippet": ""}, "Ramp") is None


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


def test_priority_founders_matter_at_small_companies():
    assert priority("founder", True) > priority("recruiter", True)
    assert priority("founder", False) < priority("recruiter", False)
    assert priority("hiring_manager", False) == 3


def test_parse_card():
    assert parse_card("Jane Doe\n• 2nd\nSenior Technical Recruiter at Ramp\nNew York\nConnect") == \
        ("Jane Doe", "Senior Technical Recruiter at Ramp")
    assert parse_card("") == ("", "")
