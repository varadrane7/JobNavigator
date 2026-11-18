"""Relocation-lane ingestion gate (curator/market.lane_blocker, h1b_checker.check_job_h1b) and
optional seniority in curator titles (curator/scoring.title_excluded)."""
import asyncio
from types import SimpleNamespace

from backend.curator import market
from backend.curator.scoring import title_excluded

DUTCH = "Wij zoeken een ervaren ontwikkelaar voor ons team. Jij hebt ervaring met Java en kennis van het domein. " * 4


def test_dutch_postings_and_requirements_are_blocked():
    assert market.lane_blocker(DUTCH) == "dutch: posting is in Dutch"
    assert market.lane_blocker("You speak fluent Dutch and English.") == "dutch: fluent dutch"
    assert market.lane_blocker("Dutch is a plus; our working language is English.") is None


def test_german_and_local_only_postings_are_blocked():
    assert market.lane_blocker("Sehr gute Deutschkenntnisse in Wort und Schrift.").startswith("german:")
    assert market.lane_blocker("We are unable to offer visa sponsorship.").startswith("jd:")
    assert market.lane_blocker("International team, English is our working language.") is None


def test_optional_seniority_is_not_a_title_exclude():
    words = ["senior", "sr.", "lead"]
    assert title_excluded("(Senior) Software Engineer (m/w/d)", words) is None
    assert title_excluded("Mid/Senior Backend Engineer", words) is None
    assert title_excluded("Senior Software Engineer", words) == "senior"
    assert title_excluded("(Senior) Team Lead Backend", words) == "lead"


def _job(desc):
    return SimpleNamespace(company="Acme", description=desc, h1b_jd_flag=False, h1b_jd_snippet=None)


def test_check_job_h1b_flags_lane_blockers_only_in_a_relocation_lane(monkeypatch):
    from backend.analyzer import h1b_checker as H

    async def no_data(*a, **k):
        return {}
    monkeypatch.setattr(H, "resolve_company_h1b", no_data)
    for countries, flagged in (('["DE", "NL"]', True), ('["US"]', False)):
        monkeypatch.setattr(H, "get_setting", lambda db, key, default=None, c=countries: c)
        job = _job(DUTCH)
        asyncio.run(H.check_job_h1b(job, db=None, company_lookup={}, phrases=[]))
        assert job.h1b_jd_flag is flagged
        if flagged:
            assert job.h1b_jd_snippet == "dutch: posting is in Dutch"
