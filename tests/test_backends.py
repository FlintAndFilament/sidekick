from unittest.mock import MagicMock, patch

from sidekick.backends import Backend, ModelSource, Preflight, Surface
from sidekick.client import FALLBACK_MODEL, GEMINI_UNPAID_NOTICE, ClientError, GeminiClient
from sidekick.config import Config


def _gemini(api_key: bool = True, notice: bool = False) -> GeminiClient:
    with patch("google.genai.Client"):
        if api_key:
            return GeminiClient(
                Config(auth={"method": "api_key"}), api_key="k", unpaid_tier_notice=notice
            )
        return GeminiClient(Config(project="p"), MagicMock())


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


async def test_unpaid_tier_notice_once_when_enabled():
    c = _gemini(notice=True)
    first = await c.preflight("gemini-3.5-flash", None)
    second = await c.preflight("gemini-3.5-flash", None)
    assert first.proceed and first.notice == GEMINI_UNPAID_NOTICE
    assert "has no billing, Google may use your prompts" in GEMINI_UNPAID_NOTICE
    assert "block_data_collection does not apply to Gemini" in GEMINI_UNPAID_NOTICE
    assert second == Preflight(proceed=True)


async def test_default_construction_has_no_notice():  # existing tests see no change
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
