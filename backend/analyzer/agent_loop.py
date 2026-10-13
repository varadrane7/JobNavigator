"""A small tool-calling loop over an OpenAI-compatible endpoint (LM Studio by default).

The curator mostly runs fixed searches and asks the model to extract JSON, which a
4B local model does reliably. This loop is for the open-ended cases: the model gets
web_search/fetch_page and a step budget, and must end with a plain-text answer.
"""
import json
import logging

from backend.analyzer.llm_client import _get_setting, _openai_client
from backend.models.db import SessionLocal
from backend.search.tools import TOOLS, run_tool

logger = logging.getLogger("jobnavigator.agent")


def _local_config() -> tuple[str, str]:
    db = SessionLocal()
    try:
        base = _get_setting(db, "llm_local_base_url", "http://localhost:1234/v1")
        model = (_get_setting(db, "curator_llm_model", "")
                 or _get_setting(db, "llm_model", "") or "local-model")
        return base, model
    finally:
        db.close()


async def run_tool_agent(system: str, user: str, tools: list | None = None,
                         max_steps: int = 6, max_tokens: int = 2000) -> str:
    """Run until the model answers without a tool call or the budget is spent.
    Returns the final text ('' when the budget ran out without an answer)."""
    base_url, model = _local_config()
    client = _openai_client("lm-studio", base_url)
    tools = tools or TOOLS
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for step in range(max_steps):
        last = step == max_steps - 1
        resp = await client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens,
            **({} if last else {"tools": tools}),
        )
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            return (msg.content or "").strip()
        messages.append({
            "role": "assistant", "content": msg.content or "",
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments}}
                           for c in calls],
        })
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except ValueError:
                args = {}
            out = await run_tool(c.function.name, args)
            logger.info(f"agent step {step}: {c.function.name}({args}) -> {len(out)} chars")
            messages.append({"role": "tool", "tool_call_id": c.id, "content": out[:6000]})
    return ""
