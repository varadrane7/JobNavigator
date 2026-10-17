"""Subscription-CLI logins from the browser (MT-21): upload the login files instead of
running `docker compose exec … login` per account.

- GET    /credentials          {scope, codex: {logged_in}, agy: {logged_in}}
- PUT    /credentials/codex    body {auth_json: "<contents of ~/.codex/auth.json>"}
- PUT    /credentials/agy      body = a .zip of the `~/.gemini` folder of a machine where
                               `agy` is signed in (raw bytes, application/zip)
- DELETE /credentials/{codex|agy}

Where the files land follows `accounts.credentials`: the owner's go to the install's default
homes (~/.codex, ~/.gemini), a member on its own Models section gets its per-account homes,
and a member on 'inherit' is refused (it uses the owner's login; switch Models to Own
first). Nothing is ever read back: GET answers logged-in yes/no only. Claude Code needs no
file: its token is the Models setting.
"""
import io
import json
import logging
import os
import shutil
import stat
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.accounts import credentials as C
from backend.accounts import settings as acct_settings
from backend.models.db import get_db

logger = logging.getLogger("jobnavigator.credentials")

router = APIRouter(prefix="/credentials", tags=["credentials"])

CODEX_MAX_BYTES = 64 * 1024
AGY_MAX_ZIP_BYTES = 10 * 1024 * 1024
AGY_MAX_UNPACKED_BYTES = 40 * 1024 * 1024
AGY_MAX_FILES = 500
DEFAULT_CODEX_HOME = "~/.codex"
DEFAULT_AGY_HOME = "~"


def _targets(db) -> dict:
    """{codex_home, agy_home, scope} for the caller, or 403 for a member on 'inherit'."""
    member = acct_settings.member_account(db)
    if member is None:
        return {"scope": "install", "codex_home": os.path.expanduser(DEFAULT_CODEX_HOME),
                "agy_home": os.path.expanduser(DEFAULT_AGY_HOME)}
    if not C.uses_own_logins(db, member):
        raise HTTPException(status_code=403, detail="login_inherited")
    return {"scope": "own", "codex_home": C.codex_home(member), "agy_home": C.agy_home(member)}


def _agy_state(home: str) -> str:
    return os.path.join(home, C.AGY_STATE_SUBDIR)


def _codex_logged_in(home: str) -> bool:
    return os.path.isfile(os.path.join(home, "auth.json"))


def _agy_logged_in(home: str) -> bool:
    state = _agy_state(home)
    return os.path.isdir(state) and any(os.scandir(state))


def _private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


@router.get("")
def status(db: Session = Depends(get_db)):
    member = acct_settings.member_account(db)
    if member is not None and not C.uses_own_logins(db, member):
        return {"scope": "inherit", "codex": {"logged_in": False}, "agy": {"logged_in": False}}
    t = _targets(db)
    return {"scope": t["scope"], "codex": {"logged_in": _codex_logged_in(t["codex_home"])},
            "agy": {"logged_in": _agy_logged_in(t["agy_home"])}}


@router.put("/codex")
def put_codex(body: dict, db: Session = Depends(get_db)):
    t = _targets(db)
    raw = (body or {}).get("auth_json")
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422, detail="auth_json is required")
    if len(raw.encode()) > CODEX_MAX_BYTES:
        raise HTTPException(status_code=413, detail="auth.json is too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="auth_json is not valid JSON")
    # a ChatGPT login carries `tokens`; a bare API key is not a subscription login
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
        raise HTTPException(status_code=422, detail="not a Codex ChatGPT login (no tokens): run `codex login` and upload ~/.codex/auth.json")
    _private_dir(t["codex_home"])
    path = os.path.join(t["codex_home"], "auth.json")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data))
    os.replace(tmp, path)
    logger.info("codex login stored (%s)", t["scope"])
    return {"ok": True, "logged_in": True}


def _safe_members(zf: zipfile.ZipFile) -> list:
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > AGY_MAX_FILES or sum(i.file_size for i in infos) > AGY_MAX_UNPACKED_BYTES:
        raise HTTPException(status_code=413, detail="archive is too large")
    names = []
    for i in infos:
        name = i.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if (name.startswith("/") or ".." in parts or not parts
                or stat.S_ISLNK(i.external_attr >> 16) or (i.flag_bits & 0x1)):
            raise HTTPException(status_code=422, detail=f"unsafe path in archive: {i.filename!r}")
        names.append((i, parts))
    return names


@router.put("/agy")
async def put_agy(request: Request, db: Session = Depends(get_db)):
    t = _targets(db)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > AGY_MAX_ZIP_BYTES:
        raise HTTPException(status_code=413, detail="archive is too large")
    data = await request.body()
    if len(data) > AGY_MAX_ZIP_BYTES:
        raise HTTPException(status_code=413, detail="archive is too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="not a zip archive")
    with zf:
        members = _safe_members(zf)
        if not members:
            raise HTTPException(status_code=422, detail="the archive is empty")
        # the archive is the `.gemini` folder itself or its parent
        rel = [parts[1:] if parts[0] == ".gemini" else parts for _, parts in members]
        root = os.path.join(t["agy_home"], ".gemini")
        fresh = root + ".incoming"
        shutil.rmtree(fresh, ignore_errors=True)
        _private_dir(t["agy_home"])
        _private_dir(fresh)
        base = os.path.realpath(fresh)
        for (info, _), parts in zip(members, rel):
            if not parts:
                continue
            dest = os.path.realpath(os.path.join(base, *parts))
            if not dest.startswith(base + os.sep):
                shutil.rmtree(fresh, ignore_errors=True)
                raise HTTPException(status_code=422, detail=f"unsafe path in archive: {info.filename!r}")
            os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            with zf.open(info) as src, open(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
                shutil.copyfileobj(src, out)
    # merge, not replace: for the owner `.gemini` is a mounted volume holding other state too
    try:
        shutil.copytree(fresh, root, dirs_exist_ok=True)
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
    logger.info("agy login stored (%s)", t["scope"])
    return {"ok": True, "logged_in": _agy_logged_in(t["agy_home"])}


@router.delete("/{provider}")
def delete_login(provider: str, db: Session = Depends(get_db)):
    if provider not in ("codex", "agy"):
        raise HTTPException(status_code=404, detail="unknown provider")
    t = _targets(db)
    if provider == "codex":
        try:
            os.remove(os.path.join(t["codex_home"], "auth.json"))
        except FileNotFoundError:
            pass
    else:
        shutil.rmtree(_agy_state(t["agy_home"]), ignore_errors=True)
    return {"ok": True}
