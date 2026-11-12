"""
sidekick/__main__.py
--------------------------
Entry point: python -m sidekick

Responsibilities:
  - Configure structured logging (file + stderr) before any other import runs
  - Load config from ~/.config/sidekick/config.json
  - Build credentials via auth.build_auth()
  - Instantiate GeminiClient and TranscriptWriter
  - Build and run the MCP server
  - On a setup problem, serve a setup_status tool instead of exiting (#116)

Design notes:
  - Single Responsibility: startup orchestration only; all logic lives in imported modules
  - Dependency Inversion: passes ready-made Config, Credentials, Client, Transcript to server
  - Logging is configured first so all downstream modules inherit the handler/formatter

Log files:
  ~/.config/sidekick/logs/YYYYMMDD-sidekick.log
  Daily files; 4 most recent kept; multiple sessions same day append to same file.
  Tail: tail -f ~/.config/sidekick/logs/$(ls -t ~/.config/sidekick/logs/*.log | head -1 | xargs basename)

Environment variables:
  SIDEKICK_LOG_LEVEL  — DEBUG | INFO | WARNING | ERROR (default: INFO)

Startup failures never exit: a config, auth, transcript or artifacts problem starts the
setup server (server.build_setup_server) so /mcp shows "connected" and Claude can explain.

Used by:  pyproject.toml [project.scripts], MCP registration (python -m sidekick)
Imports:  config.py, auth.py, client.py, transcript.py, server.py
"""

import logging
import os
import sys
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
from datetime import datetime
from pathlib import Path

_LOG_DIR = Path.home() / ".config" / "sidekick" / "logs"
_MAX_LOG_FILES = 4
# Third-party loggers worth routing into our log file: the SDK, its transport, and httpx
# (used by sources.py to resolve grounding redirects).
_LIBRARY_LOGGERS = ("google", "google_genai", "httpx", "httpcore", "urllib3")


