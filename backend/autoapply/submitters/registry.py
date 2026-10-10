"""Which submitter a job URL goes to. Pure URL logic, so the queue can route jobs before any
submitter implementation exists."""
from urllib.parse import urlparse

from backend.scraper._shared.urls import host_matches

# Job boards and aggregators: they list other companies' jobs, gate applying behind their own login or
# bounce to the real ATS. Jobs whose URL is on one of these never enter the queue; only postings a plain
# Chromium can apply to directly (company ATS / careers pages) do. Extend with autoapply_extra_aggregators.
AGGREGATORS = (
    "linkedin.com", "ziprecruiter.com", "indeed.com", "glassdoor.com", "monster.com", "dice.com",
    "simplyhired.com", "careerbuilder.com", "jobright.ai", "levels.fyi", "builtin.com", "wellfound.com",
    "angel.co", "freehire.me", "hiring.cafe", "remoteok.com", "remoteok.io", "weworkremotely.com",
    "remotive.com", "himalayas.app", "workingnomads.com", "jobgether.com", "simplify.jobs", "otta.com",
    "welcometothejungle.com", "workatastartup.com", "ycombinator.com", "talent.com", "jooble.org",
    "adzuna.com", "google.com", "handshake.com", "joinhandshake.com", "builtinnyc.com", "jobot.com",
    "lensa.com", "snagajob.com", "flexjobs.com", "ladders.com",
    "theladders.com", "hired.com", "upwork.com", "toptal.com", "mercor.com", "turing.com",
)
# Kept for imports elsewhere.
UNSUPPORTED = AGGREGATORS


def classify(url: str | None, extra_aggregators=()) -> str | None:
    """Submitter name for a job URL, or None when it is an aggregator or not a web URL."""
    if not url:
        return None
    try:
        scheme = urlparse(url).scheme
    except Exception:
        return None
    if scheme not in ("http", "https"):
        return None
    if host_matches(url, "greenhouse.io"):
        return "greenhouse"
    if host_matches(url, "jobs.lever.co", "jobs.eu.lever.co"):
        return "lever"
    if host_matches(url, "jobs.ashbyhq.com"):
        return "ashby"
    if host_matches(url, "myworkdayjobs.com", "myworkdaysite.com"):
        return "workday"
    if host_matches(url, *AGGREGATORS) or (extra_aggregators and host_matches(url, *extra_aggregators)):
        return None
    # A company careers page that embeds a Greenhouse form carries its job id in gh_jid.
    if "gh_jid=" in url:
        return "greenhouse"
    return "generic"


def site_host(url: str | None) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""
