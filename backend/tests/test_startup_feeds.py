"""SimplifyJobs and HN "Who is hiring?" parsing and filters (scraper/sources/startup_feeds.py)."""
import time

from backend.scraper.sources import startup_feeds as F

NOW = 1_800_000_000


def _simplify(**kw):
    base = {"active": True, "is_visible": True, "category": "Software", "sponsorship": "Other",
            "company_name": "Acme", "title": "Software Engineer New Grad", "url": "https://jobs.lever.co/acme/1",
            "locations": ["New York, NY"], "date_posted": NOW - 3600}
    return {**base, **kw}


def test_simplify_keeps_recent_software_roles_that_may_sponsor():
    raw = [_simplify(), _simplify(sponsorship="U.S. Citizenship is Required"),
           _simplify(sponsorship="Does Not Offer Sponsorship"), _simplify(category="Hardware"),
           _simplify(active=False), _simplify(date_posted=NOW - 5 * 86400)]
    out = F.parse_simplify(raw, hours=48, now=NOW)
    assert len(out) == 1 and out[0]["company"] == "Acme" and out[0]["location"] == "New York, NY"


def test_place_and_terms_filters():
    assert F.place_ok("New York, NY", "United States") and F.place_ok("Remote", "United States")
    assert not F.place_ok("London, UK", "United States")
    assert F.place_ok("London, UK", "London, Berlin") and not F.place_ok("Paris, France", "London, Berlin")
    assert F.place_ok("anywhere", "") and F.terms_ok("Backend Engineer", "backend, platform")
    assert not F.terms_ok("Data Analyst", "backend, platform")


def test_hn_comment_parses_company_role_place():
    c = {"id": 42, "author": "founder", "created_at": "2026-10-01T16:00:00Z",
         "text": "Tiny AI (YC W25) | Backend Engineer | New York, NY | Onsite | $150k<p>We build things.</p>"}
    j = F.parse_hn_comment(c)
    assert j["company"] == "Tiny AI" and j["title"] == "Backend Engineer" and j["location"] == "New York, NY"
    assert j["url"].endswith("id=42") and "We build things" in j["description"]
    assert F.parse_hn_comment({**c, "text": "Great thread, thanks!"}) is None
    assert F.parse_hn_comment({**c, "text": "Acme | Product Manager | Remote"}) is None   # not an engineering role


def test_yc_qualifies_and_slugs():
    from backend.scraper.yc_import import qualifies, slugs
    c = {"status": "Active", "isHiring": True, "team_size": 40, "regions": ["United States of America"],
         "tags": ["Developer Tools"], "name": "Acme Labs", "slug": "acme-labs", "website": "https://acmelabs.dev"}
    assert qualifies(c)
    assert not qualifies({**c, "team_size": 4}) and not qualifies({**c, "regions": ["Europe"]})
    assert not qualifies({**c, "tags": ["Consumer"]}) and not qualifies({**c, "isHiring": False})
    assert slugs(c) == ["acme-labs", "acmelabs"]
