"""AutoApply test helpers. The parent conftest's test_db builds every table on Base, so the
autoapply models only need importing before it runs."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import backend.autoapply.models  # noqa: F401  (registers autoapply_* tables on Base)


@pytest.fixture
def make_resume(test_db):
    from backend.models.db import Resume

    def _make(name, is_base=True, json_data=None):
        r = Resume(name=name, is_base=is_base, json_data=json_data or {"summary": name})
        test_db.add(r)
        test_db.commit()
        return r
    return _make


@pytest.fixture
def make_job(test_db):
    from backend.models.db import Job

    def _make(company="Acme", title="Engineer", url=None, cv_scores=None, status="new",
              age_days=0, content_hash=None):
        j = Job(
            external_id=uuid.uuid4().hex,
            company=company, title=title,
            url=url or f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{uuid.uuid4().int % 10**8}",
            cv_scores=cv_scores or {}, status=status, content_hash=content_hash,
            discovered_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        )
        test_db.add(j)
        test_db.commit()
        return j
    return _make


@pytest.fixture
def aa_settings(test_db):
    from backend.autoapply import settings
    settings.seed(test_db)

    def _set(**kw):
        settings.update(test_db, kw)
    return _set
