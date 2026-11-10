"""Staffing boards read from the JSON their pages load; YC's work-authorization line is not a citizenship rule."""
from backend.curator import scoring
from backend.scraper.ats import staffing_boards as sb


def test_only_the_four_boards_are_claimed():
    assert sb.is_staffing_board("https://insightglobal.com/jobs/search/all/python?page=1")
    assert sb.is_staffing_board("https://www.kforce.com/find-work/search-jobs/#/?t=python")
    assert sb.is_staffing_board("https://jobs.yoh.com/#/jobs")
    assert sb.is_staffing_board("https://www.usgrpinc.com/open-jobs.php")
    assert not sb.is_staffing_board("https://evil.example/insightglobal.com/jobs")
    assert not sb.is_staffing_board("https://kforce.com.evil.example/jobs")


def test_insight_global_row():
    rows = sb._insight({"jobs": [{"requisitionId": "abc", "jobTitle": "Software Engineer", "workRemote": "True",
                                  "workAddress": {"locality": "Newark", "administrativeArea": "NJ"}}]})
    assert rows == [{"title": "Software Engineer", "url": "https://insightglobal.com/jobs/abc",
                     "location": "Newark, NJ", "arrangement": "remote"}]


def test_kforce_row_links_the_job_page_not_the_apply_form():
    rows = sb._kforce({"value": [{"Title": "Software Engineer", "City": "Tampa", "State": "FL", "Responsibilities": "<p>Build</p>",
                                  "Skills": "Python", "ApplyUrl": "http://www.kforce.com/Jobs/1~E~2P1~99/ApplyOnline/?event=x"}]})
    assert rows[0]["url"] == "https://www.kforce.com/Jobs/1~E~2P1~99/"
    assert rows[0]["description"] == "Build Python" and rows[0]["location"] == "Tampa, FL"


def test_yoh_and_ceipal_rows():
    yoh = sb._yoh([{"data": {"jobName": "Backend Engineer", "jobURL": "https://jobs.yoh.com/job-details/x-1", "city": "Dallas",
                             "state": "Texas", "fullDescription": "<b>W2</b> only"}}])
    assert yoh[0]["description"] == "W2 only" and yoh[0]["location"] == "Dallas, Texas"
    ceipal = sb._ceipal({"results": [{"public_job_title": "Data Engineer", "apply_job_login": "https://c.test/1", "state": "NJ",
                                      "country": "United States", "remote_opportunities": "1", "public_job_desc": "Text"}]})
    assert ceipal[0]["arrangement"] == "remote" and ceipal[0]["location"] == "NJ, United States"


def test_insight_paging_keeps_the_search_and_sets_the_page():
    url = sb._page_url("https://insightglobal.com/jobs/search/all/python?page=1&size=10", 3)
    assert "/search/all/python?" in url and "page=3" in url and "size=10" in url


def test_yc_work_authorization_line_is_not_a_blocker():
    assert scoring.jd_blocker("Salary: $150K Visa: US citizen/visa only Experience: 1+ years") is None
    assert scoring.jd_blocker("Visa: US citizen / visa only") is None


def test_real_citizenship_rules_still_block():
    assert scoring.jd_blocker("This role is for US citizens only.") == "us citizens"
    assert scoring.jd_blocker("Must be a US citizen.")
    assert scoring.jd_blocker("We can't sponsor visas.")
