"""Parsing search hits into people, and email-format inference."""
import pytest

from backend.curator.intel import email_format_from_snippets, make_email, pattern_from_local
from backend.curator.linkedin_people import card_details, parse_card
from backend.curator.people import (classify, guess_email, job_areas, names_company, parse_linkedin_hit,
                                    priority, profile_country, same_company)


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
    assert hit == {"name": "Daniel Katz", "headline": "Engineering Manager at Ramp", "url": "https://www.linkedin.com/in/dkatz",
                   "location": None, "country": None, "current": True}
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


def _hit(url, title, snippet, company="Ramp"):
    return parse_linkedin_hit({"url": url, "title": title, "snippet": snippet}, company)


def test_hit_reads_location_and_current_employer():
    h = _hit("https://www.linkedin.com/in/zw", "Zach Williams - Lead Technical Recruiter at Ramp | LinkedIn",
             "Lead Technical Recruiter at Ramp · Experience: Ramp · Education: Kean University · "
             "Location: New York City Metropolitan Area · 500+ connections on LinkedIn.")
    assert (h["location"], h["country"], h["current"]) == ("New York City Metropolitan Area", "US", True)
    assert _hit("https://www.linkedin.com/in/dk", "Daniel Katz - Engineering Manager at Ramp | LinkedIn",
                "Engineering Manager at Ramp · Experience: Ramp · Location: 20005.")["country"] == "US"


def test_hit_flags_people_who_left_or_work_elsewhere():
    # headline is stale, Experience shows the current employer
    assert _hit("https://www.linkedin.com/in/a", "Ann Lee - Engineering Manager at Ramp | LinkedIn",
                "Engineering Manager at Ramp · Experience: Brex · Location: San Francisco")["current"] is False
    assert _hit("https://www.linkedin.com/in/b", "Bo Chan - EM at Brex, ex-Ramp | LinkedIn", "") is None
    assert _hit("https://www.linkedin.com/in/c", "Cy Dee - Head of Engineering @ Ramp Network | LinkedIn",
                "Experience: Ramp Network · Location: Warsaw")["current"] is False
    # only the snippet mentions the company
    assert _hit("https://www.linkedin.com/in/d", "Di Eve - Engineering Manager at Plaid | LinkedIn",
                "Previously built card products at Ramp.")["current"] is False


def test_hit_country_from_location_or_subdomain():
    assert _hit("https://ch.linkedin.com/in/x", "Xi Yu - Engineering Manager at Infosys | LinkedIn",
                "Engineering Manager at Infosys", "Infosys")["country"] == "CH"
    assert _hit("https://hr.linkedin.com/in/y", "Al M - Engineering Manager at Infosys | LinkedIn",
                "Experience: Infosys · Location: Karlovac", "Infosys")["country"] == "HR"
    assert profile_country("https://uk.linkedin.com/in/z", None) == "GB"
    assert profile_country("https://in.linkedin.com/in/z", "San Francisco Bay Area") == "US"   # moved
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
