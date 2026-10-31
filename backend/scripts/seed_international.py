"""Set up an international search lane: a linked profile of the owner that looks for jobs in
Germany, the Netherlands, Ireland and the UAE.

What it does (each step skips what is already there, so a second run only fills gaps):
- creates the profile as a linked profile of the owner (an OIDC login of the owner can switch to
  it), copying the owner's persona, Q&A bank, base résumés, settings, voices and excludes;
- sets its curator to the relocation lane: curate_countries DE/NL/IE/AE, 20 picks a day and fewer
  people, so the two profiles do not exhaust the shared LinkedIn budget (the LinkedIn section
  inherits the owner's sessions);
- writes a visa line into the persona's preferences (read by cover letters) when it has none;
- adds searches: JobSpy LinkedIn + Indeed per country (Bayt as well for the UAE), Arbeitnow and
  Berlin Startup Jobs;
- adds English-first employers known to hire from abroad, on boards the company scraper reads.
  They start PAUSED: review them on the Companies screen and resume the ones you want.

    python -m backend.scripts.seed_international                 # dry run: says what it would add
    python -m backend.scripts.seed_international --commit        # write
    python -m backend.scripts.seed_international --name "Varad International" --commit
"""
import argparse
import json
import logging
import sys
import uuid

from backend.accounts import admin
from backend.accounts import auth as acct_auth
from backend.accounts.context import unscoped
from backend.accounts.settings import put_setting
from backend.models.db import ACCOUNT_COLORS, Account, Company, Persona, Search, SessionLocal

logger = logging.getLogger("jobnavigator.seed_international")

DEFAULT_NAME = "Varad International"
COUNTRIES = ["DE", "NL", "IE", "AE"]
COPY_ITEMS = ["persona", "qa", "resumes", "settings", "voices", "excludes"]
SETTINGS = {
    "curate_countries": json.dumps(COUNTRIES),
    "curate_count": "20",
    "outreach_count": "8",
    "outreach_network_count": "4",
}
VISA_ROUTE = ("MS in Computer Science (NJIT, May 2026). Eligible for an EU Blue Card at the reduced "
              "threshold (recent graduate, IT shortage occupation); no German needed for the visa. "
              "Can apply for the work visa from the US, where I live on F-1 OPT.")
TITLE_EXCLUDE = ["intern", "junior", "associate", "working student", "werkstudent", "praktikum",
                 "praktikant", "ausbildung", "trainee"]

# (name, country alias for JobSpy, location, term, boards)
SEARCHES = [
    ("DE · backend (LinkedIn, Indeed)", "germany", "", "backend engineer", ["linkedin", "indeed"]),
    ("DE · platform / cloud (LinkedIn, Indeed)", "germany", "", "platform engineer", ["linkedin", "indeed"]),
    ("NL · backend (LinkedIn, Indeed)", "netherlands", "", "backend engineer", ["linkedin", "indeed"]),
    ("IE · backend (LinkedIn, Indeed)", "ireland", "", "backend engineer", ["linkedin", "indeed"]),
    ("UAE · backend (LinkedIn, Indeed, Bayt)", "united arab emirates", "Dubai", "backend engineer",
     ["linkedin", "indeed", "bayt"]),
]
FEEDS = [
    ("DE · Arbeitnow", "arbeitnow", "backend, platform, python, golang, cloud, devops, software engineer"),
    ("DE · Berlin Startup Jobs", "berlinstartupjobs", ""),
]

# Boards checked to list engineering jobs in the lane's countries (Oct 2026). Big boards list every
# office: the company scrape keeps only the postings in `curate_countries` (market_filter).
_GH, _ASHBY = "https://job-boards.greenhouse.io/{}", "https://jobs.ashbyhq.com/{}"
_AMAZON = ("https://www.amazon.jobs/en/search?normalized_country_code%5B%5D=DEU&normalized_country_code%5B%5D=IRL"
           "&normalized_country_code%5B%5D=NLD&normalized_country_code%5B%5D=ARE&category%5B%5D=software-development")
