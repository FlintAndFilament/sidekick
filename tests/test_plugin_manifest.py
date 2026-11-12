"""The plugin's manifests are part of the product: pin the facts users depend on (#102)."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(rel: str) -> dict:  # type: ignore[type-arg]
    return json.loads((ROOT / rel).read_text())


def test_plugin_is_named_sidekick() -> None:
    plugin = load(".claude-plugin/plugin.json")
    assert plugin["name"] == "sidekick"
    assert plugin["version"]


def test_marketplace_ships_main_never_develop() -> None:
    market = load(".claude-plugin/marketplace.json")
    (entry,) = market["plugins"]
    assert entry["name"] == "sidekick"
    assert entry["source"] == {
        "source": "github",
        "repo": "FlintAndFilament/sidekick",
        "ref": "main",
    }


def test_server_launches_frozen_with_the_venv_in_plugin_data() -> None:
    server = load(".claude-plugin/plugin.json")["mcpServers"]["mcp"]
    assert server["command"] == "uv"
    assert server["args"][:2] == ["run", "--frozen"]
    project_index = server["args"].index("--project")
    assert server["args"][project_index + 1] == "${CLAUDE_PLUGIN_ROOT}"
    assert "--directory" not in server["args"]
    assert server["args"][-1] == "sidekick"
    assert server["env"]["UV_PROJECT_ENVIRONMENT"] == "${CLAUDE_PLUGIN_DATA}/venv"


def test_lockfile_is_committed() -> None:
    assert (ROOT / "uv.lock").is_file()


def test_setup_command_drives_the_cli_and_never_asks_for_the_key() -> None:
    text = (ROOT / "commands" / "setup.md").read_text()
    assert "sidekick-setup status" in text
    assert "sidekick-setup write" in text
    assert "AskUserQuestion" in text
    assert "UV_PROJECT_ENVIRONMENT" in text  # same venv as the server, not a second one
    assert "never ask for the key" in text.lower()


def test_manifest_declares_a_masked_openrouter_key_first() -> None:
    plugin = load(".claude-plugin/plugin.json")
    assert list(plugin["userConfig"])[0] == "openrouter_api_key"
    option = plugin["userConfig"]["openrouter_api_key"]
    assert option["sensitive"] is True and option.get("required", False) is False
    assert option["title"] == "OpenRouter API key"
    env = plugin["mcpServers"]["mcp"]["env"]
    assert env["SIDEKICK_OPENROUTER_KEY"] == "${user_config.openrouter_api_key}"
    assert "OPENROUTER_API_KEY" not in env  # never shadow a shell export
