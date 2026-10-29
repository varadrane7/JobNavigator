"""Market rules for a relocation lane: sponsorship, German, local-only postings and pay abroad."""
import pytest

from backend.curator import market as M


def test_countries_setting():
    assert M.countries(None) == ["US"]
    assert M.countries('["de", "AE"]') == ["DE", "AE"]
    assert M.countries("de, nl") == ["DE", "NL"]
    assert M.is_relocation(["DE", "AE"]) and not M.is_relocation(["US", "DE"])
    assert M.in_market("DE", ["DE"]) and M.in_market(None, ["DE"]) and not M.in_market("US", ["DE"])


@pytest.mark.parametrize("text,score", [
    ("We offer visa sponsorship and relocation support.", 1.0),
    ("EU Blue Card holders welcome; we sponsor your Blue Card.", 1.0),
    ("Our working language is English.", 0.65),
    ("Build APIs in Go.", 0.35),
])
def test_relocation_score(text, score):
    assert M.relocation_score(text) == score
    assert M.relocation_score("Build APIs in Go.", tracked=True) == 0.7


@pytest.mark.parametrize("text,hit", [
    ("Fluent German (C1) is required.", True),
    ("Sehr gute Deutschkenntnisse in Wort und Schrift.", True),
    ("German is a plus, English is required.", False),
    ("Python, Kubernetes, AWS. English is our working language.", False),
    (" ".join(["Wir suchen für unser Team eine erfahrene Person mit Kenntnisse und Erfahrung, die bei uns ist und die Aufgaben mit der Gruppe lösen."] * 3), True),
])
def test_german_required(text, hit):
    assert bool(M.german_required(text)) is hit


@pytest.mark.parametrize("text,hit", [
    ("EU citizens only.", True),
    ("A valid EU work permit is required.", True),
    ("Unfortunately we are unable to offer visa sponsorship.", True),
    ("UAE nationals only — Emiratisation role.", True),
    ("We offer visa sponsorship.", False),
])
def test_local_only(text, hit):
    assert bool(M.local_only(text)) is hit


@pytest.mark.parametrize("text,want", [
    ("Salary: €65,000 – €80,000 per year", (72500, "EUR")),
    ("65.000 - 75.000 EUR brutto", (70000, "EUR")),
    ("up to 90k €", (90000, "EUR")),
    ("AED 20,000 - 25,000 per month", (270000, "AED")),
    ("Package AED 300,000", (300000, "AED")),
    ("Team of 12, founded 2015", (None, "")),
])
def test_posted_pay(text, want):
    assert M.posted_pay(text) == want


def test_comp_and_blue_card():
    assert M.comp_score(None, "", "DE") == 0.4
    assert M.comp_score(80000, "EUR", "DE") == 1.0
    assert M.comp_score(50000, "EUR", "DE") < M.comp_score(70000, "EUR", "DE")
    assert M.below_blue_card(40000, "EUR", "DE") and not M.below_blue_card(40000, "EUR", "NL")
    assert M.pay_note(72500, "EUR") == "~€72k base (posted)"
