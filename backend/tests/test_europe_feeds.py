"""Arbeitnow API and Berlin Startup Jobs RSS parsing (sources/europe_feeds.py)."""
import time
from types import SimpleNamespace

from backend.scraper.sources import europe_feeds as E

NOW = time.time()


def _an(i, hours_ago, **kw):
    x = {"slug": f"s{i}", "company_name": f"Co{i}", "title": "Backend Engineer (m/f/d)",
         "description": "<p>Python &amp; AWS. English is our working language.</p>", "remote": False,
         "url": f"https://www.arbeitnow.com/jobs/companies/co{i}/backend-{i}", "tags": [], "job_types": [],
         "location": "Dresden", "created_at": int(NOW - hours_ago * 3600)}
    x.update(kw)
    return x


def test_arbeitnow_parse_stops_at_the_window_and_names_the_country():
    jobs, older = E.parse_arbeitnow([_an(1, 1), _an(2, 2, location="Berlin, Berlin, Germany", remote="True"),
                                     _an(3, 50)], since=NOW - 24 * 3600)
    assert older is True and [j["company"] for j in jobs] == ["Co1", "Co2"]
    assert jobs[0]["location"] == "Dresden, Germany"
    assert jobs[1]["location"] == "Berlin, Berlin, Germany (Remote)"
    assert jobs[0]["description"] == "Python & AWS. English is our working language."
    assert jobs[0]["posted"].timestamp() > NOW - 2 * 3600


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><title>Senior Backend Engineer (f/m/d) // Acme GmbH</title>
<link>https://berlinstartupjobs.com/engineering/senior-backend-engineer-acme/</link>
<pubDate>{recent}</pubDate><content:encoded><![CDATA[<p>Go and Kubernetes.</p>]]></content:encoded></item>
<item><title>Old Role // Gone GmbH</title><link>https://berlinstartupjobs.com/engineering/old/</link>
<pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_bsj_parse_splits_role_and_company():
    from email.utils import formatdate
    jobs = E.parse_bsj(RSS.format(recent=formatdate(NOW - 3600, usegmt=True)), since=NOW - 24 * 3600)
    assert len(jobs) == 1
    j = jobs[0]
    assert (j["title"], j["company"], j["location"]) == ("Senior Backend Engineer (f/m/d)", "Acme GmbH", "Berlin, Germany")
    assert j["description"] == "Go and Kubernetes."


def test_arbeitnow_keeps_a_place_that_already_names_its_country():
    jobs, _ = E.parse_arbeitnow([_an(1, 1, location="London"), _an(2, 1, location="Paris")], since=NOW - 24 * 3600)
    assert [j["location"] for j in jobs] == ["London", "Paris"]


def test_terms_match_the_title_not_the_description():
    items = [{"title": "Account Executive", "description": "Sell our Python cloud platform", "location": "Berlin, Germany"},
             {"title": "Python Developer", "description": "APIs", "location": "Berlin, Germany"}]
    s = SimpleNamespace(search_term="python, cloud", location="")
    assert [j["title"] for j in E._filter(items, s)] == ["Python Developer"]


def test_relocation_lane_keeps_only_its_countries(monkeypatch):
    import backend.accounts.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda db, key, default=None: '["DE", "NL", "IE", "AE"]')
    items = [{"title": "a", "location": "Berlin, Germany"}, {"title": "b", "location": "London"},
             {"title": "c", "location": "Amsterdam"}]
    assert [j["title"] for j in E._in_lane(items)] == ["a", "c"]


def test_filter_by_term_and_place():
    items = [{"title": "Backend Engineer", "description": "Python", "location": "Munich, Germany"},
             {"title": "Sales Manager", "description": "B2B", "location": "Berlin, Germany"}]
    s = SimpleNamespace(search_term="backend, platform", location="munich, hamburg")
    assert [j["title"] for j in E._filter(items, s)] == ["Backend Engineer"]
    assert len(E._filter(items, SimpleNamespace(search_term="", location=""))) == 2
