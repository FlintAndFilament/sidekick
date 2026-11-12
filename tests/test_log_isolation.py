"""Test runs must not write into (or prune) the developer's real log directory (#136)."""

from pathlib import Path

import pytest

from sidekick import __main__ as entry
from sidekick.config import Config
from tests._fakes import FAKE_OPENROUTER_KEY


def _snapshot(log_dir: Path) -> dict[str, tuple[int, int]] | None:
    """name -> (size, mtime_ns) for every file in the dir, or None when it is absent."""
    if not log_dir.is_dir():
        return None
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in log_dir.iterdir()}


def test_startup_under_pytest_leaves_the_real_log_dir_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same _start() path test_startup_openrouter.py exercises, but the real
    ~/.config/sidekick/logs must come out byte-identical: no 'starting —' lines
    appended to today's file, no other file created, nothing pruned."""
    real_dir = Path.home() / ".config" / "sidekick" / "logs"
    before = _snapshot(real_dir)

    monkeypatch.setenv("SIDEKICK_OPENROUTER_KEY", FAKE_OPENROUTER_KEY)
    cfg = Config.model_validate({"transcript_dir": str(tmp_path)}, context={"openrouter_key": True})
    monkeypatch.setattr("sidekick.config.load_config", lambda: cfg)
    monkeypatch.setattr("sidekick.server.build_server", lambda source, *a, **k: object())
    monkeypatch.setattr("sidekick.openrouter.OpenRouterClient.refresh_catalog", lambda self: False)
    monkeypatch.setattr("sidekick.workspace.project_root", lambda: tmp_path)

    entry._start()

    assert _snapshot(real_dir) == before
