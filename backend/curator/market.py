"""Which job markets an account's curator works, and the signals that replace US H-1B data
outside the US. Pure: no DB, no network.

An account's `curate_countries` (ISO-2 list, default ["US"]) picks its market. With "US" in it the
curator runs as it always has (H-1B sponsor data, USD pay). Without it the account is a
relocation lane (e.g. Germany, Netherlands, Ireland, UAE): sponsorship is read from the posting
(visa / relocation / Blue Card wording), postings that need German or local work rights are
dropped, and pay is compared in the market's own currency.
"""
import json
import re

DEFAULT_COUNTRIES = ["US"]

# Rough FX to USD, for ranking only (not shown as money). AED is pegged.
FX_USD = {"USD": 1.0, "EUR": 1.15, "GBP": 1.33, "AED": 0.272, "CHF": 1.24}

# A mid-level backend engineer's realistic gross base per market, local currency per year.
BANDS = {
    "DE": (60000, 80000, "EUR"),
    "NL": (55000, 75000, "EUR"),
    "IE": (55000, 75000, "EUR"),
    "AT": (55000, 72000, "EUR"),
    "AE": (216000, 300000, "AED"),
}
# EU Blue Card, Germany: reduced threshold (shortage occupations / graduates within 3 years), 2026.
BLUE_CARD_DE_REDUCED = 45934


