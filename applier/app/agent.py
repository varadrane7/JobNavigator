"""Run Claude Code headless against the shared Chromium and turn its stream into steps + a result."""
import asyncio
import json
import logging
import os
import re
import time

logger = logging.getLogger("applier.agent")

# Everything except the browser is off: the agent must not read the container, run commands or browse
# the web by other means. browser_install would try to download a browser we already run; run_code
# executes Node-side Playwright code, which a hostile page could talk the agent into using to read env.
DENY = ["Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
        "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "mcp__playwright__browser_install",
        "mcp__playwright__browser_run_code", "mcp__playwright__browser_run_code_unsafe"]

RESULT_RE = re.compile(r"^\s*RESULT:\s*(\{.*\})\s*$", re.M)
SUBMIT_WORD = re.compile(r"\b(submit|send application|apply)\b", re.I)


def mcp_config(workdir: str) -> dict:
    return {"mcpServers": {"playwright": {
        "command": "playwright-mcp",
        "args": ["--cdp-endpoint=http://127.0.0.1:9222", f"--output-dir={workdir}/out",
                 "--viewport-size=1366x900", "--timeout-action=10000", "--image-responses=omit"],
    }}}


def command(model: str, workdir: str, max_turns: int) -> list[str]:
    path = os.path.join(workdir, ".mcp.json")
    with open(path, "w") as f:
        json.dump(mcp_config(workdir), f)
    cmd = ["claude", "-p", "--mcp-config", path, "--strict-mcp-config",
           "--allowedTools", "mcp__playwright", "--disallowedTools", ",".join(DENY),
           "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--max-turns", str(max_turns)]
    if model:
        cmd += ["--model", model]
    return cmd


def describe(block: dict) -> str:
    """One readable line per browser action, for the step log the UI shows."""
    name = (block.get("name") or "").replace("mcp__playwright__browser_", "")
    inp = block.get("input") or {}
    if "url" in inp:
        return f"{name} {inp['url']}"
    if name == "type" or name == "fill_form":
        fields = inp.get("fields")
        if fields:
            return f"fill {len(fields)} field(s): " + ", ".join(str(f.get('name') or f.get('element') or '')[:30] for f in fields)
        return f"type into {inp.get('element', '')[:60]}"
    if name == "file_upload":
        return "upload " + ", ".join(os.path.basename(p) for p in inp.get("paths") or [])
    if "element" in inp:
        extra = f" = {inp['values']}" if inp.get("values") else ""
        return f"{name} {inp['element'][:80]}{extra}"[:160]
    return name


def parse_result(text: str) -> dict | None:
    found = RESULT_RE.findall(text or "")
    for raw in reversed(found):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    return None


async def run(run, prompt: str, model: str, timeout_s: int, max_turns: int) -> None:
    """Fills run.steps / run.transcript / run.usage while it goes; sets run.result at the end."""
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    proc = await asyncio.create_subprocess_exec(
        *command(model, run.workdir, max_turns), cwd=run.workdir, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        limit=32 * 2**20)
    run.proc = proc
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    texts: list[str] = []

    async def read():
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                run.transcript.append(line[:2000])
                continue
            kind = msg.get("type")
            if kind == "assistant":
                for b in (msg.get("message") or {}).get("content") or []:
                    if b.get("type") == "text" and b.get("text"):
                        texts.append(b["text"])
                        run.transcript.append(b["text"][:4000])
                    elif b.get("type") == "tool_use":
                        step = describe(b)
                        run.steps.append(step)
                        if b.get("name", "").endswith("browser_click") and SUBMIT_WORD.search(step):
                            run.clicked_submit_like = True
            elif kind == "result":
                run.usage = {"turns": msg.get("num_turns"), "cost_usd": msg.get("total_cost_usd"),
                             "is_error": msg.get("is_error"), "subtype": msg.get("subtype"),
                             **{k: v for k, v in (msg.get("usage") or {}).items() if isinstance(v, int)}}
                if msg.get("result"):
                    texts.append(msg["result"])

    start = time.monotonic()
    try:
        await asyncio.wait_for(read(), timeout=timeout_s)
        await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        run.result = {"status": "failed", "reason": f"timed out after {int(time.monotonic() - start)}s",
                      "submit_clicked": run.clicked_submit_like}
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if run.result:
        return
    out = "\n".join(texts)
    res = parse_result(out)
    if res is None:
        tail = (texts[-1] if texts else "no output")[-300:]
        # Usage limits and auth errors come back as plain text from the CLI.
        if re.search(r"usage limit|rate limit|limit reached|resets? at", out, re.I):
            res = {"status": "failed", "reason": f"Claude usage limit: {tail}", "quota": True}
        else:
            res = {"status": "failed", "reason": f"agent gave no RESULT line: {tail}"}
    res.setdefault("submit_clicked", run.clicked_submit_like)
    run.result = res
