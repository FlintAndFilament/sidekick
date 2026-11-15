import json
import logging
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from sidekick.backends import Backend, ModelSource, Preflight, Surface
from sidekick.client import (
    FALLBACK_MODEL,
    GEMINI_DATA_USE_DECLINED,
    GEMINI_DATA_USE_QUESTION,
    GEMINI_DATA_USE_UNASKED,
    ClientError,
    GeminiClient,
)
from sidekick.config import Config


def _gemini(
    api_key: bool = True,
    consent: bool = False,
    config_path: Optional[Path] = None,
    accepted: bool = False,
) -> GeminiClient:
    with patch("google.genai.Client"):
        if api_key:
            return GeminiClient(
                Config(auth={"method": "api_key"}, gemini_data_use_accepted=accepted),
                api_key="k",
                needs_data_use_consent=consent,
                config_path=config_path or Path("/nonexistent/sidekick-test/config.json"),
            )
        return GeminiClient(Config(project="p"), MagicMock())


def _asker(*outcomes: str):  # type: ignore[no-untyped-def]
    asked: list[str] = []

    async def elicit(message: str):  # type: ignore[no-untyped-def]
        asked.append(message)
        return outcomes[min(len(asked), len(outcomes)) - 1]

    return elicit, asked


def test_gemini_client_is_a_backend_and_a_model_source():
    c = _gemini()
    assert isinstance(c, Backend) and isinstance(c, ModelSource)
    assert c.backends() == [c] and c.backend_for(None) is c
    assert c.name == "gemini" and c.label == "api_key"


def test_fallback_rule_is_unchanged():
    c = _gemini()
    assert c.fallback_for("gemini-3.5-flash") == FALLBACK_MODEL
    assert c.fallback_for(FALLBACK_MODEL) is None
    assert c.can_fall_back(ClientError("503 UNAVAILABLE"))
    assert not c.can_fall_back(ClientError("400 bad"))


def test_question_and_refusal_texts_are_exact():
    assert GEMINI_DATA_USE_QUESTION == (
        "Use Gemini through your API key? If the key's Google Cloud project has no billing, "
        "Google may use your prompts and responses, including any code sent with them, to "
        "improve its products. block_data_collection does not apply to Gemini."
    )
    assert GEMINI_DATA_USE_DECLINED == (
        "[sidekick] Not sent: you declined Gemini's data-use terms. Use an OpenRouter model, "
        "or a Gemini key whose project has billing (then accept)."
    )
    assert GEMINI_DATA_USE_UNASKED == (
        "[sidekick] Not sent: sidekick couldn't ask you to accept Gemini's data-use terms "
        "here. Accept from an interactive session, or set gemini_data_use_accepted: true in "
        "~/.config/sidekick/config.json — only the user should make this change (takes effect "
        "after /reload-plugins)."
    )


async def test_accept_proceeds_remembers_and_asks_once(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"auth": {"method": "api_key"}}))
    c = _gemini(consent=True, config_path=path)
    elicit, asked = _asker("accept")
    first = await c.preflight("gemini-3.5-flash", elicit)
    second = await c.preflight("gemini-3.5-flash", elicit)
    assert first == Preflight(proceed=True) and second == Preflight(proceed=True)
    assert asked == [GEMINI_DATA_USE_QUESTION]
    saved = json.loads(path.read_text())
    assert saved == {"auth": {"method": "api_key"}, "gemini_data_use_accepted": True}


@pytest.mark.parametrize("outcome", ["decline", "cancel"])
async def test_decline_refuses_and_asks_again_next_call(tmp_path: Path, outcome: str) -> None:
    path = tmp_path / "config.json"
    c = _gemini(consent=True, config_path=path)
    elicit, asked = _asker(outcome)
    first = await c.preflight("gemini-3.5-flash", elicit)
    second = await c.preflight("gemini-3.5-flash", elicit)
    assert first == Preflight(False, message=GEMINI_DATA_USE_DECLINED) == second
    assert len(asked) == 2
    assert not path.exists()


async def test_cannot_ask_refuses(tmp_path: Path) -> None:
    c = _gemini(consent=True, config_path=tmp_path / "config.json")
    assert await c.preflight("gemini-3.5-flash", None) == Preflight(
        False, message=GEMINI_DATA_USE_UNASKED
    )
    elicit, _ = _asker("unavailable")
    assert await c.preflight("gemini-3.5-flash", elicit) == Preflight(
        False, message=GEMINI_DATA_USE_UNASKED
    )
    assert not (tmp_path / "config.json").exists()


async def test_adc_client_never_asks() -> None:
    c = _gemini(api_key=False)
    elicit, asked = _asker("decline")
    assert await c.preflight("gemini-3.5-flash", elicit) == Preflight(proceed=True)
    assert await c.preflight("gemini-3.5-flash", None) == Preflight(proceed=True)
    assert asked == []


async def test_already_accepted_in_config_asks_nothing(tmp_path: Path) -> None:
    c = _gemini(consent=True, config_path=tmp_path / "config.json", accepted=True)
    elicit, asked = _asker("decline")
    assert await c.preflight("gemini-3.5-flash", elicit) == Preflight(proceed=True)
    assert await c.preflight("gemini-3.5-flash", None) == Preflight(proceed=True)
    assert asked == []


async def test_unwritable_config_on_accept_proceeds_and_warns_type_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json")  # never clobbered: the writer refuses with ConfigError
    c = _gemini(consent=True, config_path=path)
    elicit, asked = _asker("accept")
    with caplog.at_level(logging.WARNING, logger="sidekick.client"):
        assert await c.preflight("gemini-3.5-flash", elicit) == Preflight(proceed=True)
        assert await c.preflight("gemini-3.5-flash", elicit) == Preflight(proceed=True)
    assert len(asked) == 1  # kept in-process
    assert path.read_text() == "{not json"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ConfigError" in w for w in warnings)
    assert not any(str(tmp_path) in w or "not json" in w for w in warnings)


async def test_gemini_preflight_always_allows_tools():
    pre = await _gemini().preflight("gemini-3.5-flash", None, want_tools=True)
    assert pre.proceed and pre.tools is True


async def test_default_construction_asks_nothing():  # existing tests see no change
    assert await _gemini().preflight("gemini-3.5-flash", None) == Preflight(proceed=True)


def test_surfaces():
    assert _gemini().surface == Surface(
        primary="gemini", files=True, web_supported=True, web_default=False, gemini_available=True
    )
    assert _gemini(api_key=False).surface.web_supported is False


def test_session_hit_refreshes_used_at():
    c = _gemini()
    s = c.get_or_create_session("ask:a")
    s.used_at = 0.0
    assert c.get_or_create_session("ask:a") is s and s.used_at > 0.0


def test_model_hint_is_todays_schema_hint():
    c = _gemini()
    assert c.model_hint().startswith("Optional Gemini model.")


def test_gemini_tools_supported_true():
    assert _gemini().tools_supported("gemini-3.5-flash") is True
