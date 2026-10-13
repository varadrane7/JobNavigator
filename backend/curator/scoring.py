"""Pure scoring for the daily curator: no DB, no network, so every rule is unit-testable.

Each part is 0..1; the composite is their weighted sum (weights in `curate_weights`).
"""
import re
from datetime import datetime, timezone

DEFAULT_WEIGHTS = {"fit": 0.40, "fresh": 0.20, "visa_comp": 0.20, "reply": 0.12, "stability": 0.08}

LATE_STAGES = {"series_c", "series_d", "series_e", "series_f", "series_g", "public"}
STAGE_STABILITY = {
    "public": 0.8, "series_g": 0.9, "series_f": 0.9, "series_e": 0.9, "series_d": 0.9, "series_c": 0.85,
    "series_b": 0.7, "series_a": 0.55, "seed": 0.4, "pre_seed": 0.3, "bootstrapped": 0.55,
}
FRIENDLY_ATS = ("greenhouse.io", "ashbyhq.com", "lever.co", "workable.com", "rippling.com")
SLOW_ATS = ("myworkdayjobs.com", "taleo.net", "icims.com", "successfactors", "oraclecloud.com", "brassring")

# Postings a STEM-OPT candidate who will need H-1B cannot take, whatever the
# company's sponsorship record says.
_BLOCKERS = re.compile(
    r"\b(u\.?s\.? citizen(s|ship)?|united states citizen(s|ship)?|citizens only|must be a citizen|"
    r"(active|current|secret|top secret|ts/sci|dod|public trust) (security )?clearance|clearance required|"
    r"able to obtain (a |an )?(security )?clearance|green card holders?( only)?|permanent residents? only|"
    r"(not|unable to|cannot|can't|won't|will not|do not|does not) (provide |offer )?(visa )?sponsor(ship)?|"
    r"no (visa )?sponsorship|without (the need for )?(visa )?sponsorship|"
    r"not eligible for (visa )?sponsorship|itar|export control(led)? (regulations|laws))\b", re.I)
_YOE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(?:\d{1,2}\s*)?\+?\s*years?(?:\s+of)?(?:\s+\w+){0,4}?\s+experience", re.I)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── hard drops ──────────────────────────────────────────────────────────────
def title_excluded(title: str, words: list[str]) -> str | None:
    """The exclude word that matches `title` (word-boundary, case-insensitive), or None."""
    t = f" {(title or '').lower()} "
    for w in words:
        w = (w or "").lower()
        if not w:
            continue
        if w.endswith((".", " ")):        # "sr." / "sr " — literal match
            if w in t:
                return w.strip()
        elif re.search(rf"\b{re.escape(w)}\b", t):
            return w
    return None


def jd_blocker(description: str) -> str | None:
    """The phrase that rules the job out for a visa holder (citizenship, clearance, no sponsorship), or None."""
    m = _BLOCKERS.search(description or "")
    return m.group(0).lower() if m else None


def min_years_required(description: str) -> int | None:
    """Smallest 'N years ... experience' the JD asks for, or None if it never says."""
    found = [int(m.group(1)) for m in _YOE.finditer(description or "")]
    found = [n for n in found if 0 < n < 30]
    return min(found) if found else None


# ── parts ───────────────────────────────────────────────────────────────────
def hours_since(ts: datetime | None, now: datetime | None = None) -> float | None:
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds() / 3600)


def freshness(hours: float | None) -> float:
    """1.0 for the first 4 hours, then halves every 18 hours (24h ≈ 0.46, 48h ≈ 0.18)."""
    if hours is None:
        return 0.3
    if hours <= 4:
        return 1.0
    return 0.5 ** ((hours - 4) / 18)


def sponsor_score(lca_count: int | None, approval_rate: float | None, jd_flag: bool = False) -> float:
    if jd_flag:
        return 0.0
    n = lca_count or 0
    s = 1.0 if n >= 200 else 0.85 if n >= 50 else 0.6 if n >= 10 else 0.35 if n >= 1 else 0.15
    if approval_rate and approval_rate < 80:
        s *= 0.8
    return s


