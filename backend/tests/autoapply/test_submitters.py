"""Submitters against local look-alike ATS forms in a real headless Chromium. The fixtures copy
the shapes that matter (hidden file input behind "Attach", ARIA comboboxes, radio cards, a
two-step careers form, a visible reCAPTCHA challenge); nothing here touches the internet."""
import functools
import http.server
import json
import os
import threading

import pytest

from backend.tests.autoapply.test_docs_pdf import _chromium_available

pytestmark = pytest.mark.skipif(not _chromium_available(), reason="no Playwright Chromium")

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(scope="module")
def site():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=FIXTURES)
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    from backend.autoapply.submitters import base, generic

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(base, "_pause", _none)
    monkeypatch.setattr(generic, "_pause", _none)


def _context():
    from backend.seed import DEFAULT_SETTINGS
    from backend.autofill_schema import ANSWER_SCHEMA, project_answers
    from backend.autoapply.submitters.answers import Context
    persona = {
        "contact": {"first_name": "Ada", "last_name": "Dev", "email": "ada@example.com", "phone": "+1 555 0100",
                    "linkedin": "https://linkedin.com/in/ada", "current_company": "Acme"},
        "work_auth": {"authorized_us": True, "requires_sponsorship_now": False, "requires_sponsorship_future": False},
        "demographics": {"gender": "male"},
        "preferences": {"notice_period": "2 weeks"},
    }
    return Context(answers=project_answers(persona),
                   patterns=json.loads(DEFAULT_SETTINGS["autofill_field_patterns"][0]),
                   synonyms=json.loads(DEFAULT_SETTINGS["autofill_option_synonyms"][0]),
                   schema=ANSWER_SCHEMA, decline_self_id=False, qa_bank=[], persona_text="Ada Dev, engineer",
                   company="Acme", title="Engineer")


class Fake:
    def __init__(self, choice=1, text="Because I like building reliable systems."):
        self.choice, self.text, self.asked = choice, text, []

    async def choose(self, q, options, ctx):
        self.asked.append(("choose", q, list(options)))
        return self.choice

    async def write(self, q, max_chars, ctx):
        self.asked.append(("write", q))
        return self.text


def _planner(fake=None):
    from backend.autoapply.submitters.answers import Planner
    fake = fake or Fake()
    return Planner(_context(), llm_choose=fake.choose, llm_write=fake.write), fake


@pytest.fixture
def packet(tmp_path):
    from backend.autoapply.submitters.base import Packet
    pdf = tmp_path / "Ada_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%fake\n")

    def _make(url, dry_run=True):
        return Packet(item_id="t", company="Acme", title="Engineer", url=url, resume_pdf=str(pdf),
                      folder=str(tmp_path), ctx=_context(), dry_run=dry_run)
    return _make


async def _run(sub, packet, planner):
    from backend.autoapply.runner import HeadlessBrowser
    async with HeadlessBrowser() as page:
        out = await sub.run(page, packet, planner)
        stored = await page.evaluate("() => localStorage.getItem('submitted')")
        return out, json.loads(stored) if stored else None, page.url


def _gh():
    from backend.autoapply.submitters.ats import Greenhouse
    g = Greenhouse()
    g.hosted = ("127.0.0.1",)
    return g


async def test_greenhouse_dry_run_fills_everything_and_never_submits(site, packet):
    planner, fake = _planner()
    out, stored, url = await _run(_gh(), packet(f"{site}/gh/jobs/1/"), planner)
    assert out.status == "dry_run_ok", (out.error, out.steps)
    assert stored is None and "/gh/jobs/1" in url
    r = out.receipt
    assert r["First Name"] == "Ada" and r["Last Name"] == "Dev" and r["Email"] == "ada@example.com"
    assert r["Resume"] == "Ada_Resume.pdf"
    assert r["LinkedIn Profile"] == "https://linkedin.com/in/ada"
    assert r["Are you legally authorized to work in the United States?"] == "Yes"
    assert r["Will you now or in the future require visa sponsorship?"] == "No"
    assert r["Gender"] == "Male"
    assert r["Which office would you prefer?"] == "Remote"           # LLM choice, required
    assert r["Why do you want to work at Acme?"].startswith("Because")  # LLM write, required
    assert r["I acknowledge the privacy policy"] == "checked"
    assert out.screenshots and all(os.path.isfile(p) for p in out.screenshots)


async def test_greenhouse_live_submit_confirms(site, packet):
    planner, _ = _planner()
    out, stored, url = await _run(_gh(), packet(f"{site}/gh/jobs/1/", dry_run=False), planner)
    assert out.status == "submitted", (out.error, out.steps)
    assert "/gh/confirmation" in url and "Thank you for applying" in out.confirmation_text
    assert stored["first_name"] == "Ada" and stored["question_3"] == "1" and stored["question_4"] == "No"
    assert stored["resume"] == "Ada_Resume.pdf" and stored["consent"] == "on"


async def test_lever_radio_card_and_confirmation(site, packet):
    from backend.autoapply.submitters.ats import Lever
    planner, fake = _planner(Fake(choice=2))
    out, stored, url = await _run(Lever(), packet(f"{site}/lever/acme/123", dry_run=False), planner)
    assert out.status == "submitted", (out.error, out.steps)
    assert stored["name"] == "Ada Dev" and stored["cards[abc][field0]"] == "6+"
    assert stored["org"] == "Acme"
    assert ("choose", "How many years of C# experience do you have?", ["0-2", "3-5", "6+"]) in fake.asked


async def test_unanswerable_required_parks_for_human(site, packet):
    from backend.autoapply.submitters.ats import Lever
    planner, _ = _planner(Fake(choice=None))
    out, stored, _url = await _run(Lever(), packet(f"{site}/lever/acme/123", dry_run=False), planner)
    assert out.status == "needs_human" and stored is None
    assert [q["question"] for q in out.questions] == ["How many years of C# experience do you have?"]
    assert out.questions[0]["options"] == ["0-2", "3-5", "6+"]


async def test_generic_two_step_dry_run_and_live(site, packet):
    from backend.autoapply.submitters.generic import Generic
    planner, _ = _planner()
    out, stored, url = await _run(Generic(), packet(f"{site}/careers/job/"), planner)
    assert out.status == "dry_run_ok", (out.error, out.steps)
    assert stored is None and out.receipt["What is your notice period?"] == "2 weeks"

    planner, _ = _planner()
    out, stored, url = await _run(Generic(), packet(f"{site}/careers/job/", dry_run=False), planner)
    assert out.status == "submitted", (out.error, out.steps)
    assert stored == {"q": "2 weeks"} and "/careers/done" in url


async def test_captcha_parks_for_human(site, packet):
    from backend.autoapply.submitters.generic import Generic
    planner, _ = _planner()
    out, _stored, _url = await _run(Generic(), packet(f"{site}/captcha/"), planner)
    assert out.status == "needs_human" and "captcha" in out.error
