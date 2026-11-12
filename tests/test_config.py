"""Tests for sidekick/config.py — Config model, load_config(), ConfigError."""

import json
import tempfile
from pathlib import Path

import pytest

from sidekick.config import Config, ConfigError, load_config


def _write_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def test_load_config_minimal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "config.json"
        _write_config(cfg_path, {"project": "my-project"})
        cfg = load_config(cfg_path)
    assert cfg.project == "my-project"
    assert cfg.location == "global"
    assert cfg.default_thinking == "medium"
    assert cfg.auth.method == "adc"


def test_load_config_full() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "config.json"
        _write_config(
            cfg_path,
            {
                "project": "proj",
                "location": "us-east4",
                "default_thinking": "high",
                "transcript_dir": "/tmp/tx",
                "auth": {"method": "env"},
            },
        )
        cfg = load_config(cfg_path)
    assert cfg.location == "us-east4"
    assert cfg.default_thinking == "high"
    assert cfg.auth.method == "env"


def test_load_config_missing_file() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("/nonexistent/path/config.json"))


def test_load_config_invalid_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "config.json"
        cfg_path.write_text("{invalid json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            load_config(cfg_path)


def test_load_config_missing_project() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "config.json"
        _write_config(cfg_path, {"location": "us-central1"})
        with pytest.raises(ConfigError, match="validation failed"):
            load_config(cfg_path)


def test_config_keychain_auth_defaults() -> None:
    cfg = Config(project="p", auth={"method": "keychain"})
    assert cfg.auth.method == "keychain"
    assert cfg.auth.keychain_service == "sidekick"
    assert cfg.auth.keychain_account == "vertex-sa"


def test_adc_config_keychain_fields_are_none() -> None:
    from sidekick.config import AuthConfig

    cfg = AuthConfig(method="adc")
    assert cfg.keychain_service is None
    assert cfg.keychain_account is None


def test_env_config_keychain_fields_are_none() -> None:
    from sidekick.config import AuthConfig

    cfg = AuthConfig(method="env")
    assert cfg.keychain_service is None
    assert cfg.keychain_account is None


def test_keychain_explicit_service_account_preserved() -> None:
    from sidekick.config import AuthConfig

    cfg = AuthConfig(method="keychain", keychain_service="my-svc", keychain_account="my-acct")
    assert cfg.keychain_service == "my-svc"
    assert cfg.keychain_account == "my-acct"


def test_api_key_method_no_project_accepted() -> None:
    cfg = Config(auth={"method": "api_key"})
    assert cfg.auth.method == "api_key"
    assert cfg.project is None


def test_default_model_defaults_none() -> None:
    cfg = Config(project="p")
    assert cfg.default_model is None


def test_default_model_accepts_value() -> None:
    cfg = Config(project="p", default_model="gemini-2.5-pro")
    assert cfg.default_model == "gemini-2.5-pro"


def test_api_key_env_default() -> None:
    from sidekick.config import AuthConfig

    cfg = AuthConfig(method="api_key")
    assert cfg.api_key_env == "GEMINI_API_KEY"


def test_api_key_custom_env_preserved() -> None:
    from sidekick.config import AuthConfig

    cfg = AuthConfig(method="api_key", api_key_env="GOOGLE_API_KEY")
    assert cfg.api_key_env == "GOOGLE_API_KEY"


def test_vertex_methods_require_project() -> None:
    for method in ("adc", "env", "keychain"):
        with pytest.raises(Exception, match="'project' is required"):
            Config(auth={"method": method})


def test_config_location_defaults_to_global() -> None:
    cfg = Config(project="p")
    assert cfg.location == "global"


def test_config_location_override_accepted() -> None:
    cfg = Config(project="p", location="us-central1")
    assert cfg.location == "us-central1"


def test_file_tools_defaults() -> None:
    from sidekick.sandbox import DEFAULT_DENY

    cfg = Config(auth={"method": "api_key"})
    assert cfg.artifacts_dir == "./sidekick-artifacts"
    assert cfg.file_tools.enabled is True
    assert cfg.file_tools.deny == list(DEFAULT_DENY)
    assert cfg.file_tools.max_write_bytes == 262144


def test_file_tools_overrides() -> None:
    cfg = Config(
        auth={"method": "api_key"},
        artifacts_dir="out",
        file_tools={"enabled": False, "deny": ["*.secret"], "max_write_bytes": 10},
    )
    assert cfg.artifacts_dir == "out"
    assert cfg.file_tools.enabled is False
    assert cfg.file_tools.deny == ["*.secret"]
    assert cfg.file_tools.max_write_bytes == 10


def test_file_tools_rejects_nonpositive_write_cap() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Config(auth={"method": "api_key"}, file_tools={"max_write_bytes": 0})


def test_openrouter_model_ids_must_look_like_ids(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    secret = "sk-or-v1-" + "a1b2c3d4" * 8
    for bad in (
        {"default_model": secret},
        {"accepted_models": ["x-ai/grok-4.7", secret]},
    ):
        _write_config(path, {"auth": {"method": "api_key"}, "openrouter": bad})
        with pytest.raises(ConfigError) as err:
            load_config(path)
        assert "validation failed" in str(err.value) and "openrouter" in str(err.value)
        assert secret not in str(err.value) and "a1b2c3d4" not in str(err.value)


def test_openrouter_valid_ids_load(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write_config(
        path,
        {
            "auth": {"method": "api_key"},
            "openrouter": {"default_model": "x-ai/grok-4.7", "accepted_models": ["a/b:free"]},
        },
    )
    assert load_config(path).openrouter.default_model == "x-ai/grok-4.7"
