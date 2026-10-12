"""Tiny stdio MCP server giving the agent one extra tool, draft_answer, backed by the dashboard's own
autofill LLM (the one used by the extension's in-field draft). Free-text answers then cost the agent a
tool call instead of its own generation, and use whatever model/quota the dashboard is configured for.

Env: JN_URL (dashboard base URL), JN_KEY (X-API-Key), JN_COMPANY, JN_TITLE."""
import json
import os
import sys
import urllib.request

TOOL = {
    "name": "draft_answer",
    "description": ("Write a truthful first-person answer to ONE free-text application question, grounded in the "
                    "applicant's profile. Pass the question text exactly as shown. Returns the answer text to type "
                    "into the field."),
    "inputSchema": {"type": "object", "properties": {
        "question": {"type": "string"},
        "max_chars": {"type": "integer", "description": "Character limit of the field, if it shows one"}},
        "required": ["question"]},
}


def draft(question: str, max_chars=None) -> str:
    body = {"question": question, "company": os.environ.get("JN_COMPANY", ""),
            "position": os.environ.get("JN_TITLE", "")}
    if max_chars:
        body["max_chars"] = int(max_chars)
    req = urllib.request.Request(os.environ["JN_URL"].rstrip("/") + "/api/autofill/answer",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json",
                                          "X-API-Key": os.environ.get("JN_KEY", "")})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read()).get("answer", "")


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "jn", "version": "1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}}
    if method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        try:
            text, err = draft(args.get("question", ""), args.get("max_chars")), False
        except Exception as e:
            text, err = f"draft failed ({e}); write the answer yourself from the resume", True
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": err}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return None


if __name__ == "__main__":
    for line in sys.stdin:
        try:
            out = handle(json.loads(line))
        except Exception:
            continue
        if out:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
