"""The plugin's masked API key option: zero-config api_key auth, no shell export (#116)."""

import json
from pathlib import Path

import pytest

import sidekick.auth as auth_module
from sidekick.auth import AuthError, build_auth
from sidekick.config import AuthConfig, ConfigError, load_config, plugin_api_key
from sidekick.server import build_setup_server, setup_needed_text

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("raw", ["", "   ", "${user_config.gemini_api_key}"])
def test_unset_option_reads_as_absent(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SIDEKICK_API_KEY", raw)
    assert plugin_api_key() is None


def test_plugin_key_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEKICK_API_KEY", "  plugin-key \n")
    assert plugin_api_key() == "plugin-key"


def test_plugin_key_wins_over_shell_export(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEKICK_API_KEY", "plugin-key")
    monkeypatch.setenv("GEMINI_API_KEY", "shell-key")
    assert build_auth(AuthConfig(method="api_key")).api_key == "plugin-key"


def test_blank_plugin_option_falls_back_to_shell_export(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEKICK_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "shell-key")
    assert build_auth(AuthConfig(method="api_key")).api_key == "shell-key"


def test_missing_key_error_points_inside_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(AuthError) as err:
        build_auth(AuthConfig(method="api_key"))
    message = str(err.value)
    assert "/plugin configure sidekick@sidekick" in message
    assert "/reload-plugins" in message
    assert "/sidekick:setup" in message
    assert "export" not in message  # the fix is never a shell-profile edit


def test_vertex_methods_ignore_the_plugin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEKICK_API_KEY", "plugin-key")
    sentinel = object()
    monkeypatch.setitem(auth_module._LOADERS, "adc", lambda _cfg: sentinel)
    result = build_auth(AuthConfig(method="adc"))
    assert result.credentials is sentinel and result.api_key is None


def test_no_config_file_plus_plugin_key_is_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SIDEKICK_API_KEY", "plugin-key")
    assert load_config(tmp_path / "absent.json").auth.method == "api_key"


def test_no_config_file_no_key_still_asks_for_setup(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="/sidekick:setup"):
        load_config(tmp_path / "absent.json")


def test_config_without_method_plus_plugin_key_is_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"default_thinking": "low"}))
    monkeypatch.setenv("SIDEKICK_API_KEY", "plugin-key")
    cfg = load_config(path)
    assert cfg.auth.method == "api_key"
    assert cfg.default_thinking == "low"


def test_explicit_vertex_method_is_kept_despite_plugin_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"project": "p", "auth": {"method": "adc"}}))
    monkeypatch.setenv("SIDEKICK_API_KEY", "plugin-key")
    assert load_config(path).auth.method == "adc"


def test_manifest_declares_a_masked_key_and_maps_it_to_its_own_var() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    option = plugin["userConfig"]["gemini_api_key"]
    assert option["sensitive"] is True
    assert option.get("required", False) is False  # Agent Platform users leave it blank
    env = plugin["mcpServers"]["mcp"]["env"]
    assert env["SIDEKICK_API_KEY"] == "${user_config.gemini_api_key}"
    assert "GEMINI_API_KEY" not in env  # never shadow a user's own shell export


def test_setup_server_stays_up_and_explains() -> None:
    server = build_setup_server("no key")
    assert "no key" in (server.instructions or "")
    assert "/sidekick:setup" in setup_needed_text("x")
    assert "/reload-plugins" in setup_needed_text("x")
    tools = {t.name for t in server._tool_manager.list_tools()}
    assert tools == {"setup_status"}


def _fake_sa_loader(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    loaded: list[str] = []

    def load(path: str, scopes: object = None) -> tuple[object, str]:
        loaded.append(path)
        return object(), "project"

    monkeypatch.setattr("google.auth.load_credentials_from_file", load)
    return loaded


def test_plugin_sa_file_wins_over_shell_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    picked, exported = tmp_path / "picked.json", tmp_path / "exported.json"
    picked.write_text("{}")
    exported.write_text("{}")
    monkeypatch.setenv("SIDEKICK_SA_FILE", str(picked))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(exported))
    loaded = _fake_sa_loader(monkeypatch)
    build_auth(AuthConfig(method="env"))
    assert loaded == [str(picked)]


def test_unset_sa_file_option_falls_back_to_shell_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exported = tmp_path / "exported.json"
    exported.write_text("{}")
    monkeypatch.setenv("SIDEKICK_SA_FILE", "${user_config.service_account_file}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(exported))
    loaded = _fake_sa_loader(monkeypatch)
    build_auth(AuthConfig(method="env"))
    assert loaded == [str(exported)]


def test_missing_sa_file_points_at_the_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    with pytest.raises(AuthError) as err:
        build_auth(AuthConfig(method="env"))
    assert "Service-account key file" in str(err.value)
    assert "/plugin configure sidekick@sidekick" in str(err.value)
    assert "/reload-plugins" in str(err.value)


def test_picked_file_that_vanished_names_the_plugin_option(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SIDEKICK_SA_FILE", str(tmp_path / "gone.json"))
    with pytest.raises(AuthError, match="plugin's service-account file option"):
        build_auth(AuthConfig(method="env"))


def test_manifest_declares_the_sa_file_picker() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    option = plugin["userConfig"]["service_account_file"]
    assert option["type"] == "file"
    assert option.get("required", False) is False
    env = plugin["mcpServers"]["mcp"]["env"]
    assert env["SIDEKICK_SA_FILE"] == "${user_config.service_account_file}"
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env


def test_setup_mode_instructions_stay_under_the_cap() -> None:
    # Claude Code truncates server instructions at ~2048 chars; the fixed text leaves room
    # for the problem message.
    assert len(setup_needed_text("")) < 500
