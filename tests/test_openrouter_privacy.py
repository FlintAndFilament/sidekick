import pytest

from sidekick.client import ClientError
from sidekick.openrouter import OpenRouterError
from sidekick.openrouter_catalog import PINNED_FREE_MODEL, PrivacyStatus
from tests._openrouter_fixtures import (
    DATA_POLICY_404,
    GATED_403,
    FakeOpenRouter,
    make_client,
    reply,
)

NEMO = "nvidia/nemotron-3-ultra-550b-a55b:free"
GEMMA = "google/gemma-4-31b-it:free"
GROK = "x-ai/grok-4.7"
SLOW = (429, {"error": {"code": 429, "message": "rate limited upstream"}})


async def test_deny_404_marks_every_provider(tmp_path):
    fake = FakeOpenRouter(replies=[(404, DATA_POLICY_404)])
    assert await make_client(fake, tmp_path).privacy_status(NEMO) is PrivacyStatus.ALL_MAY_COLLECT
    (body,) = fake.posts
    assert body["provider"] == {"data_collection": "deny"} and body["max_tokens"] == 16
    assert body["messages"] == [{"role": "user", "content": "."}]


async def test_deny_pass_marks_some_providers(tmp_path):
    client = make_client(FakeOpenRouter(replies=[(200, reply())]), tmp_path)
    assert await client.privacy_status(GROK) is PrivacyStatus.SOME_MAY_COLLECT


@pytest.mark.parametrize(
    "answer", [(403, GATED_403), SLOW, (500, {"error": {"code": 500, "message": "x"}})]
)
async def test_other_probe_outcomes_are_unknown_without_retries(tmp_path, answer):
    fake = FakeOpenRouter(replies=[answer])
    assert await make_client(fake, tmp_path).privacy_status(NEMO) is PrivacyStatus.UNKNOWN
    assert len(fake.posts) == 1


async def test_probe_is_cached(tmp_path):
    fake = FakeOpenRouter(replies=[(200, reply())])
    c = make_client(fake, tmp_path)
    await c.privacy_status(GROK)
    await c.privacy_status(GROK)
    assert len(fake.posts) == 1 and c.cached_privacy(GROK) is PrivacyStatus.SOME_MAY_COLLECT
    assert c.cached_privacy(NEMO) is None


@pytest.mark.parametrize("status", [401, 402])
async def test_probe_auth_or_credit_failure_raises(tmp_path, status):
    fake = FakeOpenRouter(replies=[(status, {"error": {"code": status, "message": "x"}})])
    with pytest.raises(OpenRouterError):
        await make_client(fake, tmp_path).privacy_status(GROK)


async def test_block_switch_sends_deny_and_names_itself(tmp_path):
    fake = FakeOpenRouter(replies=[(404, DATA_POLICY_404)])
    c = make_client(fake, tmp_path, block_data_collection=True)
    with pytest.raises(ClientError) as err:
        await c.ask(c.get_or_create_session("a", NEMO), "q")
    assert fake.posts[-1]["provider"] == {"data_collection": "deny"}
    assert (
        "Blocked by your setting `block_data_collection`: every provider serving "
        f"`{NEMO}` may store or train on prompts."
    ) in str(err.value)


async def test_account_setting_404_names_the_account_not_the_switch(tmp_path):
    fake = FakeOpenRouter(replies=[(404, DATA_POLICY_404)])
    c = make_client(fake, tmp_path)
    with pytest.raises(ClientError) as err:
        await c.ask(c.get_or_create_session("a", NEMO), "q")
    assert "provider" not in fake.posts[-1]
    assert "openrouter.ai/settings/privacy" in str(err.value)
    assert "block_data_collection" not in str(err.value)


async def test_forced_deny_404_names_the_unattended_routing(tmp_path):
    fake = FakeOpenRouter(replies=[(404, DATA_POLICY_404)])
    c = make_client(fake, tmp_path)
    c._deny_models.add(GROK)
    with pytest.raises(ClientError) as err:
        await c.ask(c.get_or_create_session("a", GROK), "q")
    assert fake.posts[-1]["provider"] == {"data_collection": "deny"}
    assert str(err.value) == (
        f"[sidekick] Not sent: no provider for `{GROK}` that doesn't collect data "
        "is available right now."
    )


async def test_gated_free_default_moves_to_next_candidate(tmp_path):
    c = make_client(FakeOpenRouter(replies=[(403, GATED_403)]), tmp_path)
    s = c.get_or_create_session("ask:x")
    with pytest.raises(OpenRouterError) as err:
        await c.ask(s, "q")
    assert err.value.gated and c.default_model == GEMMA
    assert c.get_or_create_session("ask:x").model == GEMMA


async def test_rate_limited_free_model_cools_down_for_ten_minutes(tmp_path):
    now = [1000.0]
    c = make_client(FakeOpenRouter(replies=[SLOW]), tmp_path, clock=lambda: now[0])
    with pytest.raises(OpenRouterError):
        await c.ask(c.get_or_create_session("a", NEMO), "q")
    assert c.default_model == GEMMA
    now[0] += 601
    assert c.default_model == NEMO


def test_fallback_and_can_fall_back(tmp_path):
    c = make_client(FakeOpenRouter(), tmp_path)
    assert c.fallback_for(NEMO) == GEMMA and c.fallback_for(GROK) is None
    assert c.can_fall_back(OpenRouterError("m", status=429))
    gated = OpenRouterError("m", status=403, routing_step="Gate Free Endpoints by Agentic Harness")
    assert c.can_fall_back(gated)
    assert not c.can_fall_back(OpenRouterError("m", status=400))
    assert not c.can_fall_back(ClientError("503"))


async def test_all_free_candidates_unavailable_uses_the_pin(tmp_path):
    c = make_client(FakeOpenRouter(replies=[(403, GATED_403)]), tmp_path)
    for model in list(c.free_candidates):
        with pytest.raises(OpenRouterError):
            await c.ask(c.get_or_create_session("a", model), "q")
    assert c.free_candidates == [] and c.default_model == PINNED_FREE_MODEL
