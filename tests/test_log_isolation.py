"""The test session must never write into the user's real log directory (#136).

Importing sidekick.__main__ attaches a file handler as a side effect, so any test that imports
it would otherwise append fake-config startup lines to ~/.config/sidekick/logs and let startup
prune the user's real log files. conftest.py redirects that import to a throwaway directory.
"""

import logging
from pathlib import Path

from sidekick import __main__ as entry

REAL_LOG_DIR = Path.home() / ".config" / "sidekick" / "logs"


def _file_handlers() -> list[logging.FileHandler]:
    loggers = [logging.getLogger("sidekick"), *map(logging.getLogger, entry._LIBRARY_LOGGERS)]
    return [h for lg in loggers for h in lg.handlers if isinstance(h, logging.FileHandler)]


def test_log_dir_is_not_the_real_one() -> None:
    assert entry._LOG_DIR.resolve() != REAL_LOG_DIR.resolve()


def test_no_file_handler_points_into_the_real_log_dir() -> None:
    handlers = _file_handlers()
    assert handlers, "expected the import-time file handler to exist"
    for handler in handlers:
        assert not Path(handler.baseFilename).resolve().is_relative_to(REAL_LOG_DIR.resolve())


def test_startup_records_land_outside_the_real_log_dir() -> None:
    logging.getLogger("sidekick.__main__").info("ISOLATION_PROBE_136")
    for handler in _file_handlers():
        handler.flush()
        assert "ISOLATION_PROBE_136" in Path(handler.baseFilename).read_text()
