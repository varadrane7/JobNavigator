"""Pure curator rules: drops, parts, composite, windows."""
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.curator import scoring as s
from backend.scraper._shared.posted import parse_posted


@pytest.mark.parametrize("title,hit", [
    ("Senior Software Engineer", "senior"),
    ("Sr. Backend Engineer", "sr."),
    ("Staff Engineer, Payments", "staff"),
    ("Software Engineer II", None),
    ("Backend Engineer (Platform)", None),
    ("Engineering Manager", "manager"),
    ("Leadership Development Engineer", None),     # word boundary: "lead" ≠ "leadership"
])
def test_title_excluded(title, hit):
    words = ["senior", "sr.", "staff", "principal", "lead", "manager"]
    assert s.title_excluded(title, words) == hit


@pytest.mark.parametrize("jd,yrs", [
    ("You have 5+ years of experience building APIs", 5),
    ("3-5 years of professional experience", 3),
    ("At least 7 years of relevant industry experience", 7),
    ("We value curiosity.", None),
    ("2+ years experience with Go; 8+ years of experience preferred", 2),
])
def test_min_years_required(jd, yrs):
    assert s.min_years_required(jd) == yrs


def test_freshness_curve():
    assert s.freshness(1) == 1.0
    assert 0.4 < s.freshness(24) < 0.5
    assert 0.1 < s.freshness(48) < 0.25
    assert s.freshness(None) == 0.3
    assert s.freshness(10) > s.freshness(30)


def test_sponsor_score_orders_by_lca_and_zeroes_on_flag():
    assert s.sponsor_score(500, 98) > s.sponsor_score(30, 98) > s.sponsor_score(2, 98) > s.sponsor_score(0, None)
    assert s.sponsor_score(500, 98, jd_flag=True) == 0.0
    assert s.sponsor_score(500, 60) < s.sponsor_score(500, 95)


def test_comp_estimate_prefers_posted_then_lca_then_web():
    assert s.comp_estimate(120000, 160000, 150000, 1, 2) == (140000, "posted")
    assert s.comp_estimate(None, None, 150000, 1, 2) == (150000, "lca")
    assert s.comp_estimate(None, None, None, 130000, 150000) == (140000, "web")
    assert s.comp_estimate(None, None) == (None, "")


def test_comp_score_band():
    assert s.comp_score(150000, 115000, 150000) == 1.0
    assert s.comp_score(90000, 115000, 150000) == 0.0
    assert 0.4 < s.comp_score(120000, 115000, 150000) < 0.8
    assert s.comp_score(None, 115000, 150000) == 0.4


def test_well_funded_and_visa_comp():
    assert s.well_funded("series_d", None, 0, 2026)
    assert s.well_funded("series_b", 2025, 0, 2026)
    assert not s.well_funded("series_b", 2021, 0, 2026)
    assert s.well_funded("unknown", None, 120, 2026)
    assert s.visa_comp_score(0.15, 0.2, True) == 0.8
    assert s.visa_comp_score(1.0, 0.2, False) == 1.0


def test_reply_score_uses_own_history_first_and_ats_hint():
    assert s.reply_score(0.5, 4, 0.1) == 1.0
    assert s.reply_score(0.5, 2, 0.3) == 0.3              # too few own apps: reputation wins
    assert s.reply_score(None, 0, None, "https://boards.greenhouse.io/x/1") == pytest.approx(0.6)
    assert s.reply_score(None, 0, None, "https://x.wd5.myworkdayjobs.com/a") == pytest.approx(0.4)


def test_fit_score_unscored_never_beats_confirmed_fit():
    assert s.fit_score(None, 1.0) < s.fit_score(80, 0.5)
    assert s.fit_score(90, None) == 0.9


def test_composite_is_weighted_mean_and_deterministic():
    parts = {"fit": 1, "fresh": 1, "visa_comp": 1, "reply": 1, "stability": 1}
    assert s.composite(parts) == 1.0
    assert s.composite({"fit": 1}) == pytest.approx(0.4)
    assert s.composite({"fit": 1}, {"fit": 1, "fresh": 0, "visa_comp": 0, "reply": 0, "stability": 0}) == 1.0


def test_apply_window():
    assert s.apply_window(3, 0.2) == "now"
    assert s.apply_window(40, 0.9) == "now"
    assert s.apply_window(20, 0.3) == "morning"
    assert s.apply_window(45, 0.3) == "today"


def test_reason_line():
    r = s.reason(best_cv="SDE", llm_score=86.4, hours=5.2, lca_count=320, stage="series_c",
                 last_raise_year=2025, base=140000, base_src="posted")
    assert r == "fit 86 (SDE) · posted 5h ago · H-1B 320 LCAs · Series C '25 · ~$140k base (posted)"


# ── posted_at parsing ───────────────────────────────────────────────────────
NOW = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)   # 10:00 EDT


def test_parse_posted_relative_and_iso():
    assert parse_posted("3 hours ago", NOW) == NOW - timedelta(hours=3)
    assert parse_posted("Just now", NOW) == NOW
    assert parse_posted("2 days ago", NOW) == NOW - timedelta(days=2)
    assert parse_posted("2026-10-13T10:30:00Z", NOW) == datetime(2026, 10, 13, 10, 30, tzinfo=timezone.utc)
    assert parse_posted(None, NOW) is None
    assert parse_posted("", NOW) is None
    assert parse_posted("garbage", NOW) is None


def test_parse_posted_bare_date():
    assert parse_posted(date(2026, 10, 13), NOW) == NOW                         # today -> discovery time
    assert parse_posted(date(2026, 10, 11), NOW) == datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc)  # noon EDT
    assert parse_posted("2026-10-11", NOW) == datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc)


def test_parse_posted_never_in_future():
    assert parse_posted("2027-01-01T00:00:00Z", NOW) == NOW
