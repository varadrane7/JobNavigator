"""Faceted LinkedIn search, the id table, company-first allocation and the company_people cache.
No network: LinkedIn is a fake object, the budget and the cache run on the SQLite test database."""
import json
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from backend.curator import audience as aud
from backend.curator import company_first as cf
from backend.curator import entity_ids, li_budget
from backend.curator import linkedin_people as lp
from backend.models.db import CompanyPeople, Contact, LiEntityId, Persona, Setting
from backend.tz import local_today

NJIT, MUMBAI, KOTAK, STRIPE = "New Jersey Institute of Technology", "University of Mumbai", "Kotak Securities", "Stripe"


@pytest.fixture
def budget(test_db, monkeypatch):
    settings = {}
    monkeypatch.setattr(li_budget, "_jar", lambda which: f"/jars/{which}.json")
    monkeypatch.setattr(li_budget, "_setting_int", lambda key, default: int(settings.get(key, default)))
    monkeypatch.setattr(li_budget, "_setting_str", lambda key, default: str(settings.get(key, default)))
    li_budget.reset_all()
    yield settings
    li_budget.reset_all()


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    import types

    async def fake_sleep(s):
        return None
    monkeypatch.setattr(lp, "asyncio", types.SimpleNamespace(sleep=fake_sleep))


AFF = aud.Affiliations([NJIT, MUMBAI], [KOTAK])


# ── facet URLs ───────────────────────────────────────────────────────────────

