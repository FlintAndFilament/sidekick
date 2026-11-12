"""call_gemini through the Router: which backend answers, what it may use, what it says."""

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from sidekick.client import GEMINI_UNPAID_NOTICE, GeminiClient
from sidekick.config import Config
from sidekick.router import Router
from sidekick.tools.base import _MAX_PREFLIGHTS, call_gemini
from sidekick.transcript import TranscriptWriter
from tests._openrouter_fixtures import FakeOpenRouter, make_client, reply
from tests.test_capability_metadata import _workspace
from tests.test_tools import _register_all_tools, _text_response

GROK, KIMI = "x-ai/grok-4.7", "moonshotai/kimi-k3.1-pro"
NEMO, GEMMA = "nvidia/nemotron-3-ultra-550b-a55b:free", "google/gemma-4-31b-it:free"
INKLING = "thinkingmachines/inkling-2-small:free"
RATE_LIMITED = (429, {"error": {"code": 429, "message": "rate limited"}})


def _gemini() -> tuple[GeminiClient, AsyncMock]:
    with patch("google.genai.Client"):
        g = GeminiClient(Config(auth={"method": "api_key"}), api_key="k")
    gen = AsyncMock(return_value=_text_response("from gemini"))
    g._raw_client.aio.models.generate_content = gen
    return g, gen


def _router(
    tmp_path: Path, replies: list[tuple[int, Any]], *, accepted_models: list[str], **openrouter: Any
) -> tuple[Router, FakeOpenRouter, AsyncMock]:
    fake = FakeOpenRouter(replies=replies)
    o = make_client(fake, tmp_path, accepted_models=accepted_models, **openrouter)
    g, gen = _gemini()
    return Router(o, g), fake, gen


async def _call(source: Any, tmp_path: Path, **kw: Any) -> str:
    tr = TranscriptWriter(str(tmp_path / "t"), datetime.now())
    ws = _workspace(tmp_path)
    return await call_gemini(
        client=source,
        transcript=tr,
        tool_name="review",
        session_name="s",
        system_instruction="Be terse.",
        prompt="p",
        thinking=None,
        workspace=ws,
        **kw,
    )


def _asker(*outcomes: str):  # type: ignore[no-untyped-def]
    asked: list[str] = []

    async def elicit(message: str):  # type: ignore[no-untyped-def]
        asked.append(message)
        return outcomes[min(len(asked), len(outcomes)) - 1]

    return elicit, asked


def _transcript_written(tmp_path: Path) -> bool:
    return any((tmp_path / "t").iterdir())


async def test_default_call_goes_to_openrouter_without_file_tools(tmp_path):
    r, fake, gen = _router(tmp_path, [(200, reply())], accepted_models=[NEMO])
    answer = await _call(r, tmp_path, model=None)
    assert answer == "fine"
    assert len(fake.posts) == 1 and fake.posts[0]["model"] == NEMO
    system = fake.posts[0]["messages"][0]
    assert system["role"] == "system" and "list_dir" not in system["content"]
    gen.assert_not_awaited()


async def test_gemini_alias_goes_to_gemini_with_file_tools(tmp_path):
    r, fake, gen = _router(tmp_path, [(200, reply())], accepted_models=[NEMO])
    answer = await _call(r, tmp_path, model="flash")
    assert answer == "from gemini"
    assert fake.posts == []
    config = gen.call_args.kwargs["config"]
    assert config.tools and config.tools[0].function_declarations
    assert "list_dir" in {d.name for d in config.tools[0].function_declarations}


async def test_per_call_openrouter_model_notes_it_could_not_read_files(tmp_path):
    r, fake, _ = _router(tmp_path, [(200, reply())], accepted_models=[GROK])
    answer = await _call(r, tmp_path, model=GROK)
    assert answer.startswith(
        "[sidekick notice] `x-ai/grok-4.7` runs on OpenRouter and could not read repository files"
    )
    assert answer.endswith("fine") and fake.posts[0]["model"] == GROK


async def test_omitted_model_has_no_files_notice(tmp_path):
    r, _, _ = _router(tmp_path, [(200, reply())], accepted_models=[NEMO])
    answer = await _call(r, tmp_path, model=None)
    assert "could not read repository files" not in answer


async def test_declined_model_sends_nothing_and_logs_nothing(tmp_path):
    r, fake, _ = _router(tmp_path, [(200, reply())], accepted_models=[])
    elicit, asked = _asker("decline")
    answer = await _call(r, tmp_path, model=GROK, elicit=elicit)
    assert answer.startswith("[sidekick] Not sent") and len(asked) == 1
    assert len(fake.posts) == 1  # the privacy probe, never the prompt
    assert fake.posts[0]["max_tokens"] == 16
    assert not _transcript_written(tmp_path)
    assert all(s.history == [] for s in r.sessions())


async def test_web_true_on_openrouter_is_ignored_with_notice(tmp_path):
    r, fake, _ = _router(tmp_path, [(200, reply())], accepted_models=[NEMO])
    answer = await _call(r, tmp_path, web=True)
    assert "unavailable on the OpenRouter backend" in answer and answer.endswith("fine")
    assert "tools" not in fake.posts[-1]
    assert "search the web" not in fake.posts[-1]["messages"][0]["content"]


