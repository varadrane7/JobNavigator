"""`/mcp`: the MCP endpoint (Streamable HTTP, stateless), and `/api/connector` for Settings.

Stateless means one POST, one JSON answer: no session id, no server-to-client stream (GET is
405). That is all a tools-only server needs and what hosted clients expect.

`/mcp` is outside the dashboard's auth (main._PUBLIC_PREFIXES): it accepts only a connector
bearer token (connector/tokens.py), from OAuth or made in Settings, and runs each tool as that
token's account. Without one it answers 401 and points at the OAuth metadata, which is how a
client learns to start the sign-in.
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from backend.accounts import auth as acct_auth
from backend.accounts.context import current_account, use_account
from backend.connector import oauth, tokens, tools
from backend.models.db import SessionLocal, get_db
from backend.version import APP_VERSION

logger = logging.getLogger("jobnavigator.connector")
router = APIRouter(tags=["connector"])

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024

_PARSE, _INVALID, _NO_METHOD, _BAD_PARAMS, _INTERNAL = -32700, -32600, -32601, -32602, -32603


def _unauthorized(request: Request, description: str = "") -> JSONResponse:
    challenge = f'Bearer resource_metadata="{oauth.resource_metadata_url(request)}"'
    if description:
        challenge += f', error="invalid_token", error_description="{description}"'
    return JSONResponse({"error": "unauthorized", "detail": description or "A connector token is required"},
                        status_code=401, headers={"WWW-Authenticate": challenge})


def _bearer(request: Request) -> str:
    scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _handle(db, message, can_write: bool) -> Optional[dict]:
    """The response to one JSON-RPC message; None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, _INVALID, "Not a JSON-RPC 2.0 message")
    method, msg_id = message.get("method"), message.get("id")
    if "method" not in message or "id" not in message:
        return None                      # a notification, or a response to something we never asked
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "jobnavigator", "title": "JobNavigator", "version": APP_VERSION},
            "instructions": tools.INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools.listing(can_write)}}
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _error(msg_id, _BAD_PARAMS, "params.name is required")
        try:
            text, failed = tools.call(db, name, params.get("arguments"), can_write), False
        except tools.ToolError as exc:
            db.rollback()
            text, failed = str(exc), True
        except Exception:
            db.rollback()
            logger.exception("connector tool %s failed", name)
            text, failed = f"{name} failed inside JobNavigator; the error is in the backend log", True
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}],
                                                         "isError": failed}}
    return _error(msg_id, _NO_METHOD, f"Method not found: {method}")


def _serve(token: str, body):
    """(account found?, JSON-RPC answer or None). Runs in the threadpool."""
    db = SessionLocal()
    try:
        payload = tokens.read(db, tokens.KIND_ACCESS, token)
        account = tokens.account_of(db, payload)
        if account is None:
            return False, None
        acct_auth.touch_last_active(db, account["id"])
        can_write = payload.get("s") == "write"
        with use_account(account["id"]):
            if isinstance(body, list):
                if not body:
                    return True, _error(None, _INVALID, "Empty batch")
                answers = [a for a in (_handle(db, m, can_write) for m in body[:50]) if a is not None]
                return True, answers or None
            return True, _handle(db, body, can_write)
    finally:
        db.close()


@router.post("/mcp", include_in_schema=False)
async def mcp(request: Request):
    token = _bearer(request)
    if not token:
        return _unauthorized(request)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return JSONResponse(_error(None, _INVALID, "Request too large"), status_code=413)
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    known, answer = await run_in_threadpool(_serve, token, body)
    if not known:
        return _unauthorized(request, "The token is expired or was revoked")
    if body is None:
        return JSONResponse(_error(None, _PARSE, "Body is not JSON"), status_code=400)
    if answer is None:
        return Response(status_code=202)
    return JSONResponse(answer)


@router.get("/mcp", include_in_schema=False)
@router.delete("/mcp", include_in_schema=False)
async def mcp_no_stream(request: Request):
    if not _bearer(request):
        return _unauthorized(request)
    return Response(status_code=405, headers={"Allow": "POST"})


# ── Settings API ─────────────────────────────────────────────────────────────

def _me(db) -> dict:
    accounts = acct_auth.cached_accounts(db)
    account = acct_auth._find(accounts, current_account.get())
    if account is None and len(accounts) == 1:
        account = accounts[0]
    if account is None:
        raise HTTPException(status_code=401, detail=acct_auth.ACCOUNT_REQUIRED)
    return account


@router.get("/api/connector", summary="Where MCP clients connect, and the tools they get")
def connector_info(request: Request, db: Session = Depends(get_db)):
    """The connector URL to paste into an MCP client, how many times this account revoked its connections, and the tool list."""
    account = _me(db)
    return {"url": oauth.resource_url(request), "revoked": tokens.epoch(db, account["id"]),
            "tools": [{"name": t.name, "title": t.title, "write": t.write, "description": t.description}
                      for t in tools.TOOLS]}


@router.post("/api/connector/token", summary="Make a connector token for an agent or script")
def connector_token(body: dict, request: Request, db: Session = Depends(get_db)):
    """Body `{scope: "read" | "write", label}`. Returns `{token, url, scope, expires_in}`: a bearer token for the caller's own account, valid a year, shown once. For clients that cannot sign in with OAuth (Claude Code, other agents); it opens the connector tools only."""
    account = _me(db)
    scope = (body or {}).get("scope") or "read"
    if scope not in tokens.SCOPES:
        raise HTTPException(status_code=422, detail="scope must be read or write")
    label = str((body or {}).get("label") or "token")[:80]
    logger.info("connector: %s made a %s token (%s)", account.get("name"), scope, label)
    return {"token": tokens.make_access(db, account, scope, label, ttl=tokens.STATIC_SECONDS),
            "url": oauth.resource_url(request), "scope": scope, "expires_in": tokens.STATIC_SECONDS}


@router.post("/api/connector/revoke", summary="Disconnect every MCP client of this account")
def connector_revoke(db: Session = Depends(get_db)):
    """Every connector token of the caller's account stops working at once: OAuth connections (claude.ai) and tokens made in Settings. Clients must connect again."""
    account = _me(db)
    return {"revoked": tokens.revoke_all(db, account["id"])}