def comp_estimate(salary_min, salary_max, lca_median=None, intel_min=None, intel_max=None) -> tuple[int | None, str]:
    """(estimated base, where it came from)."""
    # under $20k is an hourly/monthly figure or a placeholder, not an annual base
    lo = salary_min if salary_min and salary_min >= 20000 else 0
    hi = salary_max if salary_max and salary_max >= 20000 else 0
    if lo and hi:
        return (lo + hi) // 2, "posted"
    if lo or hi:
        return lo or hi, "posted"
    if lca_median:
        return int(lca_median), "lca"
    if intel_min and intel_max:
        return (intel_min + intel_max) // 2, "web"
    if intel_min or intel_max:
        return intel_min or intel_max, "web"
    return None, ""


def comp_score(base: int | None, target_min: int, target_max: int) -> float:
    if not base:
        return 0.4
    floor = target_min - 25000
    return clamp((base - floor) / max(1, target_max - floor))


def well_funded(stage: str | None, last_raise_year: int | None, lca_count: int | None, this_year: int) -> bool:
    stage = (stage or "").lower()
    if stage in LATE_STAGES or (lca_count or 0) >= 50:
        return True
    return stage == "series_b" and bool(last_raise_year) and last_raise_year >= this_year - 2


def visa_comp_score(sponsor: float, comp: float, funded: bool) -> float:
    return max(sponsor, comp, 0.8 if funded else 0.0)


def reply_score(own_rate: float | None, own_n: int, reputation: float | None, url: str = "") -> float:
    if own_rate is not None and own_n >= 3:
        s = clamp(own_rate * 2)
    elif reputation is not None:
        s = clamp(reputation)
    else:
        s = 0.5
    host = (url or "").lower()
    if any(a in host for a in FRIENDLY_ATS):
        s += 0.1
    elif any(a in host for a in SLOW_ATS):
        s -= 0.1
    return clamp(s)


def stability_score(stage: str | None, last_raise_year: int | None, layoffs: bool | None, this_year: int) -> float:
    s = STAGE_STABILITY.get((stage or "").lower(), 0.5)
    if last_raise_year and last_raise_year >= this_year - 1:
        s += 0.1
    if layoffs:
        s -= 0.3
    return clamp(s)


def fit_score(llm_score: float | None, embed_pct: float | None) -> float:
    """LLM fit (0..100) blended with the job's embedding-similarity percentile in the pool."""
    if llm_score is None and embed_pct is None:
        return 0.0
    if llm_score is None:
        return clamp(embed_pct) * 0.7          # unscored: never outranks a confirmed fit
    if embed_pct is None:
        return clamp(llm_score / 100)
    return clamp(0.85 * llm_score / 100 + 0.15 * embed_pct)


def composite(parts: dict, weights: dict | None = None) -> float:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    total = sum(w.values()) or 1.0
    return round(sum(w[k] * parts.get(k, 0.0) for k in w) / total, 4)


def apply_window(rank: int, fresh: float) -> str:
    """now = apply first thing this morning; morning = before noon; today = by end of day."""
    if rank <= 15 or fresh >= 0.8:
        return "now"
    if rank <= 35:
        return "morning"
    return "today"


def reason(*, best_cv, llm_score, hours, lca_count, stage, last_raise_year, base, base_src, reply_note="") -> str:
    bits = []
    if llm_score is not None:
        bits.append(f"fit {round(llm_score)}" + (f" ({best_cv})" if best_cv else ""))
    if hours is not None:
        bits.append(f"posted {round(hours)}h ago" if hours < 48 else f"posted {round(hours / 24)}d ago")
    if lca_count:
        bits.append(f"H-1B {lca_count} LCAs")
    if stage and stage != "unknown":
        bits.append(stage.replace("_", " ").title() + (f" '{str(last_raise_year)[-2:]}" if last_raise_year else ""))
    if base:
        bits.append(f"~${base // 1000}k base ({base_src})")
    if reply_note:
        bits.append(reply_note)
    return " · ".join(bits)