TITLE_EXPR = "engineer OR developer OR devops"
COMPANIES = [
    # Germany
    ("N26", _GH.format("n26")), ("HelloFresh", _GH.format("hellofresh")), ("Celonis", _GH.format("celonis")),
    ("SumUp", _GH.format("sumup")), ("GetYourGuide", _GH.format("getyourguide")),
    ("Contentful", _GH.format("contentful")), ("Raisin", _GH.format("raisin")), ("Flix", _GH.format("flix")),
    ("Solaris", _GH.format("solarisbank")), ("commercetools", _GH.format("commercetools")),
    ("Staffbase", _GH.format("staffbase")), ("Trade Republic", _GH.format("traderepublicbank")),
    ("Enpal", _ASHBY.format("enpal")), ("Parloa", _ASHBY.format("parloa")), ("n8n", _ASHBY.format("n8n")),
    ("DeepL", _ASHBY.format("deepl")), ("trivago", _GH.format("trivago")),
    ("Helsing", _GH.format("helsing")), ("Isar Aerospace", _GH.format("isaraerospace")),
    ("Doctolib", _GH.format("doctolib")), ("Wolt", _GH.format("wolt")), ("Babbel", _ASHBY.format("babbel")),
    ("Black Forest Labs", _ASHBY.format("black-forest-labs")), ("Upvest", _ASHBY.format("upvest")),
    ("Grover", _GH.format("grover")), ("SoundCloud", _GH.format("soundcloud71")), ("Tide", _GH.format("tide")),
    ("Aleph Alpha", _ASHBY.format("alephalpha")), ("Pliant", _ASHBY.format("pliant")),
    # Netherlands
    ("Adyen", _GH.format("adyen")), ("Mollie", _ASHBY.format("mollie")), ("Miro", _ASHBY.format("miro")),
    ("Elastic", _GH.format("elastic")), ("Nebius", _GH.format("nebius")), ("IMC Trading", _GH.format("imc")),
    ("Flow Traders", _GH.format("flowtraders")), ("JetBrains", _GH.format("jetbrains")),
    ("Catawiki", _GH.format("catawiki")), ("ClickHouse", _ASHBY.format("clickhouse")),
    ("Databricks", _GH.format("databricks")),
    # Ireland
    ("Stripe", _GH.format("stripe")), ("Intercom", _GH.format("intercom")), ("Wayflyer", _ASHBY.format("wayflyer")),
    ("Workhuman", _ASHBY.format("workhuman")), ("Toast", _GH.format("toast")), ("Squarespace", _GH.format("squarespace")),
    ("Datadog", _GH.format("datadog")), ("MongoDB", _GH.format("mongodb")), ("Pleo", _ASHBY.format("pleo")),
    ("Twilio", _GH.format("twilio")), ("Tines", _GH.format("tines")), ("Okta", _GH.format("okta")),
    ("Workato", _GH.format("workato")), ("Vanta", _ASHBY.format("vanta")), ("Samsara", _GH.format("samsara")),
    # UAE
    ("Careem", _GH.format("careem")), ("Tamara", _GH.format("tamara")), ("Ziina", _ASHBY.format("ziina")),
    # Several of the lane's countries
    ("Amazon", _AMAZON), ("OpenAI", _ASHBY.format("openai")), ("Anthropic", _GH.format("anthropic")),
    ("ElevenLabs", _ASHBY.format("elevenlabs")), ("Cohere", _ASHBY.format("cohere")),
    ("Legora", _ASHBY.format("legora")), ("Synthesia", _ASHBY.format("synthesia")),
    # Remote-first, hire across the EU
    ("GitLab", _GH.format("gitlab")), ("Grafana Labs", _GH.format("grafanalabs")), ("Canonical", _GH.format("canonical")),
]


def _owner(db):
    return db.query(Account).filter(Account.role == "owner").first()