def _q(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_people_url_matches_the_verified_forms():
    url = lp.people_url(current_company="2135371", network=["F", "S"], country="US")
    assert url == ("https://www.linkedin.com/search/results/people/?origin=FACETED_SEARCH"
                   "&currentCompany=%5B%222135371%22%5D&network=%5B%22F%22%2C%22S%22%5D&geoUrn=%5B%22103644278%22%5D")
    q = _q(lp.people_url(current_company="2135371", schools=["165198", "15093732"], past_company=["110647982"], title="recruiter"))
    assert json.loads(q["schoolFilter"]) == ["165198", "15093732"] and json.loads(q["pastCompany"]) == ["110647982"]
    assert q["title"] == '"recruiter"' and "titleFreeText" not in q
    assert "keywords" not in q and _q(lp.people_url("recruiter", current_company="1"))["keywords"] == "recruiter"
    assert "title=%22recruiter%22" in lp.people_url(title="recruiter")


def test_posts_url_with_author_company():
    q = _q(lp.posts_url("hiring", author_company="2135371"))
    assert json.loads(q["authorCompany"]) == ["2135371"] and q["keywords"] == "hiring"
    assert q["datePosted"] == '"past-week"' and q["sortBy"] == '"date_posted"' and q["origin"] == "FACETED_SEARCH"
    assert "authorCompany" not in _q(lp.posts_url("hiring"))


def test_facets_read_back_and_a_stripped_param_is_a_dropped_facet():
    sent = lp.people_url(current_company="2135371", title="recruiter", country="US", network=["F", "S"])
    requested = lp.facets_of(sent)
    assert requested["currentCompany"] == ["2135371"] and requested["title"] == "recruiter"
    assert lp.dropped_facets(sent, requested) == []
    # LinkedIn reorders params and drops what it ignores: here the title is gone
    final = "https://www.linkedin.com/search/results/people/?currentCompany=%5B%222135371%22%5D&geoUrn=%5B%22103644278%22%5D&network=%5B%22F%22%2C%22S%22%5D"
    assert lp.dropped_facets(final, requested) == ["title"]
    # a changed value counts as dropped too
    other = sent.replace("2135371", "999")
    assert lp.dropped_facets(other, requested) == ["currentCompany"]


def test_pills_are_checked_and_an_unreadable_pill_bar_does_not_decide():
    req = {"currentCompany": ["1"], "schoolFilter": ["2", "3"], "network": ["F", "S"], "title": "recruiter"}
    pills = ["Stripe", "2 Schools", "1st", "2nd", "3rd+", "All filters"]
    hints = {"currentCompany": "Stripe, Inc."}
    assert lp.pill_missing(pills, req, hints) == []
    assert lp.pill_missing(["Stripe", "1st"], req, hints) == ["schoolFilter"]
    assert lp.pill_missing(["All filters"], {"currentCompany": ["1"]}, hints) == ["currentCompany"]
    assert lp.pill_missing(["Kotak Neo"], {"pastCompany": ["5"]}, {"pastCompany": "Kotak Securities"}) == []
    assert lp.pill_missing([], req, hints) == []          # no pills read: the URL check stands alone


class FakePage:
    def __init__(self, url, pills, cards=()):
        self.url, self.pills, self.cards = url, pills, list(cards)

    async def evaluate(self, js):
        return self.pills if js is lp._PILLS_JS else self.cards if js is lp._RESULTS_JS else []


def _card(name, headline):
    return {"url": f"https://www.linkedin.com/in/{name.split()[0].lower()}", "text": f"{name}\n• 2nd\n{headline}\nSeattle, Washington, United States\nConnect"}


async def _faceted(monkeypatch, final_url_of, pills, **facets):
    li = lp.LinkedInPeople()
    li.account = "main"
    opened = []

    async def fake_open(url, main_only=False):
        opened.append(url)
        li.page = FakePage(final_url_of(url), pills, [_card("Dana Roe", "Technical Recruiter at Stripe")])
        return True
    monkeypatch.setattr(li, "_open", fake_open)
    return li, opened


async def test_a_dropped_facet_is_logged_marks_the_query_unusable_and_costs_nothing_more(budget, monkeypatch):
    # LinkedIn kept the company but stripped the title: the page looks fine, the URL says otherwise
    li, opened = await _faceted(monkeypatch, lambda u: u.replace("&title=%22recruiter%22", ""), ["Stripe"])
    out = await li.search("", "US", ["F", "S"], current_company="2135371", title="recruiter", qtype="role", names={"currentCompany": "Stripe"})
    assert out == [] and len(opened) == 1
    assert li_budget.unusable("role") and any("ignored the title" in n for n in li.notes)
    # the next query of that type is not even opened
    assert await li.search("", "US", ["F", "S"], current_company="2135371", title="recruiter", qtype="role") == []
    assert len(opened) == 1
    assert not li_budget.unusable("alumni")                           # other query types are unaffected
    li_budget.forget_process_state()
    assert li_budget.unusable("role")                                 # a restart does not forget it today


async def test_a_missing_pill_alone_never_drops_a_facet(budget, monkeypatch):
    # the URL kept the school facet; the pill bar shows no school pill (stale or odd markup): not dropped
    li, _ = await _faceted(monkeypatch, lambda u: u, ["Stripe", "1st", "2nd"])
    out = await li.search("", "US", ["F", "S"], current_company="1", schools=["2"], qtype="alumni", names={"currentCompany": "Stripe"})
    assert [p["name"] for p in out] == ["Dana Roe"] and not li_budget.unusable("alumni")
    # card headlines that do not name the company (50-60% of real cards) change nothing either
    async def open2(url, main_only=False):
        li.page = FakePage(url, ["Stripe", "2 Schools", "2nd"], [_card("Dana Roe", "Data Engineer | Python | SQL")])
        return True
    monkeypatch.setattr(li, "_open", open2)
    out = await li.search("", "US", ["F", "S"], current_company="1", schools=["2"], qtype="alumni", names={"currentCompany": "Stripe"})
    assert [p["name"] for p in out] == ["Dana Roe"] and out[0]["degree"] == 2 and li.bad_pages == 0


async def test_a_stale_page_is_read_again_before_a_facet_is_called_dropped(budget, monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(lp.asyncio, "sleep", fake_sleep)
    li = lp.LinkedInPeople()
    li.account = "main"
    sent = lp.people_url(current_company="2135371", title="recruiter", country="US", network=["F", "S"])
    stale = "https://www.linkedin.com/search/results/people/?keywords=previous"
    pages = [stale, sent]                                    # the first read is the previous page, the second is right

    class Page(FakePage):
        @property
        def url(self):
            return pages.pop(0) if len(pages) > 1 else pages[0]

        @url.setter
        def url(self, v):
            pass
    li.page = Page(sent, ["Stripe"], [_card("Dana Roe", "Technical Recruiter at Stripe")])
    assert await li._facets_dropped(sent, "role", {"currentCompany": "Stripe"}) is False
    assert sleeps and not li_budget.unusable("role")


async def test_card_buttons_are_not_filter_pills(budget, monkeypatch):
    """No filter bar found (the JS returns null/[]): Connect and Message buttons never stand in for pills, and the
    URL alone decides: a kept URL is a kept facet, a stripped one is dropped."""
    async def fake_sleep(s):
        return None
    monkeypatch.setattr(lp.asyncio, "sleep", fake_sleep)
    li = lp.LinkedInPeople()
    li.account = "main"
    sent = lp.people_url(current_company="1", schools=["2"], country="US")
    li.page = FakePage(sent, None, [])                      # evaluate(_PILLS_JS) -> None: no bar
    assert await li._facets_dropped(sent, "alumni", {"currentCompany": "Stripe"}) is False
    li.page = FakePage(sent.replace("&schoolFilter=%5B%222%22%5D", ""), None, [])
    assert await li._facets_dropped(sent, "alumni", {}) is True and li_budget.unusable("alumni")
    assert "main button" not in lp._PILLS_JS and "filters-bar" in lp._PILLS_JS


def test_list_facets_compare_as_sets():
    sent = lp.people_url(current_company="1", schools=["165198", "15093732"], network=["F", "S"])
    final = ("https://www.linkedin.com/search/results/people/?currentCompany=%5B%221%22%5D&network=%5B%22S%22%2C%22F%22%5D"
             "&schoolFilter=%5B%2215093732%22%2C%22165198%22%5D")
    assert lp.dropped_facets(final, lp.facets_of(sent)) == []


async def test_posts_by_author_company_use_the_same_check(budget, monkeypatch):
    li = lp.LinkedInPeople()
    li.account = "main"

    async def fake_open(url, main_only=False):
        assert main_only and "authorCompany" in url
        li.page = FakePage(url.replace("authorCompany", "x"), ["All filters"])
        return True
    monkeypatch.setattr(li, "_open", fake_open)
    assert await li.search_posts("hiring", author_company="2135371", qtype="posts_company", name="Stripe") == []
    assert li_budget.unusable("posts_company")


# ── ids ──────────────────────────────────────────────────────────────────────

def test_ids_are_seeded_idempotently_from_the_persona_and_never_overwritten(test_db):
    assert entity_ids.seed(AFF) == 4                      # Stripe, 2 schools, Kotak
    assert entity_ids.seed(AFF) == 0
    assert entity_ids.lookup("school", NJIT) == "165198" and entity_ids.lookup("school", "University of Mumbai") == "15093732"
    assert entity_ids.lookup("company", "Stripe, Inc.") == "2135371"          # the shared company key
    assert entity_ids.lookup("company", KOTAK) == "110647982"
    assert entity_ids.remember("company", "Stripe", "1") is False and entity_ids.lookup("company", "Stripe") == "2135371"
    # a persona without those schools seeds nothing for them
    test_db.query(LiEntityId).delete()
    test_db.commit()
    assert entity_ids.seed(aud.Affiliations(["Some College"], ["Acme"])) == 1
    assert entity_ids.lookup("school", NJIT) is None and entity_ids.lookup("company", "Acme") is None


def test_ids_are_learned_from_links_on_a_loaded_page(test_db):
    items = [{"href": "https://www.linkedin.com/search/results/people/?currentCompany=%5B%22777%22%5D", "text": "Ramp"},
             {"href": "https://www.linkedin.com/company/888/", "text": "Other Co"},
             {"href": "https://www.linkedin.com/search/results/people/?currentCompany=%5B%221%22%2C%222%22%5D", "text": "Two"},
             {"href": "https://www.linkedin.com/search/results/people/?schoolFilter=%5B%229%22%5D", "text": "Some University"}]
    assert {(k, t, i) for k, t, i in entity_ids.ids_from_links(items)} == {
        ("company", "Ramp", "777"), ("company", "Other Co", "888"), ("school", "Some University", "9")}
    assert entity_ids.learn(items, {"ramp"}) == 1
    assert entity_ids.lookup("company", "Ramp") == "777" and entity_ids.lookup("company", "Other Co") is None


# ── fakes for the company search ─────────────────────────────────────────────

def person(name, headline, degree=2):
    return {"name": name, "headline": headline, "url": f"https://www.linkedin.com/in/{name.lower().replace(' ', '-')}",
            "location": "New York", "country": "US", "degree": degree, "source": "linkedin_main"}


BLOCKED = object()


class FakeLI:
    """Answers facet searches from `script[qtype]`, spends one main-session load per search like LinkedInPeople._open."""
    def __init__(self, script=None, spend=True):
        self.script, self.spend, self.calls, self.loads, self.wanted_ids, self.notes = script or {}, spend, [], 0, set(), []
        self.bad_pages = 0

    async def search(self, keywords="", country=None, network=None, **kw):
        if self.spend and not li_budget.take("main"):
            self.bad_pages += 1
            return []
        self.loads += 1
        self.calls.append({"keywords": keywords, "network": network, **kw})
        q = kw.get("qtype") or "keyword"
        r = self.script.get(q, [])
        if r is BLOCKED:                    # the load was spent, the page was a block / wall
            self.bad_pages += 1
            return []
        if q == "role":
            r = r.get(kw.get("title"), []) if isinstance(r, dict) else r
        return list(r)

    async def search_posts(self, keywords="", *, author_company=None, qtype=None, name=None):
        if self.spend and not li_budget.take("main"):
            self.bad_pages += 1
            return []
        self.loads += 1
        self.calls.append({"keywords": keywords, "author_company": author_company, "qtype": qtype, "name": name})
        r = self.script.get(qtype, [])
        if r is BLOCKED:
            self.bad_pages += 1
            return []
        return list(r)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


@pytest.fixture
def ids(test_db):
    entity_ids.seed(AFF)
    entity_ids.remember("company", "Ramp", "777")
    entity_ids.remember("company", "Brex", "778")


JOB = {"company": "Stripe", "title": "Software Engineer", "loc_country": "US", "id": None}


async def test_company_search_runs_alumni_then_role_then_ex_colleague_and_stops_when_enough(budget, ids):
    li = FakeLI({"alumni": [person("Ann Alum", "Engineer at Stripe"), person("Bo Alum", "Data Engineer | Python")],
                 "role": {"recruiter": [person("Rae Recruiter", "Technical Recruiter at Stripe")]}})
    out = await cf.facet_search(li, "Stripe", want=2, top3=True, aff=AFF)
    assert [c["qtype"] for c in li.calls] == ["alumni"]            # two alumni are enough: no more loads
    call = li.calls[0]
    assert call["current_company"] == "2135371" and call["schools"] == ["165198", "15093732"]
    assert call["network"] == ["F", "S"] and call["keywords"] == "" and "title" not in call
    assert [p["audience"] for p in out] == ["alum", "alum"] and out[0]["current"] is True

    li = FakeLI({"role": {"recruiter": [person("Rae Recruiter", "Talent Acquisition at Stripe")],
                          "software engineer": [person("Eli Eng", "Backend Engineer")]}})
    out = await cf.facet_search(li, "Stripe", want=2, top3=True, aff=AFF)
    assert [c.get("title") for c in li.calls if c["qtype"] == "role"] == ["recruiter", "software engineer"]
    assert {p["kind"] for p in out} == {"recruiter", "engineer"}


async def test_ex_colleague_search_only_for_top_three_and_only_when_nothing_else_was_found(budget, ids):
    li = FakeLI({"ex_colleague": [person("Kay Kotak", "SWE at Stripe | ex-Kotak")]})
    out = await cf.facet_search(li, "Stripe", want=2, top3=True, aff=AFF)
    assert [c["qtype"] for c in li.calls] == ["alumni", "role", "posts_company", "ex_colleague"]   # recruiter found nobody: no engineer query
    assert li.calls[3]["past_company"] == ["110647982"] and out[0]["audience"] == "ex_colleague"
    li = FakeLI({"ex_colleague": [person("Kay Kotak", "SWE")]})
    assert await cf.facet_search(li, "Stripe", want=2, top3=False, aff=AFF) == []
    assert [c["qtype"] for c in li.calls] == ["alumni", "role"]                       # not a top-3 company: 2 loads
    # people who are already connections (1st degree) or already contacts are not "found"
    li = FakeLI({"alumni": [person("Fi First", "x", degree=1), person("Ken Known", "y")]})
    out = await cf.facet_search(li, "Stripe", want=2, top3=False, aff=AFF, known={person("Ken Known", "y")["url"]})
    assert out == []


async def test_unknown_company_id_means_no_facet_search_and_no_page_load(budget, ids):
    li = FakeLI()
    assert await cf.facet_search(li, "Mystery Co", want=2, top3=True, aff=AFF) is None
    assert li.calls == [] and cf.key_of("Mystery Co") in li.wanted_ids          # asked pages may teach the id; nothing is loaded for it


async def test_unknown_company_falls_back_to_the_keyword_search(budget, ids, monkeypatch):
    import backend.curator.company_first as mod
    import backend.curator.people as people

    async def fake_find(company, title, li, want, country, region, city):
        li.loads += 1
        return [{**person("Kim Keyword", "Technical Recruiter at Mystery Co"), "kind": "recruiter"}]
    monkeypatch.setattr(people, "find_people", fake_find)
    li = FakeLI()
    out = await mod.people_for(li, {**JOB, "company": "Mystery Co"}, {"action": "search", "rank": 1}, want=2, aff=AFF)
    assert [p["name"] for p in out] == ["Kim Keyword"]
    assert cf.searched_since(["mystery"], local_today()) and cf.cached_people("mystery", local_today())


# ── selection and skips ──────────────────────────────────────────────────────

def _top(*companies, country="US"):
    return [{"company": c, "title": "SWE", "loc_country": country, "id": None} for c in companies]


def test_selection_dedupes_skips_and_caps(test_db):
    today = local_today()
    cf.remember("brex", [person("Ann", "x")], today - timedelta(days=5))               # searched 5 days ago
    cf.remember("gusto", [], today - timedelta(days=31))                               # searched 31 days ago: eligible again
    test_db.add(Contact(company="Plaid, Inc.", name="Pat", linkedin_url="u1", status="connected"))
    test_db.add(Contact(company="Figma", name="Fay", linkedin_url="u2", status="new", degree=1))
    test_db.add(Contact(company="Notion", name="Nan", linkedin_url="u3", status="requested"))   # not connected yet
    test_db.commit()
    top = _top("Stripe", "Stripe, Inc.", "Brex", "Plaid", "Figma", "Notion", "Gusto", "Ramp", "Anduril", "Zed") + _top("SAP", country="DE")
    p = cf.plan(top, today, day_cap=3)
    assert list(p) == ["stripe", "brex", "plaid", "figma", "notion", "gusto", "ramp", "anduril", "zed"]   # one per company, US only
    act = {k: v["action"] for k, v in p.items()}
    assert act["brex"] == "cache" and act["plaid"] == "connected" and act["figma"] == "connected"
    assert [k for k, a in act.items() if a == "search"] == ["stripe", "notion", "gusto"]        # 3 slots, in score order
    assert act["ramp"] == act["anduril"] == act["zed"] == "skip"
    assert p["stripe"]["rank"] == 1 and p["notion"]["rank"] == 5


def test_slots_are_shared_by_the_days_runs(test_db):
    today = local_today()
    cf.remember("stripe", [], today)
    cf.remember("ramp", [], today)
    p = cf.plan(_top("Brex", "Gusto", "Zed"), today, day_cap=3)
    assert [k for k, v in p.items() if v["action"] == "search"] == ["brex"]


# ── the cache ────────────────────────────────────────────────────────────────

def test_cache_reuse_and_expiry(test_db):
    today = local_today()
    old, fresh = person("Old One", "x"), person("Fresh One", "y")
    cf.remember("stripe", [old], today - timedelta(days=31))
    cf.remember("stripe", [fresh, {**person("Known One", "z"), "audience": "alum"}], today - timedelta(days=29), searched=False)
    got = cf.cached_people("stripe", today, known_urls={person("Known One", "z")["url"]})
    assert [p["name"] for p in got] == ["Fresh One"]                  # 31 days old: gone; a known contact: skipped
    assert cf.cached_people("stripe", today + timedelta(days=2)) == []  # the fresh one expires on day 30
    assert cf.cached_people("stripe", today)[0]["audience"] in (None, "alum")
    assert cf.searched_since(["stripe"], today) == {}                 # the 31-day-old marker no longer counts
    cf.remember("stripe", [], today)                                  # purges rows past the window, marks today
    assert test_db.query(CompanyPeople).filter(CompanyPeople.li_url == old["url"]).count() == 0
    assert cf.searched_since(["stripe"], today) == {"stripe": today}
    cf.remember("stripe", [fresh], today)                             # a person is stored once
    assert test_db.query(CompanyPeople).filter(CompanyPeople.li_url == fresh["url"]).count() == 1


async def test_cached_people_are_reused_across_jobs_and_runs_without_a_search(budget, ids):
    today = local_today()
    li = FakeLI({"alumni": [person("Ann Alum", "Engineer"), person("Bo Alum", "Engineer"), person("Cy Alum", "Engineer")]})
    first = await cf.people_for(li, JOB, {"action": "search", "rank": 1}, want=1, aff=AFF, today=today)
    assert [p["name"] for p in first] == ["Ann Alum"] and li.loads == 1       # the alumni query returned two, one is used
    # the day's second run: Stripe was searched, so it is a "cache" company; Ann is a contact now, Bo is not
    known = {p["url"] for p in first}
    li2 = FakeLI()
    again = await cf.people_for(li2, JOB, {"action": "cache", "rank": 1}, want=2, aff=AFF, known=known, today=today)
    assert [p["name"] for p in again] == ["Bo Alum"] and li2.calls == []
    # and a plan says the company is not searched again
    assert cf.plan(_top("Stripe"), today, day_cap=5)["stripe"]["action"] == "cache"


async def test_no_loads_left_means_no_search_and_no_marker(budget, ids):
    for _ in range(15):
        li_budget.take("main")
    li = FakeLI()
    assert await cf.people_for(li, JOB, {"action": "search", "rank": 1}, want=2, aff=AFF) == []
    assert li.calls == [] and cf.searched_since(["stripe"], local_today()) == {}      # the company keeps its slot


async def test_a_company_whose_facets_were_dropped_is_not_marked_searched(budget, ids):
    li_budget.mark_unusable("alumni")
    li_budget.mark_unusable("role")
    li = FakeLI()
    assert await cf.people_for(li, JOB, {"action": "search", "rank": 1}, want=2, aff=AFF) == []
    assert cf.searched_since(["stripe"], local_today()) == {}


# ── allocation across two runs ───────────────────────────────────────────────

async def test_two_runs_stay_within_their_shares_and_the_daily_total(budget, ids):
    """Main session: 15 loads a day, runs 8 and 5, 2 held back. Seven companies, none with people, so every
    top-3 company costs 3 loads and every other 2."""
    cos = ["Stripe", "Ramp", "Brex", "Zed", "Yan", "Xi", "Wu"]
    for n in cos[3:]:
        entity_ids.remember("company", n, str(900 + len(n) * 7 + ord(n[0])))
    today = local_today()
    spent, searched = [], []
    for ordinal in (1, 2):
        li_budget.enter_run(ordinal, 2)
        li = FakeLI()
        p = cf.plan(_top(*cos), today, day_cap=5)
        used0 = li_budget.used("main")
        for rank, c in enumerate(cos, 1):
            d = p[cf.key_of(c)]
            if d["action"] == "search":
                got = await cf.people_for(li, {**JOB, "company": c}, d, want=2, aff=AFF, today=today)
                assert got == []
        spent.append(li_budget.used("main") - used0)
        searched.append(cf.searched_count(today))
    li_budget.end_run()
    assert spent[0] <= 8 and spent[1] <= 5 and sum(spent) <= 13 <= li_budget.cap("main") - 2
    assert searched[1] <= 5                                         # the day's company cap holds across both runs
    assert li_budget.used("main") == sum(spent)
    assert spent[0] == 8 and searched[0] == 2                       # run 1: 3 + 3 loads, 2 more cut off by the budget: that company stays eligible


def test_the_scout_draws_only_on_the_reserve(budget):
    li_budget.begin_scout(2)
    taken = 0
    while li_budget.take("main"):
        taken += 1
    assert taken == 2 and li_budget.remaining("main") == 0           # 15 - 8 - 5
    li_budget.end_run()
    li_budget.begin_run(2)
    assert li_budget.remaining("main") == 8                         # the runs' shares were not touched
    li_budget.end_run()
    li_budget.begin_scout(1)                                        # one curator run a day: no split, daily total only
    assert li_budget.remaining("main") == 15 - 2
    li_budget.end_run()


# ── find_contacts end to end ─────────────────────────────────────────────────

async def test_find_contacts_uses_the_company_search_for_us_jobs_and_keywords_abroad(budget, ids, test_db, monkeypatch):
    import backend.curator.linkedin_people as lpm
    import backend.curator.people as people
    import backend.curator.run as run
    test_db.add(Setting(key="outreach_drafts_enabled", value="false"))
    test_db.add(Persona(id=1, resume_content={"education": [{"school": NJIT}, {"school": MUMBAI}],
                                              "experience": [{"title": "SWE", "company": KOTAK, "date": "2022 - 2024"}]}))
    test_db.commit()
    li = FakeLI({"alumni": [person("Ann Alum", "Engineer at Stripe")]})
    monkeypatch.setattr(lpm, "LinkedInPeople", lambda: li)
    kw = []

    async def fake_find(company, title, li_, want, country, region, city):
        kw.append((company, country))
        return [{**person("Sam Sap", "Engineering Manager at SAP", None), "kind": "hiring_manager"}]
    monkeypatch.setattr(people, "find_people", fake_find)
    top = [{"id": None, "company": "Stripe", "title": "SWE", "loc_country": "US"},
           {"id": None, "company": "SAP", "title": "SWE", "loc_country": "DE"}]
    saved = await run.find_contacts(top, {}, 4)
    by = {c["name"]: c for c in saved}
    assert by["Ann Alum"]["audience"] == "alum" and by["Ann Alum"]["degree"] == 2           # the facet is the evidence
    assert by["Sam Sap"]["audience"] == "hiring_manager" and by["Sam Sap"]["degree"] is None
    assert kw == [("SAP", "DE")] and [c["qtype"] for c in li.calls] == ["alumni", "role", "posts_company"]      # one alumnus is not enough for 2
    assert test_db.query(Contact).count() == 2


async def test_a_connected_contact_is_surfaced_instead_of_searching(budget, ids, test_db, monkeypatch):
    import backend.curator.linkedin_people as lpm
    import backend.curator.run as run
    test_db.add(Setting(key="outreach_drafts_enabled", value="false"))
    c = Contact(company="Stripe", name="Cal Connected", linkedin_url="https://www.linkedin.com/in/cal", status="connected")
    test_db.add(c)
    test_db.commit()
    li = FakeLI()
    monkeypatch.setattr(lpm, "LinkedInPeople", lambda: li)
    run._SURFACED.clear()
    assert await run.find_contacts([{"id": None, "company": "Stripe", "title": "SWE", "loc_country": "US"}], {}, 2) == []
    assert li.calls == []
    test_db.expire_all()
    assert test_db.get(Contact, c.id).next_action_at == local_today() and run._SURFACED


# ── the scout shares the allocator ───────────────────────────────────────────

def test_scout_queries_put_the_alumni_facet_first_and_need_no_degree_lookup(test_db):
    from backend.curator import scout
    li_people = [("project_fit", "engineering manager LLM"), ("alumni", "NJIT engineering manager")]
    assert scout.li_queries(li_people, aud.Affiliations(["Unknown School"], []))[0] == ("alumni", "NJIT engineering manager", {})
    entity_ids.seed(AFF)
    first = scout.li_queries(li_people, AFF)[0]
    assert first[0] == "alumni" and first[2]["schools"] == ["165198", "15093732"] and first[2]["network"] == ["F", "S"]
    assert not hasattr(scout, "DEGREE_CHECKS")
    # what the scout finds is filed per company for the curator to reuse
    n = cf.remember_scouted([{**person("Sue Scout", "CTO at Tiny AI"), "company": "Tiny AI", "lead_kind": "alumni", "company_verified": True},
                             {**person("Pat Post", "CTO"), "company": "Tiny AI", "lead_kind": "hiring_post"}])
    assert n == 1 and [p["name"] for p in cf.cached_people("tiny ai", local_today())] == ["Sue Scout"]
    assert cf.searched_since(["tiny ai"], local_today()) == {}


# ── a company is "searched" only when every planned step ran on a usable page ─

async def test_a_blocked_role_query_leaves_the_company_eligible(budget, ids):
    li = FakeLI({"alumni": [person("Ann Alum", "Engineer")], "role": BLOCKED})
    out = await cf.people_for(li, JOB, {"action": "search", "rank": 1}, want=3, aff=AFF)
    assert [p["name"] for p in out] == ["Ann Alum"] and [c["qtype"] for c in li.calls] == ["alumni", "role", "posts_company"]
    assert cf.searched_since(["stripe"], local_today()) == {}                       # not marked: next run tries again
    assert cf.cached_people("stripe", local_today())                                # what was found is kept
    li_ok = FakeLI({"alumni": [person("Ann Alum", "Engineer")], "role": {"recruiter": []}})
    await cf.people_for(li_ok, {**JOB, "company": "Brex"}, {"action": "search", "rank": 9}, want=3, aff=AFF)
    assert cf.searched_since(["brex"], local_today())                               # every step on a usable page: marked


async def test_budget_running_out_between_steps_leaves_the_company_eligible(budget, ids):
    budget["linkedin_main_daily_cap"] = 2
    li = FakeLI({"alumni": []})
    # one load for the alumni query, then the role step has none
    real_search = li.search

    async def spend_then_search(*a, **k):
        r = await real_search(*a, **k)
        li_budget.take("main")                              # another caller used the second load
        return r
    li.search = spend_then_search
    await cf.people_for(li, JOB, {"action": "search", "rank": 9}, want=2, aff=AFF)
    assert [c["qtype"] for c in li.calls] == ["alumni"]
    assert cf.searched_since(["stripe"], local_today()) == {}


async def test_a_walled_keyword_search_is_not_marked_either(budget, ids, monkeypatch):
    import backend.curator.people as people

    async def fake_find(company, title, li, want, country, region, city):
        li.loads += 1
        li.bad_pages += 1                                   # "limited visibility"
        return []
    monkeypatch.setattr(people, "find_people", fake_find)
    await cf.people_for(FakeLI(), {**JOB, "company": "Mystery Co"}, {"action": "search", "rank": 1}, want=2, aff=AFF)
    assert cf.searched_since(["mystery"], local_today()) == {}


# ── surfacing connected contacts ─────────────────────────────────────────────

def test_closed_and_exhausted_contacts_are_not_connected_and_planning_writes_nothing(test_db):
    today = local_today()
    test_db.add_all([
        Contact(company="Plaid", name="A", linkedin_url="a", status="connected"),
        Contact(company="Figma", name="B", linkedin_url="b", status="skip", message_stage="closed_no_reply", degree=1),
        Contact(company="Notion", name="C", linkedin_url="c", status="messaged", follow_ups_sent=2),
        Contact(company="Ramp", name="D", linkedin_url="d", status="messaged", follow_ups_sent=1)])
    test_db.commit()
    p = cf.plan(_top("Plaid", "Figma", "Notion", "Ramp"), today, day_cap=5)
    assert {k: v["action"] for k, v in p.items()} == {"plaid": "connected", "figma": "search", "notion": "search", "ramp": "connected"}
    test_db.expire_all()
    assert all(c.next_action_at is None for c in test_db.query(Contact).all())      # plan writes nothing


def test_surfacing_is_capped_at_three_per_run(test_db):
    cs = [Contact(company=f"Co{i}", name=f"N{i}", linkedin_url=f"u{i}", status="connected") for i in range(5)]
    test_db.add_all(cs)
    test_db.commit()
    assert cf.surface_contacts([c.id for c in cs]) == 3
    test_db.expire_all()
    assert sum(c.next_action_at is not None for c in test_db.query(Contact).all()) == 3


async def test_find_contacts_writes_surfaced_dates_only_after_the_search(budget, ids, test_db, monkeypatch):
    import backend.curator.linkedin_people as lpm
    import backend.curator.run as run
    test_db.add(Setting(key="outreach_drafts_enabled", value="false"))
    test_db.add(Contact(company="Stripe", name="Cal", linkedin_url="c", status="connected"))
    test_db.commit()
    seen = []
    li = FakeLI()
    monkeypatch.setattr(lpm, "LinkedInPeople", lambda: li)
    real = cf.plan
    monkeypatch.setattr(cf, "plan", lambda *a, **k: (seen.append([c.next_action_at for c in test_db.query(Contact)]), real(*a, **k))[1])
    await run.find_contacts([{"id": None, "company": "Stripe", "title": "SWE", "loc_country": "US"}], {}, 2)
    assert seen == [[None]]
    test_db.expire_all()
    assert test_db.query(Contact).one().next_action_at == local_today()


# ── cached people from the scout ─────────────────────────────────────────────

def test_cached_people_skip_connections_and_unverified_companies(test_db):
    today = local_today()
    cf.remember("tiny ai", [{**person("Fac Facet", "x"), "verified": True},
                            {**person("Fir First", "x", degree=1), "verified": True},
                            {**person("Hea Headline", "x"), "verified": False}], today, searched=False)
    assert [p["name"] for p in cf.cached_people("tiny ai", today)] == ["Fac Facet"]
    cf.remember_scouted([{**person("Cur Current", "CTO"), "company": "Zed", "lead_kind": "alumni", "company_verified": True},
                         {**person("Par Parsed", "CTO at Zed"), "company": "Zed", "lead_kind": "alumni", "company_verified": False}], today)
    assert [p["name"] for p in cf.cached_people("zed", today)] == ["Cur Current"]


# ── one reserve for the scout and a manual run ───────────────────────────────

def test_scout_and_manual_run_share_one_reserve(budget):
    li_budget.begin_scout(2)
    assert li_budget.take("main") and li_budget.take("main") and not li_budget.take("main")
    li_budget.end_run()
    li_budget.begin_run(2, manual=True)
    assert li_budget.remaining("main") == 0 and not li_budget.take("main")        # the scout already used the 2
    li_budget.end_run()


# ── ids: conflicts ───────────────────────────────────────────────────────────

def test_an_explicit_company_link_corrects_a_harvested_id_but_not_a_verified_one(test_db):
    entity_ids.remember("company", "Ramp", "111")
    page = [{"href": "https://www.linkedin.com/company/222/", "text": "Ramp"},
            {"href": "https://www.linkedin.com/company/999/", "text": "Stripe"}]
    entity_ids.seed(None)
    assert entity_ids.learn(page, {"ramp", "stripe"}) == 1
    assert entity_ids.lookup("company", "Ramp") == "222" and entity_ids.lookup("company", "Stripe") == "2135371"
    # a facet-link id (weaker evidence) only logs
    entity_ids.learn([{"href": "https://x/?currentCompany=%5B%22333%22%5D", "text": "Ramp"}], {"ramp"})
    assert entity_ids.lookup("company", "Ramp") == "222"


# ── hiring posts by company ──────────────────────────────────────────────────

def post(actor, headline, text="We're hiring a backend engineer. DM me.", slug=None):
    slug = slug or actor.lower().replace(" ", "-")
    return {"author_url": f"https://www.linkedin.com/in/{slug}?x=1", "actor": f"{actor}\n{headline}\n2d",
            "text": text, "urn": "u", "source": "linkedin_main"}


async def test_top_three_company_posts_follow_the_alumni_search_and_carry_the_post_as_evidence(budget, ids):
    li = FakeLI({"alumni": [person("Ann Alum", "Engineer at Stripe")],
                 "posts_company": [post("Hana Hire", "Engineering Manager at Stripe"),
                                   post("Pete Poster", "Software Engineer at Stripe", "Excited to say we're hiring!"),
                                   post("Nina Nope", "Designer at Stripe", "Great offsite this week")]})
    out = await cf.facet_search(li, "Stripe", want=5, top3=True, aff=AFF)
    assert [c["qtype"] for c in li.calls][:2] == ["alumni", "role"] or [c["qtype"] for c in li.calls][-1] == "posts_company"
    assert li.calls[-1]["author_company"] == "2135371" and li.calls[-1]["name"] == "Stripe" and li.calls[-1]["keywords"] == "hiring"
    by = {p["name"]: p for p in out}
    assert set(by) == {"Ann Alum", "Hana Hire", "Pete Poster"}                      # a post that is not about hiring is no lead
    assert by["Hana Hire"]["audience"] == "hiring_manager" and by["Pete Poster"]["audience"] == "post"
    assert by["Hana Hire"]["from_post"] is True and "hiring a backend engineer" in by["Hana Hire"]["evidence"]
    assert by["Hana Hire"]["url"] == "https://www.linkedin.com/in/hana-hire"
    assert not by["Ann Alum"].get("from_post")


async def test_posts_are_searched_only_for_top_three_known_ids_and_only_when_more_people_are_wanted(budget, ids):
    script = {"alumni": [person("Ann Alum", "Engineer")], "posts_company": [post("Hana Hire", "EM at Stripe")]}
    li = FakeLI(script)
    await cf.facet_search(li, "Stripe", want=2, top3=False, aff=AFF)                 # not a top-3 company
    assert "posts_company" not in [c["qtype"] for c in li.calls]
    li = FakeLI({**script, "alumni": [person("Ann Alum", "Engineer"), person("Bo Alum", "Engineer")]})
    await cf.facet_search(li, "Stripe", want=2, top3=True, aff=AFF)                  # the alumni search already found enough
    assert [c["qtype"] for c in li.calls] == ["alumni"]
    li = FakeLI(script)
    assert await cf.facet_search(li, "Mystery Co", want=2, top3=True, aff=AFF) is None     # id unknown: nothing loaded
    assert li.calls == []


async def test_post_authors_are_cached_with_their_evidence(budget, ids):
    li = FakeLI({"posts_company": [post("Hana Hire", "Engineering Manager at Stripe")]})
    today = local_today()
    first = await cf.people_for(li, JOB, {"action": "search", "rank": 1}, want=1, aff=AFF, today=today)
    assert first[0]["from_post"] is True and first[0]["audience"] == "hiring_manager"
    again = cf.cached_people("stripe", today)
    assert again[0]["from_post"] is True and "hiring a backend engineer" in again[0]["evidence"] and again[0]["audience"] == "hiring_manager"


async def test_a_dropped_author_company_facet_makes_posts_unusable_and_leaves_the_company_unfinished(budget, ids, monkeypatch):
    li = lp.LinkedInPeople()
    li.account = "main"
    scripted = FakeLI({"alumni": [person("Ann Alum", "Engineer")]})

    async def fake_open(url, main_only=False):
        li.page = FakePage(url.replace("authorCompany", "x"), ["All filters"])
        return True
    monkeypatch.setattr(li, "_open", fake_open)

    class Hybrid:
        """Alumni answers come from the script; the post search is the real one with a page that dropped the facet."""
        wanted_ids, notes = set(), []
        loads, bad_pages = 0, 0

        async def search(self, *a, **k):
            return await scripted.search(*a, **k)

        async def search_posts(self, *a, **k):
            out = await li.search_posts(*a, **k)
            self.bad_pages = li.bad_pages
            return out
    h = Hybrid()
    out = await cf.facet_search(h, "Stripe", want=5, top3=True, aff=AFF)
    assert li_budget.unusable("posts_company") and not out.complete
    assert [p["name"] for p in out] == ["Ann Alum"]
    li2 = FakeLI({"alumni": [person("Ann Alum", "Engineer")], "posts_company": [post("Hana Hire", "EM at Stripe")]})
    await cf.facet_search(li2, "Stripe", want=5, top3=True, aff=AFF)
    assert "posts_company" not in [c["qtype"] for c in li2.calls]                    # unusable for the rest of the day


async def test_posts_search_stays_inside_the_daily_main_budget(budget, ids):
    for _ in range(14):
        li_budget.take("main")
    li = FakeLI({"alumni": [person("Ann Alum", "Engineer")], "posts_company": [post("Hana Hire", "EM at Stripe")]})
    out = await cf.facet_search(li, "Stripe", want=5, top3=True, aff=AFF)
    assert [c["qtype"] for c in li.calls] == ["alumni"] and not out.complete            # the 15th load went to alumni; none left
    assert li_budget.remaining("main") == 0


async def test_the_role_step_comes_before_posts_so_a_tight_share_reaches_it(budget, ids):
    li_budget.take("main")
    for _ in range(12):
        li_budget.take("main")                                   # 2 loads left: alumni and role
    li = FakeLI({"alumni": [], "role": {"recruiter": [person("Rae Recruiter", "Technical Recruiter at Stripe")]},
                 "posts_company": [post("Hana Hire", "EM at Stripe")]})
    out = await cf.facet_search(li, "Stripe", want=5, top3=True, aff=AFF)
    assert [c["qtype"] for c in li.calls] == ["alumni", "role"] and [p["name"] for p in out] == ["Rae Recruiter"]


async def test_a_top_three_company_is_started_only_with_room_for_its_posts_load(budget, ids):
    for _ in range(13):
        li_budget.take("main")                                   # 2 loads left
    li = FakeLI()
    assert await cf.people_for(li, JOB, {"action": "search", "rank": 1}, want=2, aff=AFF) == [] and li.calls == []
    li = FakeLI()
    await cf.people_for(li, {**JOB, "company": "Ramp"}, {"action": "search", "rank": 9}, want=2, aff=AFF)
    assert [c["qtype"] for c in li.calls] == ["alumni", "role"]     # not top 3: two loads are enough
