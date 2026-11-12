"""Shared fixtures."""

import atexit
import os
import shutil
import tempfile

import pytest


def _import_entry_point_with_throwaway_logs() -> None:
    """Import sidekick.__main__ once, with HOME pointed at a throwaway directory (#136).

    The import configures logging as a side effect: it attaches a file handler under
    ~/.config/sidekick/logs and prunes old files there. Tests that import the entry point would
    otherwise write fake-config startup lines into the user's real daily log. conftest loads
    before any test module, so this import is the one that runs; HOME is restored right after,
    and only the import-time log directory is affected (config paths are read later).
    """
    log_home = tempfile.mkdtemp(prefix="sidekick-test-logs-")
    atexit.register(shutil.rmtree, log_home, ignore_errors=True)
    real_home = os.environ.get("HOME")
    os.environ["HOME"] = log_home
    try:
        import sidekick.__main__  # noqa: F401 - imported for its logging side effect
    finally:
        if real_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = real_home


_import_entry_point_with_throwaway_logs()


@pytest.fixture(autouse=True)
def _no_plugin_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real plugin option in the developer's environment must never leak into a test (#116)."""
    monkeypatch.delenv("SIDEKICK_API_KEY", raising=False)
    monkeypatch.delenv("SIDEKICK_SA_FILE", raising=False)
    monkeypatch.delenv("SIDEKICK_OPENROUTER_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
