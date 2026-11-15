"""Shared fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point __main__._LOG_DIR at tmp_path for every test (#136, #144; from PR #137).

    Importing the entry point no longer configures logging, so this guards only a test that
    calls _setup_log_file() or _configure_logging() directly: it must never touch $HOME.
    """
    monkeypatch.setattr("sidekick.__main__._LOG_DIR", tmp_path / "logs")


@pytest.fixture(autouse=True)
def _no_plugin_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real plugin option in the developer's environment must never leak into a test (#116)."""
    monkeypatch.delenv("SIDEKICK_API_KEY", raising=False)
    monkeypatch.delenv("SIDEKICK_SA_FILE", raising=False)
    monkeypatch.delenv("SIDEKICK_OPENROUTER_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
