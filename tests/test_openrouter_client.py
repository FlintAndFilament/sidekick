import logging

import httpx
import pytest

from sidekick.client import MAX_SESSIONS, ClientError
from sidekick.openrouter import ChatMessage, OpenRouterError
from sidekick.openrouter_catalog import PINNED_FREE_MODEL
from tests._fakes import FAKE_OPENROUTER_KEY
from tests._openrouter_fixtures import GATED_403, FakeOpenRouter, make_client, reply

GROK = "x-ai/grok-4.7"


async def test_chat_request_shape(tmp_path):
    fake = FakeOpenRouter(replies=[(200, reply("fine"))])
    c = make_client(fake, tmp_path)
    s = c.get_or_create_session("ask:default", GROK)
    assert await c.ask(s, "Is this safe?", "low", system_instruction="Be terse.") == "fine"
    req, body = fake.requests[-1], fake.posts[-1]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {FAKE_OPENROUTER_KEY}"
    assert req.headers["http-referer"] == "https://github.com/FlintAndFilament/sidekick"
    assert req.headers["x-openrouter-title"] == "sidekick"
    assert body["model"] == GROK and body["max_tokens"] == 16000
    assert body["messages"] == [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "Is this safe?"},
    ]
    assert body["reasoning"] == {"effort": "low", "exclude": True} and "provider" not in body


def test_catalog_request_carries_no_key(tmp_path):
    fake = FakeOpenRouter()
    make_client(fake, tmp_path)
    assert "authorization" not in fake.requests[0].headers


async def test_history_is_committed_only_on_success(tmp_path):
    fake = FakeOpenRouter(
        replies=[
            (200, reply("a1")),
            (400, {"error": {"code": 400, "message": "bad"}}),
            (200, reply("a3")),
        ]
    )
    c = make_client(fake, tmp_path)
    s = c.get_or_create_session("ask:x", GROK)
    await c.ask(s, "q1")
    with pytest.raises(OpenRouterError, match="HTTP 400"):
        await c.ask(s, "q2")
    assert s.history == [ChatMessage("user", "q1"), ChatMessage("assistant", "a1")]
    await c.ask(s, "q3")
    assert [m["content"] for m in fake.posts[-1]["messages"]] == ["q1", "a1", "q3"]


async def test_max_tokens_capped_by_model_limit_and_reasoning_omitted(tmp_path):
    fake = FakeOpenRouter(replies=[(200, reply())])
    c = make_client(fake, tmp_path)
    await c.ask(c.get_or_create_session("a", "google/gemma-4-31b-it:free"), "q", "high")
    assert fake.posts[-1]["max_tokens"] == 8192 and "reasoning" not in fake.posts[-1]


async def test_empty_answer_after_length_is_a_clear_error(tmp_path):
    c = make_client(FakeOpenRouter(replies=[(200, reply("", "length"))]), tmp_path)
    with pytest.raises(ClientError) as err:
        await c.ask(c.get_or_create_session("a", GROK), "q")
    assert "whole output budget" in str(err.value) and "max_tokens=16000" in str(err.value)
    assert "thinking='low'" in str(err.value)


async def test_empty_answer_other_reason(tmp_path):
    c = make_client(FakeOpenRouter(replies=[(200, reply(None, "stop"))]), tmp_path)
    with pytest.raises(ClientError, match="empty answer"):
        await c.ask(c.get_or_create_session("a", GROK), "q")


async def test_paid_model_retries_429_then_succeeds(tmp_path):
    fake = FakeOpenRouter(
        replies=[(429, {"error": {"code": 429, "message": "slow"}})] * 2 + [(200, reply("ok"))]
    )
    c = make_client(fake, tmp_path)
    assert await c.ask(c.get_or_create_session("a", GROK), "q") == "ok"
    assert len(fake.posts) == 3 and len(fake.sleeps) == 2


async def test_free_model_retries_once(tmp_path):
    fake = FakeOpenRouter(replies=[(429, {"error": {"code": 429, "message": "slow"}})])
    c = make_client(fake, tmp_path)
    with pytest.raises(OpenRouterError) as err:
        await c.ask(c.get_or_create_session("a", "nvidia/nemotron-3-ultra-550b-a55b:free"), "q")
    assert err.value.status == 429 and len(fake.posts) == 2


async def test_401_points_at_the_plugin_option_and_never_shows_the_key(tmp_path):
    c = make_client(
        FakeOpenRouter(replies=[(401, {"error": {"code": 401, "message": "No auth"}})]), tmp_path
    )
    with pytest.raises(OpenRouterError) as err:
        await c.ask(c.get_or_create_session("a", GROK), "q")
    assert "OpenRouter API key" in str(err.value) and "/reload-plugins" in str(err.value)
    assert FAKE_OPENROUTER_KEY not in str(err.value)


