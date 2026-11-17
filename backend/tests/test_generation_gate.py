"""The shared generation gate (`tailoring_max_concurrent`), now taken by cover-letter generation alone."""
from backend.models.db import Setting


# The gate lives in job_monitor (launch_background takes it before the worker opens a session).

def test_semaphore_default_is_two(test_db):
    """Absent setting → limit 2."""
    import backend.api.routes_resumes as rr
    import backend.job_monitor as jm
    jm.reset_limiter("tailoring")
    assert rr._get_tailoring_semaphore().value == 2


def test_semaphore_reads_setting(test_db):
    """Setting override is honored."""
    test_db.add(Setting(key="tailoring_max_concurrent", value="5"))
    test_db.commit()
    import backend.api.routes_resumes as rr
    import backend.job_monitor as jm
    jm.reset_limiter("tailoring")
    assert rr._get_tailoring_semaphore().value == 5


def test_reset_clears_cached_semaphore(test_db):
    """reset_tailoring_semaphore() forces re-read on next call."""
    import backend.api.routes_resumes as rr
    import backend.job_monitor as jm

    jm.reset_limiter("tailoring")
    assert rr._get_tailoring_semaphore().value == 2
    test_db.add(Setting(key="tailoring_max_concurrent", value="7"))
    test_db.commit()
    assert rr._get_tailoring_semaphore().value == 2      # still the cached gate
    rr.reset_tailoring_semaphore()
    assert rr._get_tailoring_semaphore().value == 7


