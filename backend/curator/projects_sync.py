"""Keep the persona's project list (personas.networking.projects) in step with the user's GitHub.

Every non-fork repository becomes a project: the curator model reads its README and writes what
it shows and who would care, so the lead scout can match a person to the project worth showing
them. An entry the user edited (`edited: true`) or added by hand (`source: manual`) is never
overwritten; a repository whose last push did not change is not read again. Tiny or personal
repositories (dotfiles, coursework, tutorials) are kept but hidden from outreach.

The blog's RSS feed (networking.blog_rss) adds the latest posts as `writing`.
"""
import logging
import re
from datetime import datetime, timezone

import httpx

from backend.accounts.settings import get_setting
from backend.models.db import SessionLocal

logger = logging.getLogger("jobnavigator.curator.projects")

GITHUB = "https://api.github.com"
_HIDE_NAMES = re.compile(r"^(dotfiles|learn[-_].*|advent[-_]of[-_]code|tutorial|.*[-_]autograder.*|cs\d{3}.*|"
                         r"activity[-_]generator|python[-_]scripts|drawio[-_]files|[a-z0-9-]+\.github\.io)$", re.I)

_SYSTEM = ("You summarise one software project for a job seeker's networking profile. Be concrete and "
           "factual: only what the README shows. No hype words.")
_PROMPT = """Repository: {name}
Description: {description}
Language: {language}
Stars: {stars}
README (truncated):
{readme}

Return JSON:
{{"one_liner": "<what it does, <= 20 words>",
  "stack": ["<main technologies, <= 6>"],
  "highlights": ["<up to 3 concrete engineering points a reviewer would notice>"],
  "who_cares": "<the kind of team or person who would find this relevant, <= 20 words>",
  "domains": ["<1-3 short domain tags, e.g. llm-infra, devtools, fintech, healthtech, cloud, homelab>"],
  "trivial": <true if this is a tutorial, coursework, config dump or toy with little to show>}}"""


def _headers(token: str | None, raw: bool = False) -> dict:
    h = {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "JobNavigator"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def list_repos(client: httpx.AsyncClient, user: str, token: str | None) -> list[dict]:
    """The user's own repositories (private ones too when the token can see them), forks excluded."""
    url = f"{GITHUB}/user/repos?affiliation=owner&per_page=100" if token else f"{GITHUB}/users/{user}/repos?type=owner&per_page=100"
    out = []
    while url:
        r = await client.get(url, headers=_headers(token))
        r.raise_for_status()
        out.extend(r.json())
        m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("link", ""))
        url = m.group(1) if m else None
    return [x for x in out if not x.get("fork")]


async def readme(client: httpx.AsyncClient, full_name: str, token: str | None) -> str:
    try:
        r = await client.get(f"{GITHUB}/repos/{full_name}/readme", headers=_headers(token, raw=True))
        return r.text[:8000] if r.status_code == 200 else ""
    except httpx.HTTPError:
        return ""


def merge_project(existing: dict | None, repo: dict, summary: dict | None) -> dict:
    """One project entry from a repository and its summary, keeping what the user wrote."""
    if existing and (existing.get("edited") or existing.get("source") == "manual"):
        return existing
    base = dict(existing or {})
    base.update({
        "name": repo["name"], "url": repo.get("html_url"), "source": "github",
        "private": bool(repo.get("private")), "stars": repo.get("stargazers_count") or 0,
        "language": repo.get("language"), "pushed_at": repo.get("pushed_at"),
        "homepage": repo.get("homepage") or None,
    })
    if summary:
        for k in ("one_liner", "stack", "highlights", "who_cares", "domains"):
            if summary.get(k):
                base[k] = summary[k]
    if not base.get("one_liner"):
        base["one_liner"] = repo.get("description") or ""
    base["hidden"] = bool(base.get("hidden_by_user")) or bool(_HIDE_NAMES.match(repo["name"])) \
        or bool(summary and summary.get("trivial"))
    return base


