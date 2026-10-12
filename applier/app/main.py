"""Applier HTTP API. One application at a time (there is one browser):

  POST /runs            start one; 409 while another is running
  GET  /runs/{id}       progress; result + screenshots once done
  POST /runs/{id}/cancel
  GET  /health

Set APPLIER_TOKEN to require `Authorization: Bearer <token>` on everything but /health."""
import asyncio
import base64
import glob
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app import agent, browser, prompt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("applier")

TOKEN = os.environ.get("APPLIER_TOKEN", "")
DEFAULT_MODEL = os.environ.get("APPLIER_MODEL", "sonnet")
WORK = os.environ.get("APPLIER_WORK", "/work")
KEEP_RUNS = 30

app = FastAPI(title="Applier")


class FileIn(BaseModel):
    kind: str                      # resume | cover_letter | other
    filename: str
    content_b64: str


class RunRequest(BaseModel):
    url: str
    company: str = ""
    title: str = ""
    dry_run: bool = True
    applicant: dict | str = Field(default_factory=dict)
    answers: dict = Field(default_factory=dict)
    qa_bank: list = Field(default_factory=list)
    resume_text: str = ""
    cover_letter_text: Optional[str] = None
    decline_self_id: bool = True
    extra_instructions: str = ""
    files: list[FileIn] = Field(default_factory=list)
    model: str = ""
    timeout_s: int = 600
    max_turns: int = 60
    autofill: Optional[dict] = None     # {server_url, api_key}: extension autofill + draft_answer tool


class Run:
    def __init__(self, req: RunRequest):
        self.id = uuid.uuid4().hex[:12]
        self.req = req
        self.workdir = os.path.join(WORK, self.id)
        self.started = time.time()
        self.finished: float | None = None
        self.steps: list[str] = []
        self.transcript: list[str] = []
        self.usage: dict = {}
        self.result: dict | None = None
        self.clicked_submit_like = False
        self.screenshots: list[dict] = []
        self.proc = None
        self.autofill = False
        self.task: asyncio.Task | None = None

    def view(self, full: bool) -> dict:
        out = {"id": self.id, "state": "done" if self.finished else "running", "url": self.req.url,
               "company": self.req.company, "title": self.req.title, "dry_run": self.req.dry_run,
               "elapsed_s": int((self.finished or time.time()) - self.started),
               "steps": self.steps, "result": self.result, "usage": self.usage}
        if full:
            out["transcript"] = self.transcript[-60:]
            out["screenshots"] = self.screenshots
        return out


RUNS: dict[str, Run] = {}
_lock = asyncio.Lock()
_current: Run | None = None


def auth(authorization: str = Header(default="")):
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "bad token")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(name or "file"))[:80] or "file"


async def _go(run: Run) -> None:
    global _current
    try:
        os.makedirs(os.path.join(run.workdir, "out"), exist_ok=True)
        files = []
        for f in run.req.files:
            name = _safe_name(f.filename)
            with open(os.path.join(run.workdir, name), "wb") as fh:
                fh.write(base64.b64decode(f.content_b64))
            files.append((f.kind, os.path.join(run.workdir, name)))
        if not await browser.ensure_running():
            run.result = {"status": "failed", "reason": "browser did not start"}
            return
        await browser.reset_tabs()
        af = run.req.autofill or {}
        draft_env = None
        if af.get("server_url"):
            ext_ok = await browser.configure_extension(af["server_url"], af.get("api_key", ""))
            run.steps.append("autofill extension " + ("ready" if ext_ok else "not available"))
            draft_env = {"JN_URL": af["server_url"], "JN_KEY": af.get("api_key", ""),
                         "JN_COMPANY": run.req.company, "JN_TITLE": run.req.title}
            run.autofill = ext_ok
        text = prompt.build(run.req, files, extension=bool(run.autofill), draft=bool(draft_env))
        await agent.run(run, text, run.req.model or DEFAULT_MODEL, run.req.timeout_s, run.req.max_turns, draft_env)
    except asyncio.CancelledError:
        run.result = {"status": "failed", "reason": "cancelled", "submit_clicked": run.clicked_submit_like}
    except Exception as e:
        logger.exception("run %s crashed", run.id)
        run.result = {"status": "failed", "reason": f"applier crashed: {e}"[:500],
                      "submit_clicked": run.clicked_submit_like}
    finally:
        shot, url = await browser.screenshot()
        if shot:
            run.screenshots.append({"name": "final.png", "url": url, "b64": shot})
        # Screenshots the agent chose to take, oldest first.
        for p in sorted(glob.glob(os.path.join(run.workdir, "out", "*.png")), key=os.path.getmtime)[-6:]:
            run.screenshots.append({"name": os.path.basename(p), "url": "", "b64": browser.b64(p)})
        if run.result is not None:
            run.result.setdefault("final_url", url)
        run.finished = time.time()
        shutil.rmtree(run.workdir, ignore_errors=True)
        _current = None
        for old in sorted(RUNS.values(), key=lambda r: r.started)[:-KEEP_RUNS]:
            RUNS.pop(old.id, None)


@app.get("/health")
async def health():
    v = await browser.version()
    try:
        cli = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        cli = ""
    return {"ok": bool(v and cli), "browser": (v or {}).get("Browser"), "claude": cli,
            "busy": _current.id if _current else None,
            "auth": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")),
            "live_view": bool(os.environ.get("APPLIER_VNC_PASSWORD")), "model": DEFAULT_MODEL}


@app.post("/runs", dependencies=[Depends(auth)])
async def start(req: RunRequest):
    global _current
    async with _lock:
        if _current is not None:
            raise HTTPException(409, f"busy with run {_current.id}")
        run = Run(req)
        RUNS[run.id] = run
        _current = run
        run.task = asyncio.create_task(_go(run))
    return run.view(False)


@app.get("/runs/{run_id}", dependencies=[Depends(auth)])
async def get_run(run_id: str):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    return run.view(bool(run.finished))


@app.post("/runs/{run_id}/cancel", dependencies=[Depends(auth)])
async def cancel(run_id: str):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    if run.proc and run.proc.returncode is None:
        run.proc.kill()
    if run.task and not run.task.done():
        run.task.cancel()
    return {"cancelled": run.id}
