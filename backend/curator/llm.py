"""The curator's LLM calls, through a chain: curator model -> curator fallback -> local model.

Defaults are Antigravity then Claude Code (the run is at 4:30, after their limits reset),
and the OpenAI (Local) server last, so a night with no cloud quota still finishes. Fit
scoring does not come through here; it uses the Scoring model like every other job.
Every call is logged like the rest of the app's calls.
"""
import asyncio
import logging
import time

from backend.analyzer.llm_client import _get_setting, call_llm
from backend.analyzer.llm_logger import log_llm_call
from backend.analyzer.model_json import parse_model_json
from backend.models.db import SessionLocal

logger = logging.getLogger("jobnavigator.curator.llm")

# A pair that failed this run is skipped for the rest of it (a spent quota stays spent).
_DEAD: set = set()


def chain() -> list[dict]:
    db = SessionLocal()
    try:
        out = []
        for base in ("curator_llm", "curator_fallback_llm"):
            p = _get_setting(db, f"{base}_provider", "")
            if p:
                out.append({"provider": p, "model": _get_setting(db, f"{base}_model", ""),
                            "api_key": _get_setting(db, f"{base}_api_key", ""),
                            "effort": _get_setting(db, f"{base}_effort", "")})
        local = {"provider": "openai_local", "model": _get_setting(db, "llm_model", "") if
                 _get_setting(db, "llm_provider", "") == "openai_local" else "", "api_key": "", "effort": ""}
        if local["model"] and all(c["provider"] != "openai_local" for c in out):
            out.append(local)
        if not out:   # nothing configured: the Primary
            out.append({"provider": None, "model": None, "api_key": None, "effort": None})
        return out
    finally:
        db.close()


def concurrency() -> int:
    db = SessionLocal()
    try:
        return max(1, int(_get_setting(db, "curate_concurrency", "4")))
    except ValueError:
        return 4
    finally:
        db.close()


_SEM: asyncio.Semaphore | None = None


def reset_run() -> None:
    """Start of a curation run: forget dead pairs, resize the concurrency gate."""
    global _SEM
    _DEAD.clear()
    _SEM = asyncio.Semaphore(concurrency())


async def complete(prompt: str, system: str, max_tokens: int = 1500, cached_prefix: str | None = None) -> dict:
    """call_llm-shaped reply from the first pair in the chain that answers."""
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(concurrency())
    last = None
    async with _SEM:
        for cfg in chain():
            key = (cfg["provider"], cfg["model"])
            if key in _DEAD:
                continue
            try:
                full = f"{cached_prefix}\n\n{prompt}" if cached_prefix and cfg["provider"] != "claude_api" else prompt
                return await call_llm(full, system, max_tokens=max_tokens,
                                      cached_prefix=cached_prefix if cfg["provider"] == "claude_api" else None,
                                      provider=cfg["provider"], model=cfg["model"],
                                      api_key=cfg["api_key"], effort=cfg["effort"])
            except Exception as e:
                last = e
                _DEAD.add(key)
                logger.warning(f"curator LLM {key} failed, moving down the chain: {str(e)[:200]}")
    raise RuntimeError(f"every curator model failed: {last}")


async def ask(prompt: str, system: str, purpose: str, max_tokens: int = 1500, job_id=None) -> str:
    started = time.monotonic()
    ok, err, usage, provider, model = True, None, {}, "", ""
    try:
        res = await complete(prompt, system, max_tokens)
        usage, provider, model = res.get("usage") or {}, res.get("provider", ""), res.get("model", "")
        return res.get("text", "")
    except Exception as e:
        ok, err = False, str(e)
        raise
    finally:
        log_llm_call(purpose=purpose, provider=provider or "curator", model=model or "", usage=usage,
                     duration_ms=int((time.monotonic() - started) * 1000), job_id=job_id,
                     success=ok, error=err)


async def ask_json(prompt: str, system: str, purpose: str, max_tokens: int = 1500, job_id=None) -> dict | None:
    """The reply's JSON object, or None when the call fails or the reply has none."""
    try:
        text = await ask(prompt, system + "\nReply with one JSON object only.", purpose, max_tokens, job_id)
        out = parse_model_json(text)
        return out if isinstance(out, dict) else None
    except Exception as e:
        logger.info(f"{purpose}: no usable JSON ({e})")
        return None
