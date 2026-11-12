"""Tests for list_models' OpenRouter section (prices, privacy check, search) and merged sessions."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP

from sidekick.client import GeminiClient
from sidekick.config import Config
from sidekick.openrouter import ChatMessage
from sidekick.openrouter_catalog import PINNED_FREE_MODEL, SETUP_PICKS
from sidekick.router import Router
from sidekick.tools import list_models as lm
from sidekick.tools import list_sessions as ls
from tests._openrouter_fixtures import DATA_POLICY_404, FakeOpenRouter, entry, make_client, reply


def _meta(name: str, display: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        name=name, display_name=display, supported_actions=["generateContent", "countTokens"]
    )


def _gemini_with_catalog() -> GeminiClient:
    with patch("google.genai.Client"):
        client = GeminiClient(Config(auth={"method": "api_key"}), api_key="k")
    client.list_models = lambda: [_meta("models/gemini-3.5-flash", "Gemini 3.5 Flash")]  # type: ignore[method-assign]
    return client


async def _call(source: Any, **args: Any) -> str:
    mcp = FastMCP("t")
    lm.register(mcp, source, None)  # type: ignore[arg-type]  # transcript is unused
    result = await mcp.call_tool("list_models", args)
    content = result[0] if isinstance(result, tuple) else result
    return content[0].text  # type: ignore[index,no-any-return]


def _line(text: str, model_id: str) -> str:
    return next(ln for ln in text.splitlines() if model_id in ln)


async def test_featured_lists_setup_picks_then_free_with_prices(tmp_path):
    out = await _call(
        Router(make_client(FakeOpenRouter(), tmp_path, default_model="x-ai/grok-4.7"))
    )
    assert "OpenRouter models (USD per 1M tokens, input / output, from the live catalog):" in out
    for model_id, _ in SETUP_PICKS:
        assert model_id in out
    line = _line(out, "deepseek/deepseek-v4.1-flash-1017")
    assert "$0.10 / $0.29" in line and "best value (Artificial Analysis 33)" in line
    nemo = _line(out, "nvidia/nemotron-3-ultra-550b-a55b:free")
    assert "free" in nemo and "(free default)" in nemo and "1M ctx" in nemo
    assert "more text models: pass query=" in out


async def test_default_is_marked(tmp_path):
    out = await _call(
        Router(make_client(FakeOpenRouter(), tmp_path, default_model="x-ai/grok-4.7"))
    )
    assert "(default)" in _line(out, "x-ai/grok-4.7")


async def test_default_that_is_the_free_default_shows_default_only(tmp_path):
    out = await _call(Router(make_client(FakeOpenRouter(), tmp_path)))
    nemo = _line(out, "nvidia/nemotron-3-ultra-550b-a55b:free")
    assert "(default)" in nemo and "(free default)" not in nemo


async def test_privacy_marker_appears_after_check(tmp_path):
    r = Router(make_client(FakeOpenRouter(replies=[(200, reply())]), tmp_path))
    assert "privacy: not checked" in _line(await _call(r), "x-ai/grok-4.7")
    await _call(r, check="x-ai/grok-4.7")
    assert "privacy: some providers may store/train" in _line(await _call(r), "x-ai/grok-4.7")


async def test_check_probes_and_explains(tmp_path):
    fake = FakeOpenRouter(replies=[(404, DATA_POLICY_404)])
    out = await _call(
        Router(make_client(fake, tmp_path)), check="nvidia/nemotron-3-ultra-550b-a55b:free"
    )
    assert "Every provider serving it may store or train on your prompts" in out
    assert "20 requests a minute" in out
    assert "privacy: every provider may store/train" in out
    assert fake.posts[-1]["provider"] == {"data_collection": "deny"}


async def test_check_unknown_id_sends_nothing(tmp_path):
    fake = FakeOpenRouter()
    out = await _call(Router(make_client(fake, tmp_path)), check="acme/nope")
    assert "is not in OpenRouter's catalog" in out and fake.posts == []


async def test_check_bad_key_is_an_error(tmp_path):
    fake = FakeOpenRouter(replies=[(401, {"error": {"code": 401, "message": "bad key"}})])
    out = await _call(Router(make_client(fake, tmp_path)), check="x-ai/grok-4.7")
    assert out.startswith("[sidekick error]")


async def test_query_filters_text_models(tmp_path):
    out = await _call(Router(make_client(FakeOpenRouter(), tmp_path)), query="GROK")
    assert "x-ai/grok-4.7" in out and "moonshotai/kimi-k3.1-pro" not in out


async def test_query_shows_zero_priced_router_without_free_tag(tmp_path):
    out = await _call(Router(make_client(FakeOpenRouter(), tmp_path)), query="openrouter")
    assert "openrouter/auto" not in out
    line = _line(out, "openrouter/free")
    assert "$0 / $0" in line and "(free default)" not in line


async def test_query_no_match(tmp_path):
    out = await _call(Router(make_client(FakeOpenRouter(), tmp_path)), query="zzz")
    assert "No OpenRouter text model matches 'zzz'." in out


async def test_query_caps_rows(tmp_path):
    fake = FakeOpenRouter(catalog=[entry(f"acme/m-{i:02d}") for i in range(45)])
    out = await _call(Router(make_client(fake, tmp_path)), query="acme")
    assert "acme/m-39" in out and "acme/m-40" not in out
    assert "…and 5 more; narrow the query." in out


async def test_mixed_server_lists_openrouter_then_gemini(tmp_path):
    out = await _call(Router(make_client(FakeOpenRouter(), tmp_path), _gemini_with_catalog()))
    assert out.index("OpenRouter models") < out.index("Gemini chat models")
    assert "Omit model to use the OpenRouter default (" in out
    assert "Omit to use the default (" not in out


async def test_gemini_only_server_ignores_query_with_notice():
    out = await _call(_gemini_with_catalog(), query="grok")
    assert out.startswith("[sidekick notice] query= and check= apply to OpenRouter models only")
    assert "Gemini chat models" in out


async def test_catalog_unavailable_says_prices_unknown(tmp_path):
    out = await _call(Router(make_client(FakeOpenRouter(catalog_status=500), tmp_path)))
    assert "catalog is unavailable right now, so prices are unknown" in out
    assert PINNED_FREE_MODEL in out


async def test_refresh_refetches_catalog(tmp_path):
    fake = FakeOpenRouter()
    await _call(Router(make_client(fake, tmp_path)), refresh=True)
    assert fake.gets == 2


async def test_list_sessions_shows_both_backends(tmp_path):
    o = make_client(FakeOpenRouter(), tmp_path)
    g = _gemini_with_catalog()
    s = o.get_or_create_session("ask:a", "x-ai/grok-4.7")
    s.history += [ChatMessage("user", "q"), ChatMessage("assistant", "a")]
    g.get_or_create_session("review:b")  # created later, so listed first
    out = ls.format_session_list(Router(o, g).sessions())
    rows = [ln.split() for ln in out.splitlines() if ln.startswith("  ")]
    assert rows[1][0] == "review" and rows[1][1] == "b"
    assert rows[2] == ["ask", "a", "x-ai/grok-4.7", "1"]
