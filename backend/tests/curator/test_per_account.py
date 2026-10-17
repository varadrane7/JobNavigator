"""MT-22 · the daily curator runs once per account, on that account's pool and settings."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.accounts.context import current_account, use_account
from backend.accounts.settings import get_setting, put_setting
from backend.models.db import Account, Job, SectionPolicy, SessionLocal, Setting


def _job(i, company):
    now = datetime.now(timezone.utc)
    return Job(external_id=f"e{i}-{company}", company=company, title="Software Engineer",
               url=f"https://boards.greenhouse.io/{company}/{i}", description="Build backend services in Python. 3+ years.",
               status="new", discovered_at=now - timedelta(hours=2), posted_at=now - timedelta(hours=1),
               best_cv_score=60 + i, best_cv="SDE", cv_scores={"SDE": 60 + i}, loc_country="US")


@pytest.fixture
def two_accounts(test_db, monkeypatch):
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    member = Account(name="Partner", role="member", color="#2e9e6b")
    test_db.add(member)
    test_db.commit()
    for acc, tag in ((owner, "OwnerCo"), (member, "MemberCo")):
        with use_account(acc.id):
            test_db.add_all([_job(i, f"{tag}{i}") for i in range(1, 9)])
            test_db.commit()
    with use_account(owner.id):
        put_setting(test_db, "curate_count", "3")
        test_db.commit()
    put_setting(test_db, "curate_count", "2", account_id=member.id)   # the member's own row ('own' by default)
    test_db.commit()

    import backend.curator.run as run

    async def none(*a, **k):
        return None

    async def no_intel(companies, concurrency=3):
        return {}
    monkeypatch.setattr(run, "similarity", none)
    monkeypatch.setattr(run, "get_intel", no_intel)
    monkeypatch.setattr(run, "_score_unscored", none)
    monkeypatch.setattr(run, "find_contacts", lambda *a, **k: _empty())
    monkeypatch.setattr(run, "find_network_contacts", lambda *a, **k: _empty())
    monkeypatch.setattr(run, "notify", none)
    return owner, member, run


async def _empty():
    return []


async def test_each_account_curates_its_own_pool_with_its_own_count(two_accounts, test_db, monkeypatch):
    owner, member, run = two_accounts
    monkeypatch.setattr(run, "_resume_texts", lambda: ["resume"])
    from backend import scheduler
    await scheduler.run_curation.__wrapped__() if hasattr(scheduler.run_curation, "__wrapped__") else await scheduler.run_curation()
    test_db.expire_all()
    with use_account(owner.id):
        saved = test_db.query(Job).filter(Job.status == "saved").all()
        assert len(saved) == 3 and all(j.company.startswith("OwnerCo") for j in saved)
    with use_account(member.id):
        saved = test_db.query(Job).filter(Job.status == "saved").all()
        assert len(saved) == 2 and all(j.company.startswith("MemberCo") for j in saved)


async def test_an_account_without_a_resume_is_skipped(two_accounts, test_db, monkeypatch):
    owner, member, run = two_accounts
    monkeypatch.setattr(run, "_resume_texts", lambda: ["resume"] if current_account.get() == owner.id else [])
    from backend import scheduler
    await scheduler.run_curation()
    test_db.expire_all()
    with use_account(member.id):
        assert test_db.query(Job).filter(Job.status == "saved").count() == 0
        assert test_db.query(Job).filter(Job.status == "new").count() == 8
    with use_account(owner.id):
        assert test_db.query(Job).filter(Job.status == "saved").count() == 3


def test_curator_sections_split_personal_model_and_infrastructure():
    from backend.accounts import settings as S
    sec = S.SETTING_SECTIONS
    assert (sec["curator"]["scope"], sec["curator"]["default"]) == ("policy", "own")
    assert (sec["curator_llm"]["scope"], sec["curator_llm"]["default"]) == ("policy", "inherit")
    assert sec["curator_infra"]["scope"] == "owner_only"
    for key in ("curate_cron", "searxng_url", "curate_embed_model", "curate_concurrency", "llm_local_base_url"):
        assert S.section_of(key) == "curator_infra"
    assert S.section_of("curate_count") == "curator" and S.section_of("curator_llm_api_key") == "curator_llm"


def test_member_curator_model_inherits_or_uses_its_own_key(test_db):
    owner = test_db.query(Account).filter(Account.role == "owner").one()
    member = Account(name="Partner", role="member", color="#2e9e6b")
    test_db.add(member)
    test_db.commit()
    put_setting(test_db, "curator_llm_provider", "claude_api")
    put_setting(test_db, "curator_llm_api_key", "owner-key")
    put_setting(test_db, "curator_llm_provider", "openrouter", account_id=member.id)
    put_setting(test_db, "curator_llm_api_key", "member-key", account_id=member.id)
    test_db.commit()
    with use_account(member.id):
        assert get_setting(test_db, "curator_llm_api_key") == "owner-key"        # inherit (default)
        test_db.add(SectionPolicy(account_id=member.id, section="curator_llm", mode="own"))
        test_db.commit()
        assert get_setting(test_db, "curator_llm_api_key") == "member-key"
        assert get_setting(test_db, "curator_llm_provider") == "openrouter"
