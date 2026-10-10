"""CDP mode end to end: a Chromium started with --remote-debugging-port stands in for the user's
own Chrome, CdpBrowser attaches to it the same way it attaches to the real one, and the LinkedIn
submitter walks a look-alike Easy Apply modal served locally."""
import json
import os
import socket
import subprocess
import tempfile
import time

import pytest

from backend.tests.autoapply.test_docs_pdf import _chromium_available
from backend.tests.autoapply.test_submitters import site, no_pauses, _planner, Fake, packet  # noqa: F401

pytestmark = pytest.mark.skipif(not _chromium_available(), reason="no Playwright Chromium")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def user_chrome():
    """A separate Chromium process with DevTools open, like `chrome --remote-debugging-port`."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        exe = p.chromium.executable_path
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="aa-chrome-")
    proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                             "--no-first-run", "--no-sandbox", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import httpx
    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=0.5)
            break
        except Exception:
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=10)


async def _run_cdp(sub, cdp_url, pkt, planner):
    from backend.autoapply.cdp import CdpBrowser
    async with CdpBrowser(cdp_url) as page:
        # The stand-in profile is shared across tests; start each run with empty site storage.
        await page.goto(pkt.url)
        await page.evaluate("() => localStorage.clear()")
        out = await sub.run(page, pkt, planner)
        stored = await page.evaluate("() => localStorage.getItem('submitted')")
    return out, json.loads(stored) if stored else None


async def test_status_reports_reachable(user_chrome):
    from backend.autoapply.cdp import status
    st = await status(user_chrome)
    assert st["reachable"] is True and st["linkedin_logged_in"] is False
    assert (await status("http://127.0.0.1:1"))["reachable"] is False


async def test_cdp_leaves_user_tabs_alone(user_chrome, site):  # noqa: F811
    from playwright.async_api import async_playwright
    from backend.autoapply.cdp import CdpBrowser, resolve_ws
    async with CdpBrowser(user_chrome) as page:
        await page.goto(f"{site}/careers/job/")
        assert await page.evaluate("() => !!window.__aa")
    pw = await async_playwright().start()
    try:
        b = await pw.chromium.connect_over_cdp(await resolve_ws(user_chrome))
        urls = [p.url for p in b.contexts[0].pages]
        assert urls == ["about:blank"]          # our tab closed, the user's tab untouched
    finally:
        await pw.stop()


async def test_linkedin_easy_apply_live(user_chrome, site, packet):  # noqa: F811
    from backend.autoapply.submitters.modal import LinkedInEasyApply
    planner, fake = _planner(Fake(choice=0, text="6"))
    out, stored = await _run_cdp(LinkedInEasyApply(), user_chrome, packet(f"{site}/li/jobs/view/1/", dry_run=False), planner)
    assert out.status == "submitted", (out.error, out.steps)
    assert "application was sent" in out.confirmation_text.lower()
    assert stored["email"] == "ada.profile@example.com"     # LinkedIn's prefill kept
    assert stored["phone"] == "+1 555 0100"
    assert stored["aws"] == "6" and stored["commute"] == "Yes"
    assert stored["resume"] == "Ada_Resume.pdf"
    assert stored["follow"] is False                         # "follow company" unticked


async def test_linkedin_dry_run_stops_at_submit(user_chrome, site, packet):  # noqa: F811
    from backend.autoapply.submitters.modal import LinkedInEasyApply
    planner, _ = _planner(Fake(choice=0, text="6"))
    out, stored = await _run_cdp(LinkedInEasyApply(), user_chrome, packet(f"{site}/li/jobs/view/1/"), planner)
    assert out.status == "dry_run_ok", (out.error, out.steps)
    assert stored is None


async def test_linkedin_rejected_step_parks(user_chrome, site, packet):  # noqa: F811
    from backend.autoapply.submitters.modal import LinkedInEasyApply
    planner, _ = _planner(Fake(choice=0, text="about six"))
    out, stored = await _run_cdp(LinkedInEasyApply(), user_chrome, packet(f"{site}/li/jobs/view/1/", dry_run=False), planner)
    assert out.status == "needs_human" and stored is None
    assert "whole number" in out.questions[0]["question"]


async def test_checkpoint_pauses_site(user_chrome, site, packet):  # noqa: F811
    from backend.autoapply.submitters.modal import LinkedInEasyApply
    planner, _ = _planner()
    out, _ = await _run_cdp(LinkedInEasyApply(), user_chrome, packet(f"{site}/li/checkpoint/"), planner)
    assert out.status == "needs_human" and out.pause_site == "linkedin"


async def test_already_applied_is_skipped(user_chrome, site, packet):  # noqa: F811
    from backend.autoapply.submitters.modal import LinkedInEasyApply
    planner, _ = _planner()
    out, _ = await _run_cdp(LinkedInEasyApply(), user_chrome, packet(f"{site}/li/jobs/view/2/"), planner)
    assert out.status == "already"


async def test_linkedin_unlabeled_late_dialog(user_chrome, site, packet):  # noqa: F811
    """Current LinkedIn: the apply box has no role=dialog, renders seconds after the click, and job
    cards above the button also say "Easy Apply" in their link text."""
    from backend.autoapply.submitters.modal import LinkedInEasyApply
    planner, _ = _planner(Fake(choice=0, text="6"))
    out, stored = await _run_cdp(LinkedInEasyApply(), user_chrome, packet(f"{site}/li/jobs/view/3/", dry_run=False), planner)
    assert out.status == "submitted", (out.error, out.steps)
    assert stored["phone"] == "+1 555 0100"


async def test_linkedin_closed_posting(user_chrome, site, packet):  # noqa: F811
    from backend.autoapply.submitters.modal import LinkedInEasyApply
    planner, _ = _planner()
    out, stored = await _run_cdp(LinkedInEasyApply(), user_chrome, packet(f"{site}/li/jobs/view/4/", dry_run=False), planner)
    assert out.status == "closed" and "no longer accepting" in out.error.lower(), (out.error, out.steps)
    assert stored is None


def test_paused_site_is_not_picked(test_db, make_job, aa_settings):
    from datetime import datetime, timedelta, timezone
    from datetime import date
    from backend.autoapply import pacing, settings
    from backend.autoapply.models import AutoApplyItem
    pacing.reset()
    now = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)
    aa_settings(autoapply_paused_until={"linkedin": (now + timedelta(hours=5)).isoformat()})
    j = make_job("A", url="https://www.linkedin.com/jobs/view/1")
    test_db.add(AutoApplyItem(job_id=j.id, run_date=date(2026, 10, 9), rank=1, submitter="linkedin",
                              status="ready", company_key="a"))
    test_db.commit()
    cfg = settings.get_all(test_db)
    assert pacing.pick_next(test_db, cfg, now)[1] == "nothing ready"
    assert pacing.pick_next(test_db, cfg, now + timedelta(hours=6))[0] is not None
