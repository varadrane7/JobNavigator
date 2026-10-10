import pytest

from backend.autoapply.submitters.registry import classify


@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse"),
    ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
    ("https://acme.com/careers?gh_jid=123", "greenhouse"),
    ("https://jobs.lever.co/acme/abc-def", "lever"),
    ("https://jobs.ashbyhq.com/acme/abc", "ashby"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/ext/job/x", "workday"),
    ("https://www.linkedin.com/jobs/view/123", "linkedin"),
    ("https://www.ziprecruiter.com/c/Acme/Job/x", "ziprecruiter"),
    ("https://www.indeed.com/viewjob?jk=1", None),
    ("https://careers.acme.com/jobs/1", "generic"),
    ("https://evil-greenhouse.io.example.com/x", "generic"),
    ("javascript:alert(1)", None),
    (None, None),
])
def test_classify(url, expected):
    assert classify(url) == expected


def test_settings_seed_and_typed_read(test_db):
    from backend.autoapply import settings
    settings.seed(test_db)
    cfg = settings.get_all(test_db)
    assert cfg["autoapply_daily_cap"] == 100
    assert cfg["autoapply_mode"] == "review"
    assert cfg["autoapply_dry_run"] is True
    assert cfg["autoapply_site_caps"]["linkedin"] == 15


def test_settings_seed_keeps_user_values(test_db):
    from backend.autoapply import settings
    settings.seed(test_db)
    settings.update(test_db, {"autoapply_daily_cap": 30})
    settings.seed(test_db)
    assert settings.get(test_db, "autoapply_daily_cap") == 30


@pytest.mark.parametrize("key,value", [
    ("autoapply_daily_cap", "lots"),
    ("autoapply_daily_cap", -1),
    ("autoapply_mode", "yolo"),
    ("autoapply_queue_cron", "* * *"),
    ("autoapply_queue_cron", "99 * * * *"),
    ("autoapply_nonexistent", 1),
])
def test_settings_update_rejects(test_db, key, value):
    from backend.autoapply import settings
    settings.seed(test_db)
    with pytest.raises(ValueError):
        settings.update(test_db, {key: value})


def test_settings_bad_update_writes_nothing(test_db):
    from backend.autoapply import settings
    settings.seed(test_db)
    with pytest.raises(ValueError):
        settings.update(test_db, {"autoapply_daily_cap": 5, "autoapply_mode": "nope"})
    assert settings.get(test_db, "autoapply_daily_cap") == 100


def test_junk_stored_value_falls_back_to_default(test_db):
    from backend.autoapply import settings
    from backend.models.db import Setting
    settings.seed(test_db)
    test_db.query(Setting).filter(Setting.key == "autoapply_daily_cap").first().value = "abc"
    test_db.commit()
    assert settings.get(test_db, "autoapply_daily_cap") == 100