async def test_402_says_out_of_credits(tmp_path):
    c = make_client(
        FakeOpenRouter(replies=[(402, {"error": {"code": 402, "message": "x"}})]), tmp_path
    )
    with pytest.raises(OpenRouterError, match="credits"):
        await c.ask(c.get_or_create_session("a", GROK), "q")


async def test_html_error_body_is_a_client_error(tmp_path):
    c = make_client(FakeOpenRouter(replies=[(502, "<html>Bad gateway</html>")]), tmp_path)
    with pytest.raises(OpenRouterError) as err:
        await c.ask(c.get_or_create_session("a", GROK), "q")
    assert err.value.status == 502 and "HTTP 502" in str(err.value)


async def test_200_with_error_object_is_a_client_error(tmp_path):
    c = make_client(
        FakeOpenRouter(replies=[(200, {"error": {"code": 500, "message": "upstream died"}})]),
        tmp_path,
    )
    with pytest.raises(OpenRouterError, match="upstream died"):
        await c.ask(c.get_or_create_session("a", GROK), "q")


async def test_routing_metadata_is_kept(tmp_path):
    c = make_client(FakeOpenRouter(replies=[(403, GATED_403)]), tmp_path)
    with pytest.raises(OpenRouterError) as err:
        await c.ask(c.get_or_create_session("a", "thinkingmachines/inkling-2-small:free"), "q")
    assert err.value.gated and not err.value.data_policy
    assert OpenRouterError("m", status=404, routing_step="Filter by Data Policy").data_policy


async def test_network_error_is_a_client_error(tmp_path):
    c = make_client(FakeOpenRouter(replies=[(0, httpx.ConnectError("boom"))]), tmp_path)
    with pytest.raises(ClientError, match="Could not reach OpenRouter"):
        await c.ask(c.get_or_create_session("a", GROK), "q")


def test_catalog_unreadable_keeps_pinned_default(tmp_path):
    c = make_client(FakeOpenRouter(catalog_status=500), tmp_path)
    assert c.catalog_loaded is False and c.default_model == PINNED_FREE_MODEL
    assert c.resolve_model("flash") == "google/gemini-3.5-flash"


def test_default_model_resolution(tmp_path):
    assert make_client(FakeOpenRouter(), tmp_path, default_model=GROK).default_model == GROK
    c = make_client(FakeOpenRouter(), tmp_path)
    assert c.default_model == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert c.resolve_model("gemini-3.5-flash") == "google/gemini-3.5-flash"
    assert c.resolve_model("flash") == "google/gemini-3.8-flash" and c.resolve_model(GROK) == GROK


def test_default_session_follows_the_default_model(tmp_path):
    c = make_client(FakeOpenRouter(), tmp_path)
    s = c.get_or_create_session("ask:x")
    s.history.append(ChatMessage("user", "q"))
    c._config.openrouter.default_model = GROK
    again = c.get_or_create_session("ask:x")
    assert again is s and s.model == GROK and len(s.history) == 1


def test_sessions_are_per_explicit_model_and_capped(tmp_path):
    c = make_client(FakeOpenRouter(), tmp_path)
    assert c.get_or_create_session("ask:x", GROK) is not c.get_or_create_session(
        "ask:x", "moonshotai/kimi-k3.1-pro"
    )
    for i in range(MAX_SESSIONS + 1):
        c.get_or_create_session(f"s{i}", GROK)
    assert len(c.sessions()) == MAX_SESSIONS


async def test_key_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    c = make_client(
        FakeOpenRouter(replies=[(401, {"error": {"code": 401, "message": "x"}})]), tmp_path
    )
    with pytest.raises(OpenRouterError):
        await c.ask(c.get_or_create_session("a", GROK), "q")
    assert FAKE_OPENROUTER_KEY not in caplog.text


async def test_read_timeout_is_not_retried_so_a_paid_call_bills_once(tmp_path):
    exc = httpx.ReadTimeout("slow")
    fake = FakeOpenRouter(replies=[(0, exc), (200, reply())])
    c = make_client(fake, tmp_path)
    with pytest.raises(OpenRouterError, match="Could not reach OpenRouter"):
        await c.ask(c.get_or_create_session("a", GROK), "q")
    assert len(fake.posts) == 1


async def test_connect_error_is_still_retried(tmp_path):
    fake = FakeOpenRouter(replies=[(0, httpx.ConnectError("boom")), (200, reply())])
    c = make_client(fake, tmp_path)
    await c.ask(c.get_or_create_session("a", GROK), "q")
    assert len(fake.posts) == 2
