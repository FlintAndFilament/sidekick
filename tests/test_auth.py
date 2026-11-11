"""Tests for sidekick/auth.py — build_credentials() dispatch and error handling."""

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sidekick.auth import AuthError, build_auth, build_credentials
from sidekick.config import AuthConfig, ConfigError
from tests._fakes import FAKE_GOOGLE_API_KEY

_SA_FROM_INFO = "google.oauth2.service_account.Credentials.from_service_account_info"


def _adc_config() -> AuthConfig:
    return AuthConfig(method="adc")


def _env_config() -> AuthConfig:
    return AuthConfig(method="env")


def _keychain_config() -> AuthConfig:
    return AuthConfig(method="keychain", keychain_service="svc", keychain_account="acct")


class TestADC:
    def test_adc_returns_credentials(self) -> None:
        mock_creds = MagicMock()
        with patch("google.auth.default", return_value=(mock_creds, "project")):
            result = build_credentials(_adc_config())
        assert result is mock_creds

    def test_adc_raises_auth_error_on_missing_creds(self) -> None:
        import google.auth.exceptions

        with patch(
            "google.auth.default",
            side_effect=google.auth.exceptions.DefaultCredentialsError("no creds"),
        ):
            with pytest.raises(AuthError, match="ADC"):
                build_credentials(_adc_config())


class TestEnv:
    """method 'env' must use GOOGLE_APPLICATION_CREDENTIALS and nothing else: google.auth.default
    silently falls back to the user's gcloud ADC, which would run the server as the user
    instead of the configured service account (#86)."""

    NO_ADC = patch("google.auth.default", side_effect=AssertionError("ADC must not be used"))

    def test_unset_var_fails_instead_of_borrowing_adc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        with self.NO_ADC, pytest.raises(AuthError, match="GOOGLE_APPLICATION_CREDENTIALS"):
            build_credentials(_env_config())

    def test_missing_key_file_names_the_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        missing = tmp_path / "sa-key.json"
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(missing))
        with self.NO_ADC, pytest.raises(AuthError, match=re.escape(str(missing))):
            build_credentials(_env_config())

    def test_key_file_is_loaded_explicitly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "sa-key.json"
        key_file.write_text(json.dumps({"type": "service_account", "client_email": "a@b"}))
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key_file))
        mock_creds = MagicMock()
        with (
            patch(_SA_FROM_INFO, return_value=mock_creds) as loader,
            patch("google.auth.load_credentials_from_file", side_effect=AssertionError),
        ):
            with self.NO_ADC:
                result = build_credentials(_env_config())
        assert result is mock_creds
        assert loader.call_args.args[0]["client_email"] == "a@b"

    @pytest.mark.parametrize("cred_type", ["external_account", "authorized_user", None])
    def test_non_service_account_file_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cred_type: object
    ) -> None:
        # #124: the env method accepts a service-account key only. An external-account or
        # user credential config must never reach a google-auth loader.
        key_file = tmp_path / "creds.json"
        key_file.write_text(json.dumps({"type": cred_type, "audience": "x"}))
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key_file))
        with patch(_SA_FROM_INFO, side_effect=AssertionError) as loader:
            with self.NO_ADC, pytest.raises(AuthError, match="not a service-account key"):
                build_credentials(_env_config())
        loader.assert_not_called()

    def test_non_json_file_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "sa-key.json"
        key_file.write_text("not json")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key_file))
        with self.NO_ADC, pytest.raises(AuthError, match="could not be loaded"):
            build_credentials(_env_config())

    def test_unreadable_key_file_raises_auth_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "sa-key.json"
        key_file.write_text(json.dumps({"type": "service_account"}))
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key_file))
        with patch(_SA_FROM_INFO, side_effect=ValueError("missing private_key")):
            with pytest.raises(AuthError, match="GOOGLE_APPLICATION_CREDENTIALS"):
                build_credentials(_env_config())


class TestKeychain:
    def _make_sa_json(self) -> str:
        # Fake SA JSON — the from_service_account_info call is patched so the key is never used.
        # The private_key field intentionally does not contain a valid PEM header/trailer
        # to avoid triggering the detect-private-key pre-commit hook.
        # from_service_account_info is patched, so the actual fields don't matter.
        # Using a minimal dict to avoid triggering credential-detection hooks.
        return json.dumps({"project_id": "proj", "client_email": "sa@proj.iam.gserviceaccount.com"})

    def test_keychain_not_found_raises_auth_error(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(44, "security"),
        ):
            with pytest.raises(AuthError, match="Keychain item not found") as err:
                build_credentials(_keychain_config())
        message = str(err.value)
        # A manual store command, not a loop back to /sidekick:setup (which can't store it, #123).
        assert "security add-generic-password -U" in message
        assert '-w "$(cat /path/to/sa-key.json)"' in message
        assert "/sidekick:setup" not in message
        assert "/reload-plugins" in message

    def test_keychain_security_missing_raises_auth_error(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(AuthError, match="macOS"):
                build_credentials(_keychain_config())

    def test_keychain_bad_json_raises_auth_error(self) -> None:
        mock_result = MagicMock()
        mock_result.stdout = "not-json"
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(AuthError, match="not valid service account JSON"):
                build_credentials(_keychain_config())

    def test_keychain_success_returns_credentials(self) -> None:
        mock_result = MagicMock()
        mock_result.stdout = self._make_sa_json()
        mock_creds = MagicMock()
        with (
            patch("subprocess.run", return_value=mock_result),
            patch(
                "google.oauth2.service_account.Credentials.from_service_account_info",
                return_value=mock_creds,
            ),
        ):
            result = build_credentials(_keychain_config())
        assert result is mock_creds


class TestUnknownMethod:
    def test_unknown_method_raises_config_error(self) -> None:
        config = AuthConfig.model_construct(method="unknown")  # type: ignore[call-arg]
        with pytest.raises((ConfigError, AuthError)):
            build_credentials(config)


class TestApiKey:
    def test_build_auth_api_key_reads_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
        result = build_auth(AuthConfig(method="api_key"))
        assert result.api_key == "test-key-123"
        assert result.credentials is None

    def test_build_auth_api_key_missing_raises_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(AuthError, match="GEMINI_API_KEY"):
            build_auth(AuthConfig(method="api_key"))

    def test_build_auth_api_key_empty_raises_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "  ")
        with pytest.raises(AuthError, match="GEMINI_API_KEY"):
            build_auth(AuthConfig(method="api_key"))

    def test_build_auth_api_key_custom_env_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_API_KEY", "custom-key")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        result = build_auth(AuthConfig(method="api_key", api_key_env="GOOGLE_API_KEY"))
        assert result.api_key == "custom-key"

    def test_build_auth_api_key_value_as_env_name_raises_auth_error(self) -> None:
        # User pasted the key itself into api_key_env instead of a variable name
        with pytest.raises(AuthError, match="looks like an API key"):
            build_auth(AuthConfig(method="api_key", api_key_env=FAKE_GOOGLE_API_KEY))

    def test_build_auth_vertex_returns_credentials(self) -> None:
        mock_creds = MagicMock()
        with patch("google.auth.default", return_value=(mock_creds, "project")):
            result = build_auth(AuthConfig(method="adc"))
        assert result.credentials is mock_creds
        assert result.api_key is None