def _setup_log_file(startup_time: datetime) -> logging.FileHandler:
    """Create today's log file, prune files beyond _MAX_LOG_FILES."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _LOG_DIR / f"{startup_time.strftime('%Y%m%d')}-sidekick.log"
    # Prune oldest files, keeping _MAX_LOG_FILES most recent
    existing = sorted(_LOG_DIR.glob("*-sidekick.log"), reverse=True)
    for old in existing[_MAX_LOG_FILES:]:
        try:
            old.unlink()
        except OSError:
            pass
    return logging.FileHandler(log_file, mode="a", encoding="utf-8")


class _DropHttpPayloads(logging.Filter):
    """Drop records carrying a google-auth HTTP payload (#98).

    `google.auth.transport.requests` logs the request URL, the **raw** headers and the body
    under `extra={"httpRequest"/"httpResponse": ...}` at DEBUG. It hashes access and refresh
    tokens but not a service-account assertion JWT, and headers carry bearer tokens verbatim.
    Our `%(message)s` formatter renders no extras, so none of it reaches the log file today —
    but that is incidental, and a structured formatter would silently start writing credential
    material into a file that lives for days. Dropping the records makes the property explicit.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (hasattr(record, "httpRequest") or hasattr(record, "httpResponse"))


def _configure_logging(startup_time: datetime) -> None:
    """Set up file + stderr logging before any module that calls getLogger() is imported."""
    raw_level = os.environ.get("SIDEKICK_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, raw_level, logging.INFO)
    formatter = logging.Formatter(
        fmt="[sidekick] %(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # On the handlers, not the loggers: a logger's filters do not run for records that
    # propagate up from its children, so google.auth.transport.requests would bypass one
    # attached to "google".
    payload_filter = _DropHttpPayloads()
    root = logging.getLogger("sidekick")
    root.setLevel(level)
    # File handler — primary; visible via tail
    file_handler = _setup_log_file(startup_time)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(payload_filter)
    root.addHandler(file_handler)
    # Stderr handler — useful when running server manually; swallowed by Claude Code
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(payload_filter)
    root.addHandler(stderr_handler)
    # Third-party loggers need our handlers attached explicitly (#88): the handlers above sit
    # on the "sidekick" logger, which their records never pass through, so without this
    # a library record reaches nothing but Python's last-resort stderr handler — DEBUG mode
    # showed no library output, and setting their level alone did nothing. Below DEBUG they
    # stay at WARNING: httpx logs every request at INFO.
    library_level = level if level <= logging.DEBUG else logging.WARNING
    for name in _LIBRARY_LOGGERS:
        library = logging.getLogger(name)
        library.setLevel(library_level)
        library.addHandler(file_handler)
        library.addHandler(stderr_handler)


_startup_time = datetime.now()
_configure_logging(_startup_time)

# Use explicit package path — __name__ is "__main__" when run via python3 -m sidekick,
# which is not a child of "sidekick" and would bypass our file handler.
_log = logging.getLogger("sidekick.__main__")


def _serve_setup_problem(problem: str) -> None:
    from sidekick.server import build_setup_server

    build_setup_server(problem).run()


def main() -> None:
    try:
        server = _start()
    except Exception as exc:  # noqa: BLE001 - last resort: never leave only "Connection closed"
        # Type name only: an unexpected message could carry anything, secrets included.
        _log.exception("startup failed — unexpected error")
        _serve_setup_problem(
            f"Unexpected startup error ({type(exc).__name__}). Details are in the sidekick log "
            "(~/.config/sidekick/logs/)."
        )
        return
    # Outside the try: a failure while serving is not a startup failure, and must not start a
    # second (setup) server on the same stdio.
    if server is not None:
        server.run()


def _start() -> "Optional[FastMCP]":
    """Build the real server, or serve setup mode and return None on a setup problem."""
    startup_time = _startup_time

    from sidekick.auth import AuthError, build_auth
    from sidekick.backends import ModelSource
    from sidekick.client import GeminiClient
    from sidekick.config import ConfigError, gemini_configured, load_config, openrouter_api_key
    from sidekick.openrouter import OpenRouterClient
    from sidekick.router import Router
    from sidekick.sandbox import SandboxError
    from sidekick.server import build_server
    from sidekick.transcript import TranscriptError, TranscriptWriter
    from sidekick.workspace import build_workspace, project_root

    try:
        config = load_config()
    except ConfigError as exc:
        _log.error("startup failed — config error: %s", exc)
        _serve_setup_problem(str(exc))
        return None

    or_key = openrouter_api_key()
    gemini: Optional[GeminiClient] = None
    gemini_problem: Optional[str] = None
    if gemini_configured(config, or_key is not None):
        try:
            auth_result = build_auth(config.auth)
        except AuthError as exc:
            if or_key is None:
                _log.error("startup failed — auth error: %s", exc)
                _serve_setup_problem(str(exc))
                return None
            gemini_problem = str(exc)
            _log.warning("Gemini backend unavailable; serving OpenRouter only: %s", exc)
        else:
            gemini = GeminiClient(
                config,
                credentials=auth_result.credentials,
                api_key=auth_result.api_key,
                unpaid_tier_notice=auth_result.api_key is not None,
            )
            latest = gemini.refresh_latest()  # one models.list call; pinned defaults if it fails
            _log.info(
                "latest models: %s",
                ", ".join(f"{family}={mid}" for family, mid in latest.items())
                or "unresolved (pinned)",
            )
    source: ModelSource
    if or_key is not None:
        openrouter = OpenRouterClient(config, or_key)
        openrouter.refresh_catalog()
        source = Router(openrouter, gemini, gemini_problem)
    else:
        assert gemini is not None
        source = gemini
    _log.info(
        "starting — primary=%s default_model=%s gemini=%s",
        source.surface.primary,
        source.default_model,
        "on" if gemini is not None else ("unavailable" if gemini_problem else "off"),
    )

    root = project_root()

    try:
        transcript = TranscriptWriter(config.transcript_dir, startup_time, base=root)
    except TranscriptError as exc:
        _log.error("startup failed — %s", exc)
        _serve_setup_problem(f"transcript_dir: {exc}")
        return None

    _log.info("transcript → %s", transcript.path)

    try:
        workspace = build_workspace(config, root)
    except SandboxError as exc:
        _log.error("startup failed — artifacts_dir %r: %s", config.artifacts_dir, exc)
        _serve_setup_problem(f"artifacts_dir {config.artifacts_dir!r}: {exc}")
        return None
    _log.info(
        "file tools %s — sandbox root %s — artifacts → %s",
        "enabled" if workspace.tools_enabled else f"DISABLED ({workspace.disabled_reason})",
        workspace.sandbox.root,
        workspace.artifacts.directory,
    )

    return build_server(source, transcript, workspace)


if __name__ == "__main__":
    main()