def ensure_profile(db, name: str, plan: list) -> uuid.UUID | None:
    owner = _owner(db)
    if owner is None:
        raise SystemExit("no owner account")
    row = db.query(Account).filter(Account.name == name).first()
    if row is not None:
        if row.parent_account_id is None and row.role != "owner":
            row.parent_account_id = owner.id
            plan.append(f"link existing account {name!r} to the owner")
        return row.id
    if db.query(Account).count() >= admin.MAX_ACCOUNTS:
        raise SystemExit(f"the install already holds {admin.MAX_ACCOUNTS} accounts")
    used = {a.color.lower() for a in db.query(Account).all() if a.color}
    color = next((c for c in ACCOUNT_COLORS if c.lower() not in used), ACCOUNT_COLORS[1])
    new = Account(id=uuid.uuid4(), name=name, color=color, role="member", parent_account_id=owner.id)
    db.add(new)
    db.flush()
    admin.seed_new_account(db, new.id)
    copied = admin.copy_from(db, owner.id, new.id, COPY_ITEMS, owner_id=owner.id)
    plan.append(f"create linked profile {name!r}, copying {copied}")
    return new.id


def ensure_settings(db, account_id, plan: list) -> None:
    for key, value in SETTINGS.items():
        put_setting(db, key, value, account_id=account_id)
    admin._set_policy(db, account_id, "linkedin", "inherit")
    plan.append(f"curator: {SETTINGS}; LinkedIn sessions inherited from the owner")


def ensure_persona(db, account_id, plan: list) -> None:
    p = db.query(Persona).filter(Persona.account_id == account_id).first()
    if p is None:
        return
    prefs = dict(p.preferences or {})
    changed = []
    if not prefs.get("visa_route"):
        prefs["visa_route"] = VISA_ROUTE
        changed.append("visa_route")
    if prefs.get("willing_to_relocate") is None:
        prefs["willing_to_relocate"] = True
        changed.append("willing_to_relocate")
    if changed:
        p.preferences = prefs
        plan.append(f"persona preferences: {', '.join(changed)}")


def ensure_searches(db, account_id, plan: list) -> None:
    have = {n for (n,) in db.query(Search.name).filter(Search.account_id == account_id).all()}
    for name, country, location, term, boards in SEARCHES:
        if name in have:
            continue
        db.add(Search(account_id=account_id, name=name, search_mode="keyword", search_term=term, country=country,
                      location=location, sources=boards, hours_old=24, results_wanted=50,
                      title_exclude_keywords=TITLE_EXCLUDE))
        plan.append(f"search {name!r}")
    for name, mode, term in FEEDS:
        if name in have:
            continue
        db.add(Search(account_id=account_id, name=name, search_mode=mode, search_term=term, sources=[],
                      location="", hours_old=24, title_exclude_keywords=TITLE_EXCLUDE))
        plan.append(f"search {name!r}")


def ensure_companies(db, account_id, plan: list) -> None:
    have = {n.lower() for (n,) in db.query(Company.name).filter(Company.account_id == account_id).all()}
    added = []
    for name, url in COMPANIES:
        if name.lower() in have:
            continue
        db.add(Company(account_id=account_id, name=name, active=True, scrape_urls=[url],
                       title_include_expr=TITLE_EXPR, title_exclude_keywords=TITLE_EXCLUDE,
                       notes="Seeded for the international lane (hires from abroad, English-first)."))
        added.append(name)
    if added:
        plan.append(f"{len(added)} companies: {', '.join(added)}")


def run(db, name: str = DEFAULT_NAME) -> list[str]:
    """Do every step on `db` (no commit); returns what was added."""
    plan: list[str] = []
    with unscoped():
        account_id = ensure_profile(db, name, plan)
        ensure_settings(db, account_id, plan)
        ensure_persona(db, account_id, plan)
        ensure_searches(db, account_id, plan)
        ensure_companies(db, account_id, plan)
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--commit", action="store_true", help="write (default: dry run)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = SessionLocal()
    try:
        plan = run(db, args.name)
        for line in plan:
            logger.info(f"- {line}")
        if args.commit:
            db.commit()
            acct_auth.invalidate_accounts_cache()
            logger.info("written")
        else:
            db.rollback()
            logger.info("dry run: nothing written (add --commit)")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