async def test_free_model_429_falls_back_once_with_both_notices(tmp_path):
    script = [RATE_LIMITED, RATE_LIMITED, (200, reply("ok")), (200, reply())]
    r, fake, _ = _router(tmp_path, script, accepted_models=[NEMO])
    answer = await _call(r, tmp_path)
    assert f"Model '{NEMO}' was unavailable (503/429)" in answer
    assert f"fallback model '{GEMMA}'" in answer
    assert f"First use of the free model `{GEMMA}`" in answer
    assert answer.endswith("fine")
    assert [p["model"] for p in fake.posts] == [NEMO, NEMO, GEMMA, GEMMA]
    session = next(s for s in r.sessions() if s.name == "review:s")
    assert len(session.history) == 2
    assert [s.name for s in r.sessions()] == ["review:s"]  # no fallback-model session


async def test_paid_model_429_does_not_switch(tmp_path):
    r, fake, _ = _router(tmp_path, [RATE_LIMITED], accepted_models=[GROK])
    answer = await _call(r, tmp_path, model=GROK)
    assert answer.startswith("[sidekick error]")
    assert fake.posts and all(p["model"] == GROK for p in fake.posts)


async def test_two_openrouter_models_same_session_name_each_answer(tmp_path):
    script = [(200, reply("grok says")), (200, reply("kimi says"))]
    r, fake, _ = _router(tmp_path, script, accepted_models=[GROK, KIMI])
    first = await _call(r, tmp_path, model=GROK)
    second = await _call(r, tmp_path, model=KIMI)
    assert first.endswith("grok says") and second.endswith("kimi says")
    assert [p["model"] for p in fake.posts] == [GROK, KIMI]
    assert {s.model for s in r.sessions()} == {GROK, KIMI}
    assert len(r.sessions()) == 2


async def test_gemini_only_server_refuses_openrouter_model_politely(tmp_path):
    g, gen = _gemini()
    answer = await _call(g, tmp_path, model=GROK)
    assert answer.startswith("[sidekick error]") and "no OpenRouter key is set" in answer
    gen.assert_not_awaited()
    assert not _transcript_written(tmp_path)


async def test_rotated_default_is_preflighted_before_send(tmp_path):
    """Concurrent calls re-point the shared default session twice while this one is asking."""
    r, fake, _ = _router(tmp_path, [(200, reply("ok"))] * 3 + [(200, reply())], accepted_models=[])
    asked: list[str] = []

    async def elicit(message: str):  # type: ignore[no-untyped-def]
        asked.append(message)
        assert all(p["max_tokens"] == 16 for p in fake.posts)  # nothing but probes so far
        if len(asked) < 3:
            # Meanwhile the current default cools and another call re-points "review:s".
            r.openrouter._cooling[[NEMO, GEMMA][len(asked) - 1]] = time.monotonic() + 600
            r.openrouter.get_or_create_session("review:s")
        return "accept"

    answer = await _call(r, tmp_path, elicit=elicit)
    assert [m for q in asked for m in (NEMO, GEMMA, INKLING) if m in q] == [NEMO, GEMMA, INKLING]
    assert answer == "fine"
    assert [p["model"] for p in fake.posts] == [NEMO, GEMMA, INKLING, INKLING]
    assert [p["max_tokens"] == 16 for p in fake.posts] == [True, True, True, False]


async def test_rotated_default_refused_sends_nothing(tmp_path):
    r, fake, _ = _router(tmp_path, [(200, reply("ok"))] * 2, accepted_models=[])
    elicit, asked = _asker("accept", "decline")
    rotate = elicit

    async def rotating(message: str):  # type: ignore[no-untyped-def]
        r.openrouter._cooling[NEMO] = time.monotonic() + 600
        r.openrouter.get_or_create_session("review:s")
        return await rotate(message)

    answer = await _call(r, tmp_path, elicit=rotating)
    assert len(asked) == 2 and GEMMA in asked[1]
    assert answer.startswith("[sidekick] Not sent") and GEMMA in answer
    assert all(p["max_tokens"] == 16 for p in fake.posts)  # only the two probes went out
    assert not _transcript_written(tmp_path)


async def test_endlessly_rotating_default_gives_up_and_sends_nothing(tmp_path):
    r, fake, _ = _router(tmp_path, [(200, reply("ok"))], accepted_models=[])
    asked: list[str] = []

    async def elicit(message: str):  # type: ignore[no-untyped-def]
        asked.append(message)
        r.openrouter.get_or_create_session("review:s").model = f"acme/model-{len(asked)}"
        return "accept"

    answer = await _call(r, tmp_path, elicit=elicit)
    assert answer.startswith("[sidekick error]") and "nothing was sent" in answer
    assert len(asked) == _MAX_PREFLIGHTS
    assert fake.posts and all(p["max_tokens"] == 16 for p in fake.posts)
    assert not _transcript_written(tmp_path)


async def test_gemini_unpaid_notice_survives_a_failed_call(tmp_path):
    with patch("google.genai.Client"):
        g = GeminiClient(Config(auth={"method": "api_key"}), api_key="k", unpaid_tier_notice=True)
    g._raw_client.aio.models.generate_content = AsyncMock(side_effect=Exception("API down"))
    answer = await _call(g, tmp_path)
    assert answer.startswith(GEMINI_UNPAID_NOTICE + "[sidekick error]")


def test_injected_context_is_not_a_tool_parameter(tmp_path):
    mcp = _register_all_tools(tmp_path)
    tools = asyncio.run(mcp.list_tools())  # type: ignore[attr-defined]
    assert len(tools) == 5
    for tool in tools:
        assert "ctx" not in tool.inputSchema["properties"], tool.name
