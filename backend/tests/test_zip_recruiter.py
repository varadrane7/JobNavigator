"""ZipRecruiter through JobSpy: its own calls (one per plain term, with descriptions) and the 403 retry."""
import sys
import types

import pandas as pd
import pytest


def _row(site, title, url, company="Acme"):
    return {"site": site, "title": title, "company": company, "job_url": url, "description": None}


@pytest.fixture
def fake_jobspy(monkeypatch):
    """A stand-in jobspy whose scrape_jobs answers from `answers(kwargs)` and logs each call."""
    import jobspy.model  # noqa: F401  (keep the real submodules importable)
    calls = []
    state = {"answer": lambda kw: pd.DataFrame()}

    def scrape_jobs(**kwargs):
        calls.append(kwargs)
        rows, errors = state["answer"](kwargs)
        df = pd.DataFrame(rows)
        df.attrs = {"errors": errors, "warnings": {}}
        return df

    mod = types.ModuleType("jobspy")
    mod.scrape_jobs = scrape_jobs
    mod.model = sys.modules["jobspy.model"]
    monkeypatch.setitem(sys.modules, "jobspy", mod)
    return calls, state


def test_zip_terms_splits_boolean_queries():
    from backend.scraper.sources.jobspy import zip_terms
    assert zip_terms('"software engineer" OR "backend engineer" OR "full stack"') == \
        ["software engineer", "backend engineer", "full stack"]
    assert zip_terms("C# developer") == ["C# developer"]
    assert zip_terms('("new grad" OR "New Grad" OR entry level)') == ["new grad", "entry level"]
    assert zip_terms("") == [""]


def test_scrape_boards_untouched_without_zip(fake_jobspy):
    from backend.scraper.sources.jobspy import scrape_boards
    calls, state = fake_jobspy
    state["answer"] = lambda kw: ([_row("indeed", "SWE", "https://i/1")], {})
    df = scrape_boards({"site_name": ["indeed"], "search_term": '"a" OR "b"'})
    assert len(calls) == 1 and calls[0]["search_term"] == '"a" OR "b"'
    assert len(df) == 1


def test_scrape_boards_asks_zip_once_per_term(fake_jobspy):
    from backend.scraper.sources.jobspy import scrape_boards
    calls, state = fake_jobspy

    def answer(kw):
        if kw["site_name"] == ["zip_recruiter"]:
            # the same job comes back for both terms: kept once
            return [_row("zip_recruiter", kw["search_term"], f"https://zip/{kw['search_term'][0]}"),
                    _row("zip_recruiter", "Shared", "https://zip/shared")], {}
        return [_row("indeed", "SWE", "https://i/1")], {}
    state["answer"] = answer

    df = scrape_boards({"site_name": ["indeed", "zip_recruiter"],
                        "search_term": '"software engineer" OR "backend engineer"'})
    assert [c["site_name"] for c in calls] == [["indeed"], ["zip_recruiter"], ["zip_recruiter"]]
    assert calls[0]["search_term"] == '"software engineer" OR "backend engineer"'
    assert [c["search_term"] for c in calls[1:]] == ["software engineer", "backend engineer"]
    # only ZipRecruiter's own calls fetch descriptions (LinkedIn's per-job fetch stays off)
    assert "fetch_description" not in calls[0]
    assert all(c["fetch_description"] is True for c in calls[1:])
    assert sorted(df["job_url"]) == ["https://i/1", "https://zip/b", "https://zip/s", "https://zip/shared"]
    assert df.attrs["errors"] == {}


def test_scrape_boards_single_term_still_splits_zip_off(fake_jobspy):
    from backend.scraper.sources.jobspy import scrape_boards
    calls, state = fake_jobspy
    state["answer"] = lambda kw: ([_row(kw["site_name"][0], "SWE", f"https://{kw['site_name'][0]}/1")], {})
    df = scrape_boards({"site_name": ["linkedin", "zip_recruiter"], "search_term": "software engineer"})
    assert [c["site_name"] for c in calls] == [["linkedin"], ["zip_recruiter"]]
    assert sorted(df["site"]) == ["linkedin", "zip_recruiter"]


def test_scrape_boards_retries_a_blocked_zip(fake_jobspy):
    from backend.scraper.sources.jobspy import scrape_boards
    calls, state = fake_jobspy
    tries = {"n": 0}

    def answer(kw):
        if kw["site_name"] == ["zip_recruiter"]:
            tries["n"] += 1
            if tries["n"] == 1:
                return [], {"zip_recruiter": "response status code 403 (blocked: use a proxy)"}
            return [_row("zip_recruiter", "SWE", "https://zip/1")], {}
        return [], {}
    state["answer"] = answer

    df = scrape_boards({"site_name": ["zip_recruiter"], "search_term": "software engineer"}, pause=0)
    assert len(calls) == 2
    assert list(df["job_url"]) == ["https://zip/1"]
    assert "zip_recruiter" not in df.attrs["errors"]


def test_scrape_boards_keeps_the_error_when_zip_stays_blocked(fake_jobspy):
    from backend.scraper.sources.jobspy import scrape_boards
    calls, state = fake_jobspy
    state["answer"] = lambda kw: ([], {"zip_recruiter": "response status code 403"})
    df = scrape_boards({"site_name": ["zip_recruiter"], "search_term": "swe"}, pause=0)
    assert len(calls) == 2
    assert df.empty
    assert "403" in df.attrs["errors"]["zip_recruiter"]
