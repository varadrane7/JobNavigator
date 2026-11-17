"""Scraper errors shared by the ATS handlers."""


class BoardNotFoundError(RuntimeError):
    """The board itself is gone: the ATS answered 404/410 for the company's board (a
    renamed or closed company). Raised by the API handlers outside debug mode, so the
    company's run is recorded as an error, not as a quiet "no postings" warning
    (QA F6 / T3-03)."""


GONE_STATUSES = (404, 410)


def raise_if_board_gone(status_code: int, ats: str, slug: str) -> None:
    if status_code in GONE_STATUSES:
        raise BoardNotFoundError(f"{ats} board '{slug}' not found (HTTP {status_code})")


class BoardUnavailableError(RuntimeError):
    """The ATS itself is down or refusing for now (a maintenance page, a 5xx, a 429):
    nothing was read and the next run tries again. Raised so the run is recorded as an
    error that says so, not as a parse failure or a quiet "no postings" warning."""
