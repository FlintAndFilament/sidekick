"""Test runs must never write into, create or prune the user's real log directory (#136, #144).

Logging is configured in main(), not when sidekick.__main__ is imported (approach from PR #137
by @bfs2021), so importing the entry point for tests attaches no file handler. conftest.py also
points _LOG_DIR at tmp_path for every test, so even a direct _configure_logging() call stays
out of $HOME.
"""

import logging
from pathlib import Path

import pytest

from sidekick import __main__ as entry
from sidekick.config import Config
from tests._fakes import FAKE_OPENROUTER_KEY

REAL_LOG_DIR = Path.home() / ".config" / "sidekick" / "logs"


def _file_handlers() -> list[logging.FileHandler]:
    loggers = [logging.getLogger("sidekick"), *map(logging.getLogger, entry._LIBRARY_LOGGERS)]
    return [h for lg in loggers for h in lg.handlers if isinstance(h, logging.FileHandler)]


def _snapshot(log_dir: Path) -> "dict[str, tuple[int, int]] | None":
    """name -> (size, mtime_ns) for every file in the directory, or None when it is absent."""
    if not log_dir.is_dir():
        return None
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in log_dir.iterdir()}


def test_importing_the_entry_point_attaches_no_file_handler() -> None:
    assert _file_handlers() == []


def test_log_dir_points_at_tmp_path_during_tests(tmp_path: Path) -> None:
    assert entry._LOG_DIR == tmp_path / "logs"


def test_startup_leaves_the_real_log_dir_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same _start() path test_startup_openrouter.py exercises (check from PR #137): the
    real log directory comes out byte-identical — nothing appended, created or pruned."""
    before = _snapshot(REAL_LOG_DIR)
    monkeypatch.setenv("SIDEKICK_OPENROUTER_KEY", FAKE_OPENROUTER_KEY)
    cfg = Config.model_validate({"transcript_dir": str(tmp_path)}, context={"openrouter_key": True})
    monkeypatch.setattr("sidekick.config.load_config", lambda: cfg)
    monkeypatch.setattr("sidekick.server.build_server", lambda source, *a, **k: object())
    monkeypatch.setattr("sidekick.openrouter.OpenRouterClient.refresh_catalog", lambda self: False)
    monkeypatch.setattr("sidekick.workspace.project_root", lambda: tmp_path)

    entry._start()

    assert _snapshot(REAL_LOG_DIR) == before


def test_configure_logging_writes_under_the_patched_dir(tmp_path: Path) -> None:
    startup = entry.datetime.now()
    handlers_before = {
        name: list(logging.getLogger(name).handlers)
        for name in ("sidekick", *entry._LIBRARY_LOGGERS)
    }
    try:
        entry._configure_logging(startup)
        logging.getLogger("sidekick.probe").info("ISOLATION_PROBE_144")
        files = _file_handlers()
        assert files and all(Path(h.baseFilename).parent == tmp_path / "logs" for h in files)
        for h in files:
            h.flush()
        assert "ISOLATION_PROBE_144" in Path(files[0].baseFilename).read_text()
    finally:
        for name, kept in handlers_before.items():
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                if h not in kept:
                    lg.removeHandler(h)
                    h.close()
