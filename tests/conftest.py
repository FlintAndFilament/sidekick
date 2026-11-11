"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _no_plugin_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real plugin key in the developer's environment must never leak into a test (#116)."""
    monkeypatch.delenv("SIDEKICK_API_KEY", raising=False)
