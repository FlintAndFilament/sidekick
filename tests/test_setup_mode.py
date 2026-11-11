"""#116 review follow-ups: plugin-sourced secrets in setup, never echoing secrets, and the
setup-mode server actually replacing every startup exit."""

import json
import logging
from pathlib import Path

import pytest

from sidekick import __main__ as entry
from sidekick import setup_cli
from sidekick.auth import AuthError, build_auth
from sidekick.config import AuthConfig, ConfigError, load_config
from sidekick.server import build_setup_server
from tests._fakes import FAKE_GOOGLE_API_KEY


def _write(cfg: Path, *args: str) -> dict:  # type: ignore[type-arg]
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_cli.main(["write", "--config", str(cfg), *args])
    return json.loads(out.getvalue())  # type: ignore[no-any-return]


# ---- F1: setup_cli cannot see plugin options, so it must not give env-var advice for them


def test_plugin_key_source_gives_no_export_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = _write(tmp_path / "c.json", "--auth-method", "api_key", "--key-source", "plugin")
    assert out["ok"] is True and out["warnings"] == []


def test_env_key_source_still_warns_without_shell_profile_advice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (warning,) = _write(tmp_path / "c.json", "--auth-method", "api_key")["warnings"]
    assert "GEMINI_API_KEY" in warning and "plugin" in warning
    assert "shell profile" not in warning


def test_picker_sa_file_ignores_a_stale_shell_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "gone.json"))
    out = _write(
        tmp_path / "c.json",
        *("--auth-method", "env", "--project", "p", "--sa-file-source", "plugin"),
    )
    assert out["ok"] is True and out["warnings"] == []


# ---- F2: secret-shaped text in the SA file option is never echoed


@pytest.mark.parametrize(
    "pasted",
    [
        FAKE_GOOGLE_API_KEY,
        '{"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----\\nabc"}',
    ],
)
def test_secret_in_sa_file_option_is_never_echoed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pasted: str
) -> None:
    monkeypatch.setenv("SIDEKICK_SA_FILE", pasted)
    caplog.set_level(logging.DEBUG, logger="sidekick")
    with pytest.raises(AuthError) as err:
        build_auth(AuthConfig(method="env"))
    assert "isn't a file path" in str(err.value)
    for leak in (pasted, pasted[:12], 'private_key": "-----'):
        assert leak not in str(err.value)
        assert leak not in caplog.text


def test_plugin_api_key_is_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SIDEKICK_API_KEY", FAKE_GOOGLE_API_KEY)
    caplog.set_level(logging.DEBUG, logger="sidekick")
    build_auth(AuthConfig(method="api_key"))
    assert FAKE_GOOGLE_API_KEY not in caplog.text


# ---- F4: every startup failure serves the setup server


def test_unreadable_config_is_a_config_error(tmp_path: Path) -> None:
    (tmp_path / "config.json").mkdir()  # a directory where the file belongs
    with pytest.raises(ConfigError, match="could not be read"):
        load_config(tmp_path / "config.json")


def test_keychain_json_that_is_not_a_service_account_is_an_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout='{"not": "a service account"}')

    monkeypatch.setattr("sidekick.auth.subprocess.run", fake_run)
    with pytest.raises(AuthError, match="not valid service account JSON"):
        build_auth(AuthConfig(method="keychain"))


@pytest.mark.parametrize(
    "failure",
    [
        ("sidekick.config.load_config", ConfigError("bad config")),
        ("sidekick.auth.build_auth", AuthError("no key here")),
        ("sidekick.config.load_config", RuntimeError("secret-ish detail")),
    ],
)
def test_main_serves_setup_mode_instead_of_exiting(
    monkeypatch: pytest.MonkeyPatch, failure: tuple[str, Exception]
) -> None:
    target, exc = failure

    def boom(*_a: object, **_k: object) -> None:
        raise exc

    monkeypatch.setattr(target, boom)
    if target.endswith("build_auth"):
        monkeypatch.setattr("sidekick.config.load_config", lambda: object.__new__(_Cfg))
    served: list[str] = []
    monkeypatch.setattr(entry, "_serve_setup_problem", served.append)
    entry.main()  # must return, never SystemExit
    (problem,) = served
    if isinstance(exc, (ConfigError, AuthError)):
        assert str(exc) in problem
    else:
        assert "RuntimeError" in problem and "secret-ish detail" not in problem


class _Cfg:
    auth = AuthConfig(method="api_key")


async def test_setup_status_tool_returns_the_fix() -> None:
    server = build_setup_server("no key: open Configure options")
    result = await server.call_tool("setup_status", {})
    text = json.dumps(result, default=str)
    assert "no key: open Configure options" in text
    assert "/sidekick:setup" in text


def test_a_crash_while_serving_is_not_treated_as_a_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Server:
        def run(self) -> None:
            raise OSError("stdio closed")

    monkeypatch.setattr(entry, "_start", lambda: _Server())
    served: list[str] = []
    monkeypatch.setattr(entry, "_serve_setup_problem", served.append)
    with pytest.raises(OSError):
        entry.main()
    assert served == []  # no second server on an already-initialized stdio
