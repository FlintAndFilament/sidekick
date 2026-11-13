from unittest.mock import patch

import pytest

from sidekick.backends import Surface
from sidekick.client import ClientError, GeminiClient
from sidekick.config import Config
from sidekick.models import is_openrouter_id
from sidekick.router import Router, route
from tests._openrouter_fixtures import FakeOpenRouter, entry, make_client


def _gemini() -> GeminiClient:
    with patch("google.genai.Client"):
        return GeminiClient(Config(auth={"method": "api_key"}), api_key="k")


@pytest.mark.parametrize(
    "model,has_gemini,want",
    [
        (None, True, "openrouter"),
        ("", True, "openrouter"),
        ("x-ai/grok-4.7", True, "openrouter"),
        ("flash", True, "gemini"),
        ("gemini-3.5-flash", True, "gemini"),
        ("models/gemini-3.5-flash", True, "gemini"),
        ("flash", False, "openrouter"),
        ("gemini-3.5-flash", False, "openrouter"),
    ],
)
def test_route(model, has_gemini, want):
    assert route(model, has_gemini=has_gemini) == want


def test_is_openrouter_id():
    assert is_openrouter_id("x-ai/grok-4.7")
    assert not any(
        map(
            is_openrouter_id,
            ["flash", "models/gemini-3.5-flash", "publishers/google/models/gemini-3.5-flash"],
        )
    )


def test_gemini_only_server_refuses_openrouter_ids():
    with pytest.raises(ClientError, match="no OpenRouter key is set"):
        _gemini().backend_for("x-ai/grok-4.7")
    g = _gemini()
    assert g.backend_for("models/gemini-3.5-flash") is g


def test_router_wiring(tmp_path):
    o, g = make_client(FakeOpenRouter(), tmp_path), _gemini()
    r = Router(o, g)
    assert r.backends() == [o, g] and r.backend_for(None) is o and r.backend_for("pro") is g
    assert r.surface == Surface("openrouter", False, False, False, True)
    assert Router(o).surface.gemini_available is False and Router(o).backend_for("pro") is o


def test_router_properties(tmp_path):
    o, g = make_client(FakeOpenRouter(), tmp_path), _gemini()
    r = Router(o, g, gemini_problem="no Gemini key")
    assert r.openrouter is o and r.gemini_problem == "no Gemini key"
    assert r.auth_method == "openrouter" and r.web_supported is False
    assert r.default_model == o.default_model and r.web_default is o.web_default
    assert r.default_thinking == o.default_thinking
    assert r.resolved_latest == g.resolved_latest and Router(o).resolved_latest == {}
    assert Router(o).gemini_problem is None


def test_sessions_merge_most_recent_first(tmp_path):
    o, g = make_client(FakeOpenRouter(), tmp_path), _gemini()
    so, sg = o.get_or_create_session("ask:a", "x-ai/grok-4.7"), g.get_or_create_session("ask:b")
    so.used_at, sg.used_at = 2.0, 1.0
    assert Router(o, g).sessions() == [so, sg]
    so.used_at = 0.5
    assert Router(o, g).sessions() == [sg, so]


def test_model_hint(tmp_path):
    o = make_client(FakeOpenRouter(), tmp_path)
    assert "x-ai/grok-4.7" in Router(o).model_hint()
    assert "conversation only" in Router(o).model_hint()
    assert "read this repository" not in Router(o).model_hint()
    tooled = make_client(FakeOpenRouter(catalog=[entry(o.default_model, tools=True)]), tmp_path)
    hint = Router(tooled).model_hint()
    assert "OpenRouter models that support tools read this repository; none have web" in hint
    assert "conversation only" not in hint
    assert "google/<id> through OpenRouter" in Router(o).model_hint()
    assert "runs on Gemini directly" in Router(o, _gemini()).model_hint()
    assert Router(o).model_hint().endswith(" list_models shows prices.")
