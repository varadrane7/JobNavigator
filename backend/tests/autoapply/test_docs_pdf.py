"""docs.resume_pdf goes through upstream's own export endpoint with a real Chromium, so the
user's templates are what ends up in the PDF. Skipped where no browser is installed."""
import copy
import os

import pytest

from backend.tests.autoapply.resume_data import BASE


def _chromium_available() -> bool:
    try:
        from playwright._impl._driver import compute_driver_executable  # noqa: F401
    except Exception:
        return False
    return any(os.path.isdir(os.path.join(root, d)) and d.startswith("chromium")
               for root in (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright"),)
               if os.path.isdir(root) for d in os.listdir(root))


@pytest.mark.skipif(not _chromium_available(), reason="no Playwright Chromium")
async def test_resume_pdf_renders_with_upstream_template(test_db, make_resume, tmp_path):
    from backend.autoapply import docs
    r = make_resume("Cloud", json_data=copy.deepcopy(BASE))
    path = await docs.resume_pdf(str(r.id), str(tmp_path))
    with open(path, "rb") as f:
        head = f.read(5)
    assert head == b"%PDF-"
    assert os.path.basename(path).startswith("AdaDev_Cloud_Resume")
