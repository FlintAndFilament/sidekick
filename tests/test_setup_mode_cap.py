"""Setup-mode instructions stay under the client's instructions cap (#121).

In setup mode the server's MCP instructions are the startup problem text itself
(server.setup_needed_text). Claude Code truncates instructions past about 2048 characters,
so the fix advice at the end of a long error would be cut off. These tests build the longest
realistic startup errors through the real code paths and check the result fits
INSTRUCTIONS_BUDGET, the same budget the normal server's instructions keep."""

import json
from pathlib import Path

import pytest

from sidekick.auth import AuthError, build_auth
from sidekick.config import AuthConfig, ConfigError, load_config
from sidekick.guide import INSTRUCTIONS_BUDGET
from sidekick.server import build_setup_server, setup_needed_text


def _deep_dir(root: Path) -> Path:
    """A long but plausible project path (about 200 characters)."""
    deep = root / "Users" / "someone-with-a-long-name" / "Library" / "Mobile Documents"
    deep = deep / "com~apple~CloudDocs" / "work" / "clients" / "a-client-with-a-long-name"
    deep = deep / "gcp" / "service-accounts" / "sidekick-production-agent-platform"
    deep.mkdir(parents=True)
    return deep


def _config_error(tmp_path: Path) -> str:
    """A hand-edited config.json with four wrong values, at a long path.

    Pydantic adds about 200 characters per invalid field, so a file with nine or more wrong
    values would overflow; four is a realistic worst case for a hand edit."""
    cfg = _deep_dir(tmp_path) / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "default_thinking": "maximum",
                "file_tools": {"max_write_bytes": -1},
                "web_tools": {"enabled": "maybe"},
                "auth": {"method": "oauth"},
            }
        )
    )
    with pytest.raises(ConfigError) as info:
        load_config(cfg)
    return str(info.value)


def _sa_file_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """An SA key file at a long path that exists but is not a service-account key."""
    sa = _deep_dir(tmp_path) / "sidekick-production-agent-platform-service-account-key.json"
    sa.write_text(json.dumps({"type": "authorized_user", "client_id": "x" * 200}))
    monkeypatch.delenv("SIDEKICK_SA_FILE", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(sa))
    with pytest.raises(AuthError) as info:
        build_auth(AuthConfig(method="env"))
    return str(info.value)


def _missing_key_error(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.delenv("SIDEKICK_API_KEY", raising=False)
    monkeypatch.delenv("A_VERY_LONG_ENVIRONMENT_VARIABLE_NAME_FOR_THE_GEMINI_KEY", raising=False)
    with pytest.raises(AuthError) as info:
        build_auth(
            AuthConfig(
                method="api_key",
                api_key_env="A_VERY_LONG_ENVIRONMENT_VARIABLE_NAME_FOR_THE_GEMINI_KEY",
            )
        )
    return str(info.value)


def test_long_config_error_fits(tmp_path: Path) -> None:
    problem = _config_error(tmp_path)
    assert len(problem) > 500, "the fixture should produce a genuinely long error"
    assert len(setup_needed_text(problem)) < INSTRUCTIONS_BUDGET


def test_long_sa_file_error_fits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    problem = _sa_file_error(tmp_path, monkeypatch)
    assert len(problem) > 400, "the fixture should produce a genuinely long error"
    assert len(setup_needed_text(problem)) < INSTRUCTIONS_BUDGET


def test_missing_key_error_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(setup_needed_text(_missing_key_error(monkeypatch))) < INSTRUCTIONS_BUDGET


def test_setup_server_instructions_are_the_capped_text(tmp_path: Path) -> None:
    problem = _config_error(tmp_path)
    server = build_setup_server(problem)
    assert server.instructions == setup_needed_text(problem)
    assert server.instructions is not None and len(server.instructions) < INSTRUCTIONS_BUDGET


def test_any_problem_length_fits_under_the_cap() -> None:
    # A config with many invalid fields produced >2000 chars before the cap (#121).
    text = setup_needed_text("field: bad value\n" * 400)
    assert len(text) < 2000
    assert "truncated" in text
