import json

import pytest

from sidekick.config import (
    Config,
    ConfigError,
    OpenRouterConfig,
    gemini_configured,
    load_config,
    openrouter_api_key,
    remember_accepted_model,
    remember_repo_access,
)
from tests._fakes import FAKE_OPENROUTER_KEY


def test_plugin_option_wins_over_shell_export(monkeypatch):
    monkeypatch.setenv("SIDEKICK_OPENROUTER_KEY", FAKE_OPENROUTER_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "shell-key")
    assert openrouter_api_key() == FAKE_OPENROUTER_KEY


@pytest.mark.parametrize("raw", ["", "  ", "${user_config.openrouter_api_key}"])
def test_unset_option_falls_back_to_shell_export(monkeypatch, raw):
    monkeypatch.setenv("SIDEKICK_OPENROUTER_KEY", raw)
    monkeypatch.setenv("OPENROUTER_API_KEY", " shell-key ")
    assert openrouter_api_key() == "shell-key"


def test_no_key_anywhere_is_none():
    assert openrouter_api_key() is None


def test_openrouter_section_defaults():
    assert Config(project="p").openrouter == OpenRouterConfig(
        default_model=None, accepted_models=[], block_data_collection=False
    )


def test_no_file_with_openrouter_key_loads_without_gemini(monkeypatch, tmp_path):
    monkeypatch.setenv("SIDEKICK_OPENROUTER_KEY", FAKE_OPENROUTER_KEY)
    cfg = load_config(tmp_path / "absent.json")
    assert gemini_configured(cfg, openrouter_key_present=True) is False


def test_file_without_project_or_method_is_valid_with_openrouter_key(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"openrouter": {"default_model": "x-ai/grok-4.7"}}))
    cfg = load_config(path)
    assert cfg.openrouter.default_model == "x-ai/grok-4.7"
    assert gemini_configured(cfg, True) is False


def test_same_file_without_openrouter_key_still_needs_a_project(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"openrouter": {"default_model": "x-ai/grok-4.7"}}))
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(path)


@pytest.mark.parametrize("raw", [{"auth": {"method": "api_key"}}, {"project": "p"}])
def test_explicit_gemini_setup_is_kept_alongside_openrouter(monkeypatch, tmp_path, raw):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    assert gemini_configured(load_config(path), True) is True


def test_gemini_plugin_key_counts_as_gemini(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("SIDEKICK_API_KEY", "g")
    assert gemini_configured(load_config(tmp_path / "absent.json"), True) is True


def test_without_openrouter_key_gemini_is_always_configured():
    assert gemini_configured(Config(project="p"), False) is True


def test_remember_creates_the_file(tmp_path):
    path = tmp_path / "sidekick" / "config.json"
    remember_accepted_model("x-ai/grok-4.7", path)
    assert json.loads(path.read_text()) == {"openrouter": {"accepted_models": ["x-ai/grok-4.7"]}}


def test_remember_merges_and_is_idempotent(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "project": "p",
                "auth": {"method": "adc"},
                "openrouter": {"block_data_collection": True},
            }
        )
    )
    remember_accepted_model("x-ai/grok-4.7", path)
    remember_accepted_model("x-ai/grok-4.7", path)
    assert json.loads(path.read_text()) == {
        "project": "p",
        "auth": {"method": "adc"},
        "openrouter": {"block_data_collection": True, "accepted_models": ["x-ai/grok-4.7"]},
    }
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json"]  # no temp file left


def test_remember_never_clobbers_an_unreadable_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        remember_accepted_model("x-ai/grok-4.7", path)
    assert path.read_text() == "{not json"


def test_remember_repo_access_adds_once_and_keeps_other_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"project": "p", "openrouter": {"accepted_models": ["x-ai/grok-4.7"]}})
    )
    remember_repo_access("x-ai/grok-4.7", path)
    remember_repo_access("x-ai/grok-4.7", path)
    assert json.loads(path.read_text()) == {
        "project": "p",
        "openrouter": {
            "accepted_models": ["x-ai/grok-4.7"],
            "repo_access_models": ["x-ai/grok-4.7"],
        },
    }
    assert OpenRouterConfig().repo_access_models == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json"]


def test_remember_repo_access_refuses_unparseable_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        remember_repo_access("x-ai/grok-4.7", path)
    assert path.read_text() == "{not json"


def test_repo_access_models_must_be_model_ids():
    with pytest.raises(ValueError):
        OpenRouterConfig(repo_access_models=["sk-or-v1-not-a-model"])


def test_deprecated_repo_notice_flag_still_loads():
    cfg = Config.model_validate(
        {"openrouter": {"repo_access_notice_shown": True}}, context={"openrouter_key": True}
    )
    assert cfg.openrouter.repo_access_models == []
