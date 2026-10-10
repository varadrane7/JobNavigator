"""Which submitter a job URL goes to. Pure URL logic, so the queue can route jobs before any
submitter implementation exists."""
from urllib.parse import urlparse

from backend.scraper._shared.urls import host_matches

# Boards we do not submit to at all: no stable apply form, or heavy bot walls.
UNSUPPORTED = ("indeed.com", "glassdoor.com", "monster.com", "dice.com", "simplyhired.com",
               "jobright.ai", "levels.fyi", "builtin.com", "wellfound.com")


def classify(url: str | None) -> str | None:
    """Submitter name for a job URL, or None when nothing can apply there."""
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
    if host_matches(url, "linkedin.com"):
        return "linkedin"
    if host_matches(url, "ziprecruiter.com"):
        return "ziprecruiter"
    if host_matches(url, *UNSUPPORTED):
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
