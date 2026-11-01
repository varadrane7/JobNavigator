"""Hiring Cafe extension capture: result parsing and POST /api/jobs/hiring-cafe-import."""
import pytest

from backend.scraper.sources import hiring_cafe as hc


def _result(i="r1", **v5):
    data = {"company_name": "Acme", "formatted_workplace_location": "New York, NY", "workplace_type": "Hybrid",
            "yearly_min_compensation": 140000, "yearly_max_compensation": 180000,
            "listed_compensation_currency": "USD", "estimated_publish_date": "2026-10-30T12:00:00Z",
            "seniority_level": "Mid Level", "visa_sponsorship": True, "min_industry_and_role_yoe": 2}
    data.update(v5)
    return {"id": i, "apply_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
            "job_information": {"title": "Software Engineer", "description": "<p>Build <b>payments</b>.</p>"},
            "v5_processed_job_data": data}


def test_parse_result_reads_hiring_cafes_fields():
    j = hc.parse_result(_result())
    assert (j["title"], j["company"], j["location"]) == ("Software Engineer", "Acme", "New York, NY")
    assert j["url"] == j["apply_url"] == "https://boards.greenhouse.io/acme/jobs/r1"
    assert (j["salary_min"], j["salary_max"]) == (140000, 180000)
    assert j["arrangement"] == "Hybrid" and j["posted"].isoformat() == "2026-10-30T12:00:00+00:00"
    assert j["description"].startswith("Seniority: Mid Level\n")
    assert "Visa sponsorship: True" in j["description"] and "Experience: 2+ years" in j["description"]
    assert j["description"].endswith("Build payments.")


def test_parse_result_fallbacks():
    raw = _result("r2", workplace_type="Remote", formatted_workplace_location="",
                  listed_compensation_currency="EUR")
    raw.pop("apply_url")
    j = hc.parse_result(raw)
    assert j["url"] == "https://hiring.cafe/viewjob/r2" and j["apply_url"] is None
    assert j["location"] == "Remote" and j["salary_min"] is None
    assert hc.parse_result({"id": "x", "job_information": {"title": "No company"}}) is None
    assert hc.parse_result("junk") is None


def test_parse_batch_dedups_by_id():
    assert [j["hc_id"] for j in hc.parse_batch([_result("a"), _result("a"), _result("b"), {}])] == ["a", "b"]


def _hc_search(db, **kw):
    from backend.models.db import Search
    s = Search(name="Hiring Cafe", search_mode="hiring_cafe", active=True, auto_scoring_depth="off", **kw)
    db.add(s)
    db.commit()
    return s


@pytest.fixture
def no_h1b(monkeypatch):
    from backend.scraper.sources import startup_feeds as feeds

    async def none(*a, **k):
        return None
    monkeypatch.setattr(feeds, "prefetch_h1b", none)


def test_import_stores_jobs_under_the_hiring_cafe_search(api_client, test_db, no_h1b):
    from backend.models.db import Job
    s = _hc_search(test_db, title_exclude_keywords=["staff"])
    staff = _result("r3")
    staff["job_information"]["title"] = "Staff Engineer"
    r = api_client.post("/api/jobs/hiring-cafe-import", json={"jobs": [_result("r1"), staff, _result("r1")]})
    assert r.status_code == 200, r.text
    assert r.json() == {"received": 3, "parsed": 2, "jobs_found": 1, "new_jobs": 1}
    job = test_db.query(Job).filter(Job.source == "hiring_cafe").one()
    assert job.search_id == s.id and job.salary_source == "posting" and job.arr_hybrid is True

    again = api_client.post("/api/jobs/hiring-cafe-import", json={"jobs": [_result("r1")]})
    assert again.json()["new_jobs"] == 0


def test_import_refuses_bad_bodies_and_paused_search(api_client, test_db, no_h1b):
    assert api_client.post("/api/jobs/hiring-cafe-import", json={"jobs": "nope"}).status_code == 422
    assert api_client.post("/api/jobs/hiring-cafe-import", json={"jobs": []}).status_code == 409   # no search yet
    s = _hc_search(test_db)
    s.active = False
    test_db.commit()
    r = api_client.post("/api/jobs/hiring-cafe-import", json={"jobs": [_result()]})
    assert r.status_code == 200 and r.json()["new_jobs"] == 0
