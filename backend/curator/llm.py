"""The curator's LLM calls: `curator_llm_*` settings (default = Primary), logged like every other call."""
import logging
import time

from backend.analyzer.llm_client import call_llm, resolve_llm_config
from backend.analyzer.llm_logger import log_llm_call
from backend.analyzer.model_json import parse_model_json

logger = logging.getLogger("jobnavigator.curator.llm")


async def ask(prompt: str, system: str, purpose: str, max_tokens: int = 1500, job_id=None) -> str:
    cfg = resolve_llm_config("curator")
    started = time.monotonic()
    ok, err, usage, text = True, None, {}, ""
    try:
        res = await call_llm(prompt, system, max_tokens=max_tokens, provider=cfg["provider"],
                             model=cfg["model"], api_key=cfg["api_key"], effort=cfg["effort"])
        text, usage = res.get("text", ""), res.get("usage") or {}
        return text
    except Exception as e:
        ok, err = False, str(e)
        raise
    finally:
        log_llm_call(purpose=purpose, provider=cfg["provider"], model=cfg["model"], usage=usage,
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
