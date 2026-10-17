"""A member's own local model server (MT-21).

Ollama / LM Studio / OpenAI (Local) addresses are install settings (owner-only): one host
for everyone, because a backend that fetches a URL a user typed is a request-forgery tool.
The owner can lift that per member (`local_url_allowed_accounts`, a JSON list of account
ids, toggled in Settings › Accounts); an allowed member then sets `member_<provider>_base_url`
(own settings, Settings › Models) and its own calls go there instead of the owner's server.

The address must be http(s), carry no credentials and resolve only to loopback or private
(LAN / VPN / Docker) addresses; link-local (cloud metadata 169.254.169.254), unspecified,
multicast and public addresses are refused. The check runs when it is saved and again before
every call (so a DNS name that later points elsewhere is caught).
"""
import ipaddress
import json
import socket
from typing import Optional
from urllib.parse import urlsplit

ALLOWED_KEY = "local_url_allowed_accounts"
MEMBER_KEYS = {"lmstudio": "member_lmstudio_base_url",
               "ollama": "member_ollama_base_url",
               "openai_local": "member_llm_local_base_url"}


def allowed_ids(db) -> set:
    from backend.accounts.settings import get_install_setting
    try:
        raw = json.loads(get_install_setting(db, ALLOWED_KEY, "") or "[]")
    except ValueError:
        return set()
    return {str(x) for x in raw} if isinstance(raw, list) else set()


def is_allowed(db, account_id) -> bool:
    return str(account_id) in allowed_ids(db)


def set_allowed(db, account_id, on: bool) -> None:
    """Add/remove an account in the allow list. No commit."""
    from backend.accounts.settings import put_setting
    ids = allowed_ids(db)
    (ids.add if on else ids.discard)(str(account_id))
    put_setting(db, ALLOWED_KEY, json.dumps(sorted(ids)), account_id=None)


def check_url(url: str) -> Optional[str]:
    """None when `url` may be used as a member's local server, else why not."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "must be an http:// or https:// address"
    if parts.username or parts.password:
        return "must not contain credentials"
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError:
        return "host does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if ip.is_loopback:
            continue
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            return "must not point at a link-local, unspecified or reserved address"
        if not ip.is_private:
            return "must be a local or private-network address (public hosts are not allowed)"
    return None


def member_override(db, provider: str) -> Optional[str]:
    """The URL the current member has set for `provider`, when it is allowed to; None =
    use the install's server. Raises NonRetryableLLMError for a stored URL that no longer
    passes the check."""
    from backend.accounts.settings import get_setting, member_account
    member = member_account(db)
    key = MEMBER_KEYS.get(provider)
    if member is None or key is None or not is_allowed(db, member):
        return None
    value = (get_setting(db, key) or "").strip()
    if not value:
        return None
    problem = check_url(value)
    if problem:
        from backend.analyzer.llm_client import NonRetryableLLMError
        raise NonRetryableLLMError(f"Your local model server address {problem} (Settings › Models)")
    return value.rstrip("/")