def countries(raw) -> list[str]:
    """`curate_countries` as an upper-case ISO-2 list; the default when empty or unreadable."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip().startswith("[") else raw.split(",")
        except ValueError:
            raw = None
    out = [str(c).strip().upper() for c in (raw or []) if str(c).strip()]
    return out or list(DEFAULT_COUNTRIES)


def is_relocation(cs: list[str]) -> bool:
    """True for a lane abroad: no US in the market list, so H-1B data says nothing."""
    return "US" not in cs


def in_market(loc_country, cs: list[str]) -> bool:
    """A job is in the pool when its country is one of the account's, or unknown (the account's
    own searches found it, and a remote or unparsed location says no country)."""
    return not loc_country or loc_country.upper() in cs


# ── sponsorship read from the posting ──────────────────────────────────────
_VISA_YES = re.compile(
    r"\b(visa sponsorship (is )?(available|provided|offered)|we (can |will |do )?(offer|provide|support) "
    r"(visa|relocation|work permit)|sponsor(ship)? (your |a |the )?(visa|work permit|blue card)|"
    r"(eu )?blue card|relocation (package|support|assistance|bonus|budget)|help (you )?(with )?relocat\w+|"
    r"we (support|assist with|help with) relocation|relocation (to|is) (offered|provided|covered)|"
    r"visa (and|&) relocation|open to (international|candidates from abroad))\b", re.I)
_ENGLISH_OK = re.compile(
    r"\b(working language (is|will be) english|english is (our|the) (working|company|official) language|"
    r"no german (is )?(required|needed)|german (is )?(not required|a plus|nice to have|optional)|"
    r"english[- ]speaking (team|company|environment)|international team)\b", re.I)


def relocation_score(description: str, tracked: bool = False) -> float:
    """0..1 stand-in for `sponsor_score` abroad: says it sponsors or relocates > English-first >
    nothing said. A company on the Companies list (picked as a known sponsor) gets a floor."""
    text = description or ""
    s = 1.0 if _VISA_YES.search(text) else 0.65 if _ENGLISH_OK.search(text) else 0.35
    return max(s, 0.7) if tracked else s


# ── postings a relocating candidate cannot take ────────────────────────────
_LOCAL_ONLY = re.compile(
    r"\b(eu citizens? only|(must|need to|required to) (be an? )?eu (citizen|national)|"
    r"(valid|existing|current) (eu |german |dutch |irish |uae )?(work|residence) (permit|authori[sz]ation|visa) "
    r"(is )?(required|needed|mandatory|a must)|"
    r"(must|need to) (already )?(have|hold|possess) (the )?(right|permission|authori[sz]ation) to work in "
    r"(the eu|europe|germany|the netherlands|ireland|the uae)|"
    r"(we (are )?(unable|not able) to|we can ?not|we do not|we don't) (offer |provide |support )?"
    r"(visa sponsorship|sponsorship|relocation|work permits?)|"
    r"no (visa sponsorship|relocation)( support)? (is )?(offered|provided|available)|"
    r"(uae|emirati) nationals? only|only (uae|emirati) nationals?|emiratisation|emiratization)\b", re.I)


def local_only(description: str) -> str | None:
    m = _LOCAL_ONLY.search(description or "")
    return m.group(0).lower() if m else None


_GERMAN_REQ = re.compile(
    r"\b((fluent|fließend\w*|sehr gut\w*|verhandlungssicher\w*|excellent|very good|good|strong|business[- ]fluent|"
    r"native)\s+(command of )?(german|deutsch\w*)|"
    r"(german|deutsch)\s*(\(|-|:)?\s*(c1|c2|b2|level b2|level c1|native|fluent|verhandlungssicher|fließend)|"
    r"(german|deutsch\w*) (language )?(skills )?(are |is )?(required|mandatory|a must|essential|unerlässlich|"
    r"erforderlich|vorausgesetzt)|deutschkenntnisse|"
    r"(c1|c2|b2)[- ]?(level )?(german|deutsch)|german is (the|our) working language)\b", re.I)
_GERMAN_WORDS = re.compile(r"\b(und|der|die|das|mit|für|wir|sie|ihre|eine?n?|bei|auf|zu|von|ist|sind|oder|"
                           r"aufgaben|kenntnisse|erfahrung)\b", re.I)
_ENGLISH_WORDS = re.compile(r"\b(and|the|with|for|we|you|your|our|of|to|in|is|are|or|experience)\b", re.I)


def german_required(description: str) -> str | None:
    """Why the posting needs German: a stated requirement, or a posting written in German."""
    text = description or ""
    m = _GERMAN_REQ.search(text)
    if m and not re.search(r"(german|deutsch)\w*\s+(is\s+)?(a plus|nice to have|optional|not required|von vorteil|wünschenswert)",
                           text[max(0, m.start() - 10):m.end() + 40], re.I):
        return m.group(0).lower()
    de, en = len(_GERMAN_WORDS.findall(text)), len(_ENGLISH_WORDS.findall(text))
    if de >= 25 and de > 1.5 * en:
        return "posting is in German"
    return None


# ── pay in the market's currency ────────────────────────────────────────────
_NUM = r"(\d{2,3}(?:[.,\s]\d{3})|\d{2,3}\s?[kK])"
_EUR = re.compile(rf"(?:€|EUR)\s?{_NUM}(?:\s?(?:-|–|to|bis)\s?(?:€|EUR)?\s?{_NUM})?|"
                  rf"{_NUM}(?:\s?(?:-|–|to|bis)\s?{_NUM})?\s?(?:€|EUR)", re.I)
_AED = re.compile(rf"AED\s?{_NUM}(?:\s?(?:-|–|to)\s?(?:AED)?\s?{_NUM})?|{_NUM}(?:\s?(?:-|–|to)\s?{_NUM})?\s?AED", re.I)


def _amount(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip()
    if s[-1] in "kK":
        return int(s[:-1].strip()) * 1000
    return int(re.sub(r"[.,\s]", "", s))


def posted_pay(description: str) -> tuple[int | None, str]:
    """(yearly base in local currency, currency) from a EUR or AED figure in the posting, or
    (None, ""). AED under 80k is read as monthly (the UAE norm)."""
    text = description or ""
    for rx, cur in ((_EUR, "EUR"), (_AED, "AED")):
        for m in rx.finditer(text):
            nums = [_amount(g) for g in m.groups() if g]
            nums = [n for n in nums if n]
            if not nums:
                continue
            mid = sum(nums) // len(nums)
            if cur == "AED" and mid < 80000:
                mid *= 12
            if (cur == "EUR" and 20000 <= mid <= 300000) or (cur == "AED" and 60000 <= mid <= 1500000):
                return mid, cur
    return None, ""


def to_usd(amount: int | None, currency: str) -> int | None:
    rate = FX_USD.get(currency or "")
    return int(amount * rate) if amount and rate else None


def comp_score(amount: int | None, currency: str, country: str | None) -> float:
    """0..1 for a posted local pay against the market band; 0.4 when nothing is posted."""
    band = BANDS.get((country or "").upper())
    if not amount or not band:
        return 0.4
    lo, hi, band_cur = band
    usd, lo_usd, hi_usd = to_usd(amount, currency), to_usd(lo, band_cur), to_usd(hi, band_cur)
    if not usd:
        return 0.4
    floor = lo_usd * 0.75
    return max(0.0, min(1.0, (usd - floor) / max(1, hi_usd - floor)))


def below_blue_card(amount: int | None, currency: str, country: str | None) -> bool:
    """A German posting whose stated pay is under the reduced Blue Card threshold."""
    return (country or "").upper() == "DE" and currency == "EUR" and bool(amount) and amount < BLUE_CARD_DE_REDUCED


def pay_note(amount: int | None, currency: str) -> str:
    if not amount:
        return ""
    sym = "€" if currency == "EUR" else f"{currency} "
    return f"~{sym}{amount // 1000}k base (posted)"
