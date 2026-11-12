"""Shared fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_plugin_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real plugin option in the developer's environment must never leak into a test (#116)."""
    monkeypatch.delenv("SIDEKICK_API_KEY", raising=False)
    monkeypatch.delenv("SIDEKICK_SA_FILE", raising=False)
    monkeypatch.delenv("SIDEKICK_OPENROUTER_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolated_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point __main__._LOG_DIR at tmp_path for every test (#136).

    A real log directory in $HOME must never be created in, written to or pruned by a test
    run — not even by a future case that calls _setup_log_file()/_configure_logging() direct.
    Startup runs are additionally covered by tests/test_log_isolation.py.
    """
    monkeypatch.setattr("sidekick.__main__._LOG_DIR", tmp_path / "logs")
