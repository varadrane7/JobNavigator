"""Add hiring YC startups to the Companies list, each with its own job board.

The yc-oss mirror of the YC directory lists every company YC marks as hiring
(https://yc-oss.github.io/api/companies/hiring.json). A company qualifies when it is active,
operates in the US (or remote), has 15-500 people and works in a field that fits the user
(`DEFAULT_TAGS`). Its board is found by asking Ashby, Greenhouse and Lever's public APIs for its
slug (and a few variants of its name); one with at least one opening becomes a Companies row with
that board URL and a title filter for engineering roles. Companies already on the list are
skipped. New rows start inactive unless `activate` is set, so they can be reviewed first.

Run: POST /api/companies/import-yc, or
`docker compose exec backend python -m backend.scraper.yc_import --account <name> [--activate] [--limit 40]`.
"""
import asyncio
import logging
import re

import httpx

logger = logging.getLogger("jobnavigator.yc_import")

HIRING_URL = "https://yc-oss.github.io/api/companies/hiring.json"
DEFAULT_TAGS = {"artificial intelligence", "ai", "generative ai", "developer tools", "fintech", "infrastructure",
                "b2b", "saas", "devops", "security", "health tech", "healthcare", "machine learning",
                "data engineering", "api", "cloud computing", "open source", "enterprise software"}
TITLE_EXPR = "(engineer OR developer) AND NOT (senior OR staff OR principal OR lead OR manager)"
_US = ("united states of america", "america / canada", "remote", "fully remote")


def qualifies(c: dict, tags: set = DEFAULT_TAGS) -> bool:
    if c.get("status") != "Active" or not c.get("isHiring"):
        return False
    size = c.get("team_size") or 0
    if not 15 <= size <= 500:
        return False
    regions = " ".join(c.get("regions") or []).lower()
    if not any(r in regions for r in _US):
        return False
    ctags = {t.lower() for t in (c.get("tags") or []) + (c.get("industries") or [])}
    return bool(ctags & tags)


def slugs(c: dict) -> list[str]:
    name = re.sub(r"[^a-z0-9 ]", "", (c.get("name") or "").lower())
    out = [c.get("slug") or "", name.replace(" ", ""), name.replace(" ", "-")]
    site = re.sub(r"^https?://(www\.)?", "", c.get("website") or "").split("/")[0].split(".")[0]
    out.append(site)
    seen, res = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            res.append(s)
    return res


async def find_board(client: httpx.AsyncClient, c: dict) -> tuple[str, int] | None:
    """(board URL, openings) of the company's Ashby / Greenhouse / Lever board, or None."""
    for slug in slugs(c):
        try:
            r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            if r.status_code == 200 and (r.json().get("jobs") or []):
                return f"https://jobs.ashbyhq.com/{slug}", len(r.json()["jobs"])
            r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            if r.status_code == 200 and (r.json().get("jobs") or []):
                return f"https://job-boards.greenhouse.io/{slug}", len(r.json()["jobs"])
            r = await client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
            if r.status_code == 200 and isinstance(r.json(), list) and r.json():
                return f"https://jobs.lever.co/{slug}", len(r.json())
        except (httpx.HTTPError, ValueError):
            continue
    return None


async def import_yc(limit: int = 40, activate: bool = False, tags: set = DEFAULT_TAGS) -> dict:
    """Add up to `limit` qualifying YC companies with a live board to the current account's list."""
    from backend.models.db import Company, SessionLocal
    db = SessionLocal()
    try:
        have = {(n or "").strip().lower() for (n,) in db.query(Company.name).all()}
    finally:
        db.close()
    added, checked, no_board = [], 0, 0
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "JobNavigator"}) as client:
        r = await client.get(HIRING_URL)
        r.raise_for_status()
        cands = [c for c in r.json() if qualifies(c, tags) and (c.get("name") or "").strip().lower() not in have]
        # bigger teams first: they run a public ATS board and are likelier to sponsor an H-1B
        cands.sort(key=lambda c: -(c.get("team_size") or 0))
        sem = asyncio.Semaphore(6)

        async def probe(c):
            async with sem:
                return c, await find_board(client, c)
        for i in range(0, len(cands), 30):
            if len(added) >= limit:
                break
            for c, board in await asyncio.gather(*(probe(c) for c in cands[i:i + 30])):
                checked += 1
                # a slug guess can land on another company's board: more openings than people is one
                if board and board[1] > (c.get("team_size") or 0) + 10:
                    board = None
                if not board:
                    no_board += 1
                    continue
                if len(added) < limit:
                    added.append((c, board))
    db = SessionLocal()
    try:
        for c, (url, n) in added:
            db.add(Company(name=c["name"].strip(), active=activate, scrape_urls=[url], tier=3,
                           title_include_expr=TITLE_EXPR, playwright_enabled=False,
                           notes=f"YC {c.get('batch') or ''} · {c.get('one_liner') or ''} · {c.get('team_size')} people · "
                                 f"{n} openings when added · {c.get('website') or ''}".strip()))
        db.commit()
    finally:
        db.close()
    out = {"qualified": len(cands), "checked": checked, "no_board": no_board,
           "added": [{"name": c["name"], "board": url, "openings": n} for c, (url, n) in added]}
    logger.info(f"YC import: {len(added)} added of {len(cands)} qualifying ({no_board} without a public board)")
    return out


def main(argv=None) -> int:
    import argparse
    from backend.accounts.background import acting_as
    from backend.refresh_linkedin_session import resolve_account
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--account", default=None, help="account name or id (default: owner)")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--activate", action="store_true")
    a = p.parse_args(argv)
    acct = resolve_account(a.account) if a.account else None
    if a.account and acct is None:
        print(f"no single account matches {a.account!r}")
        return 1
    with acting_as(acct[0] if acct else None):
        out = asyncio.run(import_yc(a.limit, a.activate))
    for x in out["added"]:
        print(f"+ {x['name']:30} {x['openings']:3} openings  {x['board']}")
    print(f"{len(out['added'])} added · {out['qualified']} qualifying · {out['no_board']} without a public board")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