async def blog_posts(client: httpx.AsyncClient, rss: str, limit: int = 8) -> list[dict]:
    try:
        r = await client.get(rss, headers={"User-Agent": "JobNavigator"}, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        logger.info(f"blog feed {rss}: {e}")
        return []
    posts = []
    for item in re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", r.text, re.S)[:limit]:
        title = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        link = re.search(r"<link[^>]*?(?:href=\"([^\"]+)\"[^>]*/?>|>(.*?)</link>)", item, re.S)
        if title:
            posts.append({"title": title.group(1).strip(),
                          "url": (link.group(1) or link.group(2) or "").strip() if link else None})
    return posts


async def sync_projects(force: bool = False) -> dict:
    """Refresh the current account's persona projects from GitHub; {repos, read, kept, hidden}."""
    from sqlalchemy.orm.attributes import flag_modified
    from backend.accounts.owned import get_persona
    from backend.curator import llm
    db = SessionLocal()
    try:
        p = get_persona(db)
        net = dict(p.networking or {})
        user = (net.get("github_user") or "").strip()
        token = str(get_setting(db, "github_token") or "").strip() or None
        if not user and not token:
            return {"repos": 0, "read": 0, "kept": 0, "hidden": 0, "error": "no GitHub user in the networking profile"}
        by_name = {x.get("name"): x for x in net.get("projects") or [] if isinstance(x, dict)}
    finally:
        db.close()

    if llm._SEM is None:
        llm.reset_run()
    stats = {"repos": 0, "read": 0, "kept": 0, "hidden": 0}
    async with httpx.AsyncClient(timeout=30) as client:
        repos = await list_repos(client, user, token)
        stats["repos"] = len(repos)
        for repo in repos:
            old = by_name.get(repo["name"])
            if old and not force and (old.get("edited") or old.get("source") == "manual"
                                      or old.get("pushed_at") == repo.get("pushed_at")):
                stats["kept"] += 1
                continue
            summary = None
            if not _HIDE_NAMES.match(repo["name"]):
                text = await readme(client, repo["full_name"], token)
                if text or repo.get("description"):
                    summary = await llm.ask_json(_PROMPT.format(
                        name=repo["name"], description=repo.get("description") or "", language=repo.get("language") or "",
                        stars=repo.get("stargazers_count") or 0, readme=text or "(no README)"),
                        _SYSTEM, "persona_project_summary", max_tokens=600)
                    stats["read"] += 1
            by_name[repo["name"]] = merge_project(old, repo, summary)
        writing = await blog_posts(client, net["blog_rss"]) if net.get("blog_rss") else None

    # shown projects first, most recently pushed first; manual entries keep their place among them
    projects = sorted(by_name.values(), key=lambda x: str(x.get("pushed_at") or "9999"), reverse=True)
    projects = [x for x in projects if not x.get("hidden")] + [x for x in projects if x.get("hidden")]
    stats["hidden"] = sum(1 for x in projects if x.get("hidden"))
    db = SessionLocal()
    try:
        p = get_persona(db)
        net = dict(p.networking or {})
        net["projects"] = projects
        if writing is not None:
            net["writing"] = writing
        net["projects_synced_at"] = datetime.now(timezone.utc).isoformat()
        p.networking = net
        flag_modified(p, "networking")
        db.commit()
    finally:
        db.close()
    logger.info(f"persona projects synced: {stats}")
    return stats


def networking_brief(net: dict, limit: int = 12) -> str:
    """The networking profile as text for the scout's prompts: pitch, asks, affinities, projects."""
    if not net:
        return ""
    lines = []
    for key, label in (("pitch", "Pitch"), ("visa_note", "Work authorization"), ("asks", "Asks"),
                       ("target_roles", "Target roles"), ("target_domains", "Target domains"),
                       ("affinities", "Affinities"), ("talking_points", "Talking points")):
        v = net.get(key)
        if isinstance(v, list):
            v = "; ".join(str(x) for x in v if x)
        if v:
            lines.append(f"{label}: {v}")
    shown = [x for x in net.get("projects") or [] if isinstance(x, dict) and not x.get("hidden")][:limit]
    if shown:
        lines.append("Projects:")
        for x in shown:
            stack = ", ".join(x.get("stack") or [])[:80]
            lines.append(f"- {x.get('name')}: {x.get('one_liner') or ''}"
                         + (f" ({stack})" if stack else "") + (f" [{x.get('url')}]" if x.get("url") else ""))
    posts = net.get("writing") or []
    if posts:
        lines.append("Writing: " + "; ".join(f"{w.get('title')}" for w in posts[:4] if w.get("title")))
    return "\n".join(lines)
