"""The applier hand-off: request shape, result mapping, and what the runner does with each outcome."""
import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from backend.autoapply import applier_client
from backend.autoapply.models import AutoApplyItem, AutoApplyQuestion
from backend.autoapply.submitters.answers import Context
from backend.autoapply.submitters.base import Packet

REAL_CLIENT = httpx.AsyncClient
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def _packet(tmp_path, **kw):
    pdf = tmp_path / "Ada_Dev_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    ctx = Context(answers={"work_auth": "yes"}, patterns={}, synonyms={}, schema={}, decline_self_id=True,
                  qa_bank=[{"question": "Years of Go?", "answer": "3"}], persona_text="Name: Ada Dev",
                  company="Acme", title="Engineer", resume_text="Ada Dev\nPython, Go")
    return Packet(item_id="1", company="Acme", title="Engineer", url="https://jobs.lever.co/acme/1",
                  resume_pdf=str(pdf), folder=str(tmp_path / "item"), ctx=ctx, **kw)


def test_request_body_carries_files_and_answers(tmp_path):
    body = applier_client.request_body(_packet(tmp_path, dry_run=False), {"autoapply_agent_timeout_s": 600})
    assert body["dry_run"] is False and body["timeout_s"] == 600
    assert body["files"][0]["kind"] == "resume" and body["files"][0]["filename"] == "Ada_Dev_Resume.pdf"
    assert base64.b64decode(body["files"][0]["content_b64"]) == b"%PDF-1.4 fake"
    assert body["qa_bank"][0]["answer"] == "3" and body["answers"] == {"work_auth": "yes"}
    assert body["applicant"] == "Name: Ada Dev" and "Python" in body["resume_text"]


@pytest.mark.parametrize("result,status,err_part", [
    ({"status": "submitted", "confirmation": "Thanks!"}, "submitted", None),
    ({"status": "dry_run_ok"}, "dry_run_ok", None),
    ({"status": "closed", "reason": "404"}, "closed", "404"),
    ({"status": "already"}, "already", "already"),
    ({"status": "captcha"}, "needs_human", "captcha"),
    ({"status": "login_required"}, "needs_human", "login"),
    ({"status": "failed", "reason": "Claude usage limit", "quota": True}, "retry", "usage limit"),
    ({"status": "failed", "reason": "form broke"}, "failed", "form broke"),
    ({}, "failed", "agent failed"),
])
def test_to_outcome_maps_statuses(tmp_path, result, status, err_part):
    out = applier_client.to_outcome({"result": result, "steps": ["navigate x"]}, _packet(tmp_path))
    assert out.status == status
    if err_part:
        assert err_part in (out.error or "")
    assert out.receipt["engine"] == "agent"


def test_to_outcome_questions_screenshots_and_submit_click(tmp_path):
    run = {"result": {"status": "needs_human", "reason": "clearance", "submit_clicked": True,
                      "questions": [{"question": "Do you hold TS/SCI?", "field_type": "radio", "options": ["Yes", "No"]}]},
           "steps": ["click Submit"], "screenshots": [{"name": "final.png", "b64": PNG}],
           "usage": {"turns": 12, "cost_usd": 0.4}, "elapsed_s": 80}
    out = applier_client.to_outcome(run, _packet(tmp_path))
    assert out.questions == [{"question": "Do you hold TS/SCI?", "field_type": "radio", "options": ["Yes", "No"]}]
    assert "clicked submit" in out.steps
    assert len(out.screenshots) == 1 and open(out.screenshots[0], "rb").read().startswith(b"\x89PNG")
    assert out.receipt["turns"] == 12 and out.llm_calls == 12


def _mock_client(monkeypatch, handler):
    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return REAL_CLIENT(*a, **kw)
    monkeypatch.setattr(applier_client.httpx, "AsyncClient", factory)
    monkeypatch.setattr(applier_client, "POLL_S", 0)


async def test_run_polls_until_done(tmp_path, monkeypatch):
    calls = {"get": 0}

    def handler(req):
        if req.method == "POST" and req.url.path == "/runs":
            body = json.loads(req.content)
            assert body["url"] == "https://jobs.lever.co/acme/1"
            return httpx.Response(200, json={"id": "r1", "state": "running"})
        calls["get"] += 1
        if calls["get"] < 3:
            return httpx.Response(200, json={"id": "r1", "state": "running", "steps": ["navigate"]})
        return httpx.Response(200, json={"id": "r1", "state": "done", "steps": ["navigate", "click Submit"],
                                         "result": {"status": "submitted", "submit_clicked": True}})
    _mock_client(monkeypatch, handler)
    out = await applier_client.run(_packet(tmp_path), {"autoapply_applier_url": "http://applier:8080",
                                                       "autoapply_cover_letter": "off"})
    assert out.status == "submitted" and calls["get"] == 3


async def test_run_busy_or_down_means_retry(tmp_path, monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(409, json={"detail": "busy"}))
    out = await applier_client.run(_packet(tmp_path), {"autoapply_applier_url": "http://a", "autoapply_cover_letter": "off"})
    assert out.status == "retry" and "busy" in out.error

    def down(req):
        raise httpx.ConnectError("refused")
    _mock_client(monkeypatch, down)
    out = await applier_client.run(_packet(tmp_path), {"autoapply_applier_url": "http://a", "autoapply_cover_letter": "off"})
    assert out.status == "retry" and "unreachable" in out.error


@pytest.fixture
def ready_item(test_db, make_job, aa_settings, tmp_path):
    aa_settings(autoapply_data_dir=str(tmp_path), autoapply_enabled=True)
    pdf = tmp_path / "Ada_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    job = make_job("Acme", url="https://jobs.lever.co/acme/123")
    it = AutoApplyItem(job_id=job.id, run_date=datetime.now(timezone.utc).date(), rank=1, score=90,
                       submitter="lever", status="ready", resume_pdf_path=str(pdf), base_resume_name="SDE",
                       company_key="acme")
    test_db.add(it)
    test_db.commit()
    return it


async def test_runner_agent_engine_outcomes(test_db, ready_item, aa_settings, monkeypatch):
    from backend.autoapply import runner
    from backend.autoapply.submitters import answers
    from backend.autoapply.submitters.base import Outcome
    aa_settings(autoapply_engine="agent", autoapply_dry_run=False)
    monkeypatch.setattr(answers, "load_context", lambda *a, **k: None)
    results = iter([
        Outcome(status="retry", error="applier busy"),
        Outcome(status="needs_human", error="needs answers",
                questions=[{"question": "Clearance?", "field_type": "text", "options": []}]),
    ])

    async def fake_run(packet, cfg):
        assert packet.dry_run is False
        return next(results)
    monkeypatch.setattr(applier_client, "run", fake_run)
    monkeypatch.setattr(runner, "submitter_for", lambda name: object)

    await runner.submit_item(ready_item.id)
    test_db.expire_all()
    it = test_db.query(AutoApplyItem).get(ready_item.id)
    na = it.next_attempt_at
    assert it.status == "ready" and (na if na.tzinfo else na.replace(tzinfo=timezone.utc)) > datetime.now(timezone.utc)

    await runner.submit_item(ready_item.id)
    test_db.expire_all()
    it = test_db.query(AutoApplyItem).get(ready_item.id)
    assert it.status == "needs_human"
    assert test_db.query(AutoApplyQuestion).filter(AutoApplyQuestion.item_id == it.id).one().question == "Clearance?"
