"""Hand one application to the applier service (applier/: Claude Code driving its own Chromium) and turn
its answer into the same Outcome the scripted submitters return."""
import asyncio
import base64
import logging
import os

import httpx

from backend.autoapply.submitters.base import Outcome, Packet

logger = logging.getLogger("jobnavigator.autoapply.applier")

POLL_S = 5
# The applier reports these as needs_human with a reason a person can act on.
HUMAN_REASON = {
    "captcha": "captcha on the form: solve it in the live view (or by hand), then Retry submit or Mark applied",
    "login_required": "the site wants a login or account: create one by hand (or in the live view), then Retry submit",
}


def _headers() -> dict:
    token = os.environ.get("APPLIER_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _file(kind: str, path: str | None) -> dict | None:
    if not path or not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return {"kind": kind, "filename": os.path.basename(path), "content_b64": base64.b64encode(f.read()).decode()}


async def health(base_url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base_url.rstrip('/')}/health")
            return {"reachable": True, **r.json()}
    except Exception as e:
        return {"reachable": False, "error": str(e) or type(e).__name__}


def _dashboard_key() -> str:
    """The key the extension and draft_answer send as X-API-Key (same one the dashboard checks)."""
    try:
        from backend.config import INITIAL_API_KEY
        from backend.models.db import SessionLocal, Setting
        db = SessionLocal()
        try:
            row = db.query(Setting).filter(Setting.key == "dashboard_api_key").first()
            return (row.value if row and row.value else INITIAL_API_KEY) or ""
        finally:
            db.close()
    except Exception:
        return ""


def request_body(packet: Packet, cfg: dict) -> dict:
    ctx = packet.ctx
    files = [f for f in (_file("resume", packet.resume_pdf), _file("cover_letter", packet.cover_letter_pdf)) if f]
    return {
        "url": packet.url, "company": packet.company, "title": packet.title, "dry_run": packet.dry_run,
        "applicant": ctx.persona_text or {}, "answers": ctx.answers or {}, "qa_bank": list(ctx.qa_bank or []),
        "resume_text": ctx.resume_text or "", "cover_letter_text": packet.cover_letter_text,
        "decline_self_id": bool(ctx.decline_self_id), "files": files,
        "extra_instructions": cfg.get("autoapply_agent_instructions") or "",
        "model": cfg.get("autoapply_agent_model") or "",
        "timeout_s": int(cfg.get("autoapply_agent_timeout_s") or 600),
        "max_turns": int(cfg.get("autoapply_agent_max_turns") or 60),
        "autofill": ({"server_url": cfg.get("autoapply_agent_backend_url") or "http://backend:8000",
                      "api_key": _dashboard_key()}
                     if cfg.get("autoapply_agent_autofill", True) else None),
    }


def to_outcome(run: dict, packet: Packet) -> Outcome:
    """Applier run (state done) -> Outcome; screenshots are written into the item's folder."""
    res = run.get("result") or {}
    status = res.get("status") or "failed"
    reason = (res.get("reason") or "").strip()
    out = Outcome(steps=list(run.get("steps") or []))
    if res.get("submit_clicked"):
        out.steps.append("clicked submit")      # runner.CLICKED_SUBMIT: never resend unattended
    usage = run.get("usage") or {}
    out.receipt = {k: v for k, v in {
        "engine": "agent", "final_url": res.get("final_url"), "elapsed_s": run.get("elapsed_s"),
        "turns": usage.get("turns"), "cost_usd": usage.get("cost_usd"),
    }.items() if v not in (None, "")}
    out.llm_calls = int(usage.get("turns") or 0)
    os.makedirs(packet.folder, exist_ok=True)
    for i, s in enumerate(run.get("screenshots") or []):
        path = os.path.join(packet.folder, f"agent-{i:02d}-{os.path.basename(s.get('name') or 'shot.png')}")
        try:
            with open(path, "wb") as f:
                f.write(base64.b64decode(s["b64"]))
            out.screenshots.append(path)
        except Exception as e:
            out.steps.append(f"screenshot {i} not saved: {e}")
    out.confirmation_text = res.get("confirmation") or None

    if status == "submitted":
        out.status = "submitted"
    elif status == "dry_run_ok":
        out.status = "dry_run_ok"
    elif status == "closed":
        out.status, out.error = "closed", reason or "posting closed"
    elif status == "already":
        out.status, out.error = "already", reason or "already applied"
    elif status in HUMAN_REASON:
        out.status, out.error = "needs_human", HUMAN_REASON[status] + (f" ({reason})" if reason else "")
    elif status == "needs_human":
        out.status, out.error = "needs_human", reason or "the agent needs answers it does not have"
        for q in res.get("questions") or []:
            if isinstance(q, dict) and q.get("question"):
                out.questions.append({"question": str(q["question"]), "field_type": q.get("field_type"),
                                      "options": [str(o) for o in (q.get("options") or [])]})
    elif res.get("quota"):
        out.status, out.error = "retry", reason or "Claude usage limit"
    else:
        out.status, out.error = "failed", reason or "agent failed"
    return out


async def run(packet: Packet, cfg: dict) -> Outcome:
    base = (cfg.get("autoapply_applier_url") or "").rstrip("/")
    # The agent cannot ask for a letter halfway through a form, so it gets one up front.
    if cfg.get("autoapply_cover_letter") != "off" and not packet.cover_letter_pdf and packet.make_cover_letter:
        try:
            packet.cover_letter_pdf, packet.cover_letter_text = await packet.make_cover_letter()
        except Exception as e:
            logger.warning(f"AutoApply: cover letter for {packet.company} failed, applying without: {e}")
    body = request_body(packet, cfg)
    deadline = body["timeout_s"] + 120
    try:
        async with httpx.AsyncClient(timeout=30, headers=_headers()) as c:
            r = await c.post(f"{base}/runs", json=body)
            if r.status_code == 409:
                return Outcome(status="retry", error="applier busy with another application")
            r.raise_for_status()
            run_id = r.json()["id"]
            waited = 0
            while True:
                await asyncio.sleep(POLL_S)
                waited += POLL_S
                g = await c.get(f"{base}/runs/{run_id}")
                g.raise_for_status()
                data = g.json()
                if data.get("state") == "done":
                    return to_outcome(data, packet)
                if waited > deadline:
                    await c.post(f"{base}/runs/{run_id}/cancel")
                    out = to_outcome({**data, "result": {"status": "failed", "reason": "applier did not finish in time"}}, packet)
                    return out
    except asyncio.CancelledError:
        raise
    except httpx.HTTPError as e:
        return Outcome(status="retry", error=f"applier unreachable at {base}: {e or type(e).__name__}")
