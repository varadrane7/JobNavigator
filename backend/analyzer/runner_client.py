"""HTTP transport to optional CLI runners. Account policy stays in llm_client.

A runner takes RUNNER_SLOTS calls at a time and refuses the next one at once ("Runner is
busy"), never queueing it. Scoring, tailoring and the curator together send more than that
(a curator run scores 150 jobs in one burst), so the queue is here: a call waits for one of
this process's slots for its provider before it is sent, and a call the runner still refuses
(another process holds a slot, or the manager is still starting a runner it had stopped for
being idle) is sent again after a short pause. A refused call never ran,
so sending it again cannot spend subscription quota twice.
"""
import asyncio
import os
import random
import weakref

import httpx

from backend.cli_runner.auth import ensure_token
from backend.cli_runner.protocol import (
    AGY_TIMEOUT, CLI_TIMEOUT, PROTOCOL_VERSION, SERVICES, CLITimeoutError,
    NonRetryableLLMError,
)


RUNNER_SLOTS = 2             # cli_runner/server.py: calls a runner executes at once
BUSY_WAIT_SECONDS = 1800     # how long a call keeps asking a runner that says it is busy
READY_WAIT_SECONDS = 180     # how long it waits for a runner the manager is still starting
BUSY_PAUSE = (2.0, 5.0)      # pause between two tries, seconds (random within the range)
# Refusals that mean "not yet": the call did not run. The manager stops an idle runner and
# starts it on the next call, answering "not ready" until it is up.
_WAITS = (("Runner is busy", "BUSY_WAIT_SECONDS"), ("Runner is not ready", "READY_WAIT_SECONDS"))

# One semaphore per provider and event loop: a semaphore belongs to the loop it first waits in,
# and the scheduler's threads run loops of their own.
_slots: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _slot(provider) -> asyncio.Semaphore:
    per_loop = _slots.setdefault(asyncio.get_running_loop(), {})
    if provider not in per_loop:
        per_loop[provider] = asyncio.Semaphore(RUNNER_SLOTS)
    return per_loop[provider]


async def complete(provider, prompt, system, model, max_tokens, *, account_id=None,
                   claude_token="", effort=""):
    """One completion on `provider`'s runner, waiting for a free slot instead of failing when
    the runner is busy. Raises the runner's own error for everything else."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with _slot(provider):
        while True:
            try:
                return await _complete_once(provider, prompt, system, model, max_tokens,
                                            account_id=account_id, claude_token=claude_token, effort=effort)
            except NonRetryableLLMError as exc:
                wait = next((globals()[name] for prefix, name in _WAITS if str(exc).startswith(prefix)), None)
                if wait is None or loop.time() - started >= wait:
                    raise
            await asyncio.sleep(random.uniform(*BUSY_PAUSE))


async def _complete_once(provider, prompt, system, model, max_tokens, *, account_id=None,
                         claude_token="", effort=""):
    from backend.runner_management import enabled, manager_url
    service = SERVICES[provider]
    env_name = "JN_" + service.upper().replace("-", "_") + "_URL"
    base = os.getenv(env_name, f"http://{service}:8000").rstrip("/")
    if enabled():
        base = manager_url()
    payload = {"protocol": PROTOCOL_VERSION, "provider": provider, "prompt": prompt, "system": system,
               "model": model, "max_tokens": max_tokens, "effort": effort,
               "account_id": str(account_id) if account_id is not None else None,
               "claude_token": claude_token}
    # Codex performs a login-status check (up to 30s) before the completion.
    limit = AGY_TIMEOUT if provider == "antigravity_cli" else CLI_TIMEOUT + 30
    try:
        token = ensure_token()
    except (OSError, RuntimeError):
        raise NonRetryableLLMError("Cannot read or create the shared runner credential") from None
    try:
        # Never inherit a host HTTP proxy for this private service connection.
        async with httpx.AsyncClient(timeout=httpx.Timeout(limit + 10, connect=5), trust_env=False) as client:
            response = await client.post(f"{base}/v1/complete", json=payload,
                                         headers={"Authorization": f"Bearer {token}"})
    except httpx.ConnectError:
        if enabled():
            raise NonRetryableLLMError(
                "Runner manager is unavailable. Check provider status in Settings > Models and the runner-manager service.") from None
        raise NonRetryableLLMError(
            f"{service} is unavailable. Enable its Compose profile and run "
            f"`docker compose up -d {service}`.") from None
    except httpx.TransportError:
        # A lost response may already have consumed subscription quota. Don't replay it.
        raise NonRetryableLLMError(f"Lost connection to {service}; execution outcome is unknown") from None
    try:
        data = response.json()
        if not isinstance(data, dict) or data.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("protocol mismatch")
        if not response.is_success:
            error = data.get("error") or {}
            message = str(error.get("message") or f"{service} failed")
            kind = error.get("kind")
            if kind == "timeout":
                raise CLITimeoutError(message)
            if kind == "non_retryable":
                raise NonRetryableLLMError(message)
            if kind == "retryable":
                raise RuntimeError(message)
            raise ValueError("unknown error")
        result = data["result"]
        if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not isinstance(result.get("usage"), dict):
            raise ValueError("invalid result")
        return result
    except (ValueError, KeyError, TypeError, AttributeError):
        raise NonRetryableLLMError(
            f"{service} returned an incompatible response; rebuild/pull the backend and runner together") from None
