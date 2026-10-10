"""P5: tiers pick providers in order, quota / errors cool a provider down and fail over, usage is counted."""
import pytest

from backend.analyzer.llm_client import NonRetryableLLMError

CHAIN = [{"provider": "openai_local", "model": "m1"}, {"provider": "antigravity_cli", "model": ""},
         {"provider": "claude_code", "model": ""}]


class Dispatch:
    """Scripted provider behaviour: provider -> reply text or Exception."""

    def __init__(self, script):
        self.script, self.calls = script, []

    async def __call__(self, provider, model, prompt, system, max_tokens):
        self.calls.append((provider, model))
        r = self.script[provider]
        if isinstance(r, Exception):
            raise r
        return {"text": r, "usage": {"input_tokens": 10, "output_tokens": 5}}


@pytest.fixture
def router(test_db, aa_settings):
    from backend.autoapply import llm_router
    aa_settings(autoapply_llm_cheap=CHAIN, autoapply_llm_quality=list(reversed(CHAIN)))
    return llm_router


async def test_first_provider_answers_and_is_counted(router, test_db):
    d = Dispatch({"openai_local": "hi"})
    out = await router.call("cheap", "p", "s", dispatch=d)
    assert out["text"] == "hi" and out["provider"] == "openai_local" and out["model"] == "m1"
    assert d.calls == [("openai_local", "m1")]
    row = router.usage_summary(test_db)[0]
    assert (row["provider"], row["calls"], row["failures"], row["input_tokens"]) == ("openai_local", 1, 0, 10)


async def test_quota_fails_over_and_cools_down(router, test_db):
    d = Dispatch({"claude_code": NonRetryableLLMError("Claude Code usage limit reached"),
                  "antigravity_cli": "from agy"})
    out = await router.call("quality", "p", "s", dispatch=d)        # quality: claude_code first
    assert out["provider"] == "antigravity_cli"
    from backend.autoapply import settings
    cfg = settings.get_all(test_db)
    assert "claude_code" in router.cooldowns(cfg)
    assert [e["provider"] for e in router.chain(cfg, "quality")] == ["antigravity_cli", "openai_local"]
    row = {r["provider"]: r for r in router.usage_summary(test_db)}["claude_code"]
    assert row["quota_hits"] == 1 and row["failures"] == 1
    # Next call skips the cooling provider without trying it.
    d2 = Dispatch({"antigravity_cli": "again"})
    await router.call("quality", "p", "s", dispatch=d2)
    assert d2.calls == [("antigravity_cli", "")]


async def test_plain_error_gets_short_cooldown(router, test_db):
    d = Dispatch({"openai_local": RuntimeError("connection refused"), "antigravity_cli": "ok"})
    out = await router.call("cheap", "p", "s", dispatch=d)
    assert out["provider"] == "antigravity_cli"
    from datetime import datetime, timezone
    from backend.autoapply import settings
    until = router.cooldowns(settings.get_all(test_db))["openai_local"]
    assert 0 < (until - datetime.now(timezone.utc)).total_seconds() <= 5 * 60


async def test_empty_reply_counts_as_failure(router):
    d = Dispatch({"openai_local": "   ", "antigravity_cli": "real"})
    assert (await router.call("cheap", "p", "s", dispatch=d))["text"] == "real"


async def test_all_failed_raises(router):
    d = Dispatch({p["provider"]: RuntimeError("down") for p in CHAIN})
    with pytest.raises(router.AllProvidersFailed) as e:
        await router.call("cheap", "p", "s", dispatch=d)
    assert "openai_local" in str(e.value)
    with pytest.raises(router.AllProvidersFailed):          # everyone cooling now
        await router.call("cheap", "p", "s", dispatch=d)


async def test_unknown_tier(router):
    with pytest.raises(ValueError):
        await router.call("fast", "p", "s", dispatch=Dispatch({}))


def test_usage_route(api_client, router):
    r = api_client.get("/api/autoapply/llm/usage")
    assert r.status_code == 200 and r.json() == {"usage": [], "cooling": {}}


async def test_effort_reaches_dispatch_only_when_set(router, aa_settings):
    seen = []

    async def d(provider, model, prompt, system, max_tokens, **kw):
        seen.append((provider, model, kw))
        return {"text": "ok"}
    aa_settings(autoapply_llm_cheap=[{"provider": "claude_code", "model": "claude-haiku-4-5", "effort": "low"}],
                autoapply_llm_quality=[{"provider": "openai_local", "model": ""}])
    await router.call("cheap", "p", "s", dispatch=d)
    await router.call("quality", "p", "s", dispatch=d)
    assert seen == [("claude_code", "claude-haiku-4-5", {"effort": "low"}), ("openai_local", "", {})]


def test_seed_names_models_in_untouched_chains_only(test_db):
    from backend.autoapply import settings
    from backend.models.db import Setting
    edited = [{"provider": "claude_code", "model": ""}]
    test_db.add(Setting(key="autoapply_llm_cheap", value=settings._dump("json", settings._OLD_DEFAULTS["autoapply_llm_cheap"])))
    test_db.add(Setting(key="autoapply_llm_quality", value=settings._dump("json", edited)))
    test_db.commit()
    settings.seed(test_db)
    cfg = settings.get_all(test_db)
    assert cfg["autoapply_llm_cheap"] == settings.DEFAULT_CHEAP
    assert all(e["model"] for e in cfg["autoapply_llm_cheap"] if e["provider"] != "openai_local")
    assert cfg["autoapply_llm_quality"] == edited
