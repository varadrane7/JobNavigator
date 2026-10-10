"""Spread AutoApply's LLM work over the providers you have: a local model for cheap, high-volume
calls (form answers), the Claude Code / Antigravity subscriptions for quality calls (tailoring,
cover letters). A provider that hits its quota or errors is left alone for a cooldown and the next
one in the tier's chain is tried, so a run is never stuck behind one dead provider.

Each tier is an ordered list of {"provider", "model"} in settings (autoapply_llm_cheap /
autoapply_llm_quality). An empty model lets the provider use its own default."""
import logging
from datetime import date, datetime, timedelta, timezone

from backend.autoapply import settings
from backend.autoapply.models import AutoApplyLLMUsage

logger = logging.getLogger("jobnavigator.autoapply.llm_router")

TIERS = ("cheap", "quality")


class AllProvidersFailed(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_quota(exc: Exception) -> bool:
    from backend.analyzer.llm_client import NonRetryableLLMError, _QUOTA_RE
    return isinstance(exc, NonRetryableLLMError) or bool(_QUOTA_RE.search(str(exc)))


def cooldowns(cfg) -> dict:
    out = {}
    for prov, until in (cfg.get("autoapply_llm_cooldowns") or {}).items():
        try:
            t = datetime.fromisoformat(str(until))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out[prov] = t
    return out


def chain(cfg, tier: str) -> list[dict]:
    """The tier's provider list with providers that are cooling down moved out of the way."""
    entries = [e for e in (cfg.get(f"autoapply_llm_{tier}") or []) if isinstance(e, dict) and e.get("provider")]
    now = _now()
    cd = cooldowns(cfg)
    return [e for e in entries if not (cd.get(e["provider"]) and cd[e["provider"]] > now)]


def _bump(db, provider: str, model: str, *, ok: bool, quota: bool = False, usage: dict | None = None) -> None:
    day = _now().date()
    row = (db.query(AutoApplyLLMUsage)
           .filter(AutoApplyLLMUsage.provider == provider, AutoApplyLLMUsage.day == day).first())
    if not row:
        row = AutoApplyLLMUsage(provider=provider, day=day, calls=0, failures=0, quota_hits=0,
                                input_tokens=0, output_tokens=0)
        db.add(row)
    row.model = model or row.model
    row.calls = (row.calls or 0) + 1
    if not ok:
        row.failures = (row.failures or 0) + 1
    if quota:
        row.quota_hits = (row.quota_hits or 0) + 1
        row.last_quota_at = _now()
    if usage:
        row.input_tokens = (row.input_tokens or 0) + int(usage.get("input_tokens") or 0)
        row.output_tokens = (row.output_tokens or 0) + int(usage.get("output_tokens") or 0)
    db.commit()


def _cool(db, cfg, provider: str, quota: bool) -> None:
    minutes = int(cfg["autoapply_llm_quota_cooldown_min"] if quota else cfg["autoapply_llm_error_cooldown_min"])
    cd = dict(cfg.get("autoapply_llm_cooldowns") or {})
    cd[provider] = (_now() + timedelta(minutes=minutes)).isoformat()
    settings.update(db, {"autoapply_llm_cooldowns": cd})
    logger.warning(f"{provider} cooling down {minutes} min ({'quota' if quota else 'error'})")


async def _dispatch(provider: str, model: str, prompt: str, system: str, max_tokens: int) -> dict:
    from backend.analyzer import llm_client
    return await llm_client._dispatch(provider, model, "", prompt, system, max_tokens)


async def call(tier: str, prompt: str, system: str, max_tokens: int = 1200, purpose: str = "autoapply",
               job_id=None, dispatch=None) -> dict:
    """Run one completion on the first healthy provider of the tier. Returns the provider's reply
    dict plus "provider"/"model". Raises AllProvidersFailed when every provider in the chain failed
    or is cooling down."""
    from backend.models.db import SessionLocal
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    dispatch = dispatch or _dispatch
    db = SessionLocal()
    try:
        cfg = settings.get_all(db)
        errors: list[str] = []
        for e in chain(cfg, tier):
            provider, model = e["provider"], e.get("model") or ""
            try:
                resp = await dispatch(provider, model, prompt, system, max_tokens)
                if not str((resp or {}).get("text") or "").strip():
                    raise RuntimeError("empty reply")
            except Exception as exc:
                quota = _is_quota(exc)
                _bump(db, provider, model, ok=False, quota=quota)
                _cool(db, cfg, provider, quota)
                cfg = settings.get_all(db)
                errors.append(f"{provider}: {str(exc)[:160]}")
                continue
            _bump(db, provider, model, ok=True, usage=resp.get("usage"))
            try:
                from backend.analyzer.llm_logger import track_llm_call
                async with track_llm_call(purpose, provider, model, job_id=job_id) as t:
                    t.record({**resp, "provider": provider, "model": model})
            except Exception:
                pass
            return {**resp, "provider": provider, "model": model}
        waiting = ", ".join(f"{p} until {t:%H:%M}" for p, t in cooldowns(cfg).items() if t > _now())
        raise AllProvidersFailed(
            f"no LLM provider answered for tier {tier}: " + ("; ".join(errors) or "chain empty")
            + (f" (cooling: {waiting})" if waiting else ""))
    finally:
        db.close()


def usage_summary(db, days: int = 7) -> list[dict]:
    since = date.today() - timedelta(days=days - 1)
    rows = (db.query(AutoApplyLLMUsage).filter(AutoApplyLLMUsage.day >= since)
            .order_by(AutoApplyLLMUsage.day.desc(), AutoApplyLLMUsage.provider).all())
    return [{"provider": r.provider, "model": r.model, "day": r.day.isoformat(), "calls": r.calls,
             "failures": r.failures, "quota_hits": r.quota_hits,
             "input_tokens": r.input_tokens, "output_tokens": r.output_tokens} for r in rows]
