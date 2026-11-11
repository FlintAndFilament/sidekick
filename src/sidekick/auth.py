"""
sidekick/auth.py
---------------------
Credential loading for all supported authentication methods.

Responsibilities:
  - Load Application Default Credentials (ADC) — default method
  - Load service account credentials from GOOGLE_APPLICATION_CREDENTIALS env var
  - Load service account credentials from Apple Keychain (macOS)
  - Read the Google AI Studio API key: plugin option first, then the configured env var
  - Build a google.auth.credentials.Credentials instance for any configured method

Design notes:
  - Single Responsibility: credential loading only; SDK client construction is in client.py
  - Open/Closed: add a new method by adding a loader function + one entry in _LOADERS —
    build_credentials() requires no modification
  - Liskov Substitution: all paths return google.auth.credentials.Credentials; callers are agnostic
  - Dependency Inversion: client.py depends on the Credentials abstraction, not concrete loaders

Raises:
  AuthError — wraps all credential failures with actionable messages for Claude to surface

Used by:  __main__.py -> build_auth()
Imports:  config.py (AuthConfig, ConfigError)
"""

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, cast

_log = logging.getLogger(__name__)

import google.auth
import google.auth.credentials
import google.auth.exceptions
from google.oauth2 import service_account

from sidekick.config import (
    PLUGIN_SA_FILE_VAR,
    AuthConfig,
    ConfigError,
    plugin_api_key,
    plugin_option,
)

_VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# All loaders receive the full AuthConfig so parameterized methods (keychain)
# can access their fields without special-casing in build_credentials().
_CredLoader = Callable[[AuthConfig], google.auth.credentials.Credentials]


class AuthError(Exception):
    """Raised when credential loading fails. Message is user-actionable."""


@dataclass
class AuthResult:
    """Unified auth output — exactly one field is non-None after build_auth() succeeds."""

    credentials: Optional[google.auth.credentials.Credentials] = None
    api_key: Optional[str] = None


def _load_adc(auth_config: AuthConfig) -> google.auth.credentials.Credentials:
    try:
        credentials, _ = google.auth.default(scopes=_VERTEX_SCOPES)
        _log.debug("adc credentials loaded")
        return credentials
    except google.auth.exceptions.DefaultCredentialsError as exc:
        _log.error("adc credential load failed: %s", exc)
        raise AuthError(
            "Gemini auth error: no ADC credentials found.\n"
            "Fix: gcloud auth application-default login"
        ) from exc


_ENV_CREDENTIALS_VAR = "GOOGLE_APPLICATION_CREDENTIALS"
_ENV_FIX = (
    "Fix: in Claude Code run /plugin configure sidekick@sidekick, pick the file on the "
    "**Service-account key file** row and press Enter, then run /reload-plugins. "
    f"(Or set {_ENV_CREDENTIALS_VAR} in the environment Claude Code starts from.)"
)


def _not_a_path(value: str) -> bool:
    """True unless value is shaped like a file path, so only a path is ever echoed (#119).

    An allow-list, not a key-shape block-list: Google API keys come in more than one prefix
    (`AIza…`, newer `AQ…`), and any of them pasted here must never reach a log or message."""
    if len(value) > 1024 or any(marker in value for marker in ("{", "\n", "private_key", "BEGIN ")):
        return True
    return not ("/" in value or "\\" in value or value.startswith("~") or value.endswith(".json"))


def _load_env(auth_config: AuthConfig) -> google.auth.credentials.Credentials:
    """Load credentials from the plugin's file option, else GOOGLE_APPLICATION_CREDENTIALS.

    The variable and the file are checked here, and the file is then loaded explicitly, because
    google.auth.default() falls back to the user's gcloud ADC when the variable is unset. That
    fallback would silently run the server as the user instead of the configured service
    account, and only fail if ADC were missing too (#86).
    """
    from_plugin = plugin_option(PLUGIN_SA_FILE_VAR)
    path = from_plugin or os.environ.get(_ENV_CREDENTIALS_VAR, "").strip()
    source = "the plugin's service-account file option" if from_plugin else _ENV_CREDENTIALS_VAR
    if not path:
        _log.error(
            "no service-account file: plugin option unset and %s not set", _ENV_CREDENTIALS_VAR
        )
        raise AuthError(
            "Gemini auth error: auth method is 'env' but no service-account key file is set "
            f"(plugin option empty, {_ENV_CREDENTIALS_VAR} not set).\n{_ENV_FIX}"
        )
    if _not_a_path(path):
        # Never echo it: a mis-pasted API key or SA JSON would land in the log, in setup_status
        # and so in the model's context (#116).
        _log.error("%s holds something that is not a file path (value withheld)", source)
        raise AuthError(
            f"Gemini auth error: {source} holds something that isn't a file path (it looks like "
            "a key or key JSON, so it is not shown). Clear it and pick the JSON key file "
            "instead.\n" + _ENV_FIX
        )
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        _log.error("%s points at a missing file: %s", source, path)
        raise AuthError(
            f"Gemini auth error: {source} points at a file that does not exist: {path}\n{_ENV_FIX}"
        )
    # Not google.auth.load_credentials_from_file: it accepts any credential type, including an
    # external-account config that makes google-auth fetch tokens from URLs the file names, and
    # google-auth deprecates it for that reason. This method takes a service-account key only (#124).
    try:
        with open(path, encoding="utf-8") as fh:
            info = json.load(fh)
    except (ValueError, OSError) as exc:
        _log.error("env credential file unreadable: %s", type(exc).__name__)
        raise AuthError(
            f"Gemini auth error: the service-account file from {source} could not be loaded: "
            f"{path}\n(it is not readable JSON: {type(exc).__name__})\n{_ENV_FIX}"
        ) from exc
    cred_type = info.get("type") if isinstance(info, dict) else None
    if cred_type != "service_account":
        _log.error("env credential file is type %r, not service_account", cred_type)
        raise AuthError(
            f"Gemini auth error: the file from {source} is not a service-account key "
            f"(its type is {cred_type!r}, expected 'service_account'): {path}\n{_ENV_FIX}"
        )
    try:
        loaded = _service_account_credentials(info)
    except (google.auth.exceptions.GoogleAuthError, ValueError, KeyError, TypeError) as exc:
        _log.error("env credential load failed: %s", type(exc).__name__)
        raise AuthError(
            f"Gemini auth error: the service-account file from {source} could not be loaded: "
            f"{path}\n({type(exc).__name__}: the key is missing or has a malformed field)\n"
            f"{_ENV_FIX}"
        ) from exc
    _log.debug("env credentials loaded from %s", path)
    return loaded


def _service_account_credentials(info: dict[str, object]) -> google.auth.credentials.Credentials:
    """Agent Platform credentials from parsed service-account key JSON (env and keychain)."""
    # google-auth ships no annotation for this constructor; the return type is documented.
    creds = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
        info, scopes=_VERTEX_SCOPES
    )
    return cast(google.auth.credentials.Credentials, creds)


def _keychain_store_fix(service: str, account: str) -> str:
    """How to (re-)store the SA JSON by hand; /sidekick:setup can't store it yet (#123).

    `-w` with no value would prompt instead of putting the JSON on argv, but the prompt reads a
    single line (and a terminal line caps at 1024 bytes), so a multi-line SA key can't go through
    it. `$(cat …)` is on argv only while `security` runs; the hex decode below reads it back."""
    return (
        "Fix: store the service account key from a terminal (never paste it into the chat):\n"
        f"  security add-generic-password -U -s '{service}' -a '{account}' "
        '-w "$(cat /path/to/sa-key.json)"\n'
        "then delete the JSON file and run /reload-plugins in Claude Code. "
        "See docs/auth.md, Method 3: Apple Keychain."
    )


def _load_keychain(auth_config: AuthConfig) -> google.auth.credentials.Credentials:
    """Load service account JSON from Apple Keychain (macOS only)."""
    # model_validator guarantees non-None when method="keychain"; or-defaults are a safety net
    service = auth_config.keychain_service or "sidekick"
    account = auth_config.keychain_account or "vertex-sa"
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        _log.error("keychain item not found (service=%r, account=%r)", service, account)
        raise AuthError(
            f"Gemini auth error: Keychain item not found "
            f"(service={service!r}, account={account!r}).\n" + _keychain_store_fix(service, account)
        ) from exc
    except FileNotFoundError as exc:
        _log.error("security CLI not found — Keychain auth requires macOS")
        raise AuthError(
            "Gemini auth error: 'security' CLI not found. Keychain auth requires macOS."
        ) from exc

    raw = result.stdout.strip()
    # macOS Keychain stores multi-line values (like SA JSON) as binary and returns
    # them as a lowercase hex string via security(1) -w. Detect and decode.
    if raw and all(c in "0123456789abcdef" for c in raw):
        try:
            raw = bytes.fromhex(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            _log.error("keychain value hex decode failed: %s", exc)
            raise AuthError(
                "Gemini auth error: Keychain value could not be decoded.\n"
                + _keychain_store_fix(service, account)
            ) from exc
    try:
        sa_info = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.error("keychain value is not valid JSON: %s", exc)
        raise AuthError(
            "Gemini auth error: Keychain value is not valid service account JSON.\n"
            + _keychain_store_fix(service, account)
        ) from exc

    _log.debug("keychain credentials loaded (service=%r, account=%r)", service, account)

    try:
        return _service_account_credentials(sa_info)
    # Valid JSON that is not a service account (or lacks fields) raises these, not AuthError.
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        _log.error("keychain value is not a service account (%s)", type(exc).__name__)
        raise AuthError(
            "Gemini auth error: Keychain value is not valid service account JSON.\n"
            + _keychain_store_fix(service, account)
        ) from exc


def _looks_like_api_key(value: str) -> bool:
    """Return True if value looks like an API key rather than an env var name."""
    return value.startswith("AIza") or not value.replace("_", "").isupper() or len(value) > 40


_CONFIGURE_HINT = (
    "Fix: in Claude Code run /sidekick:setup, or run /plugin configure sidekick@sidekick, paste "
    "your key on the Gemini API key row (stored in your system keychain) and press Enter, then "
    "run /reload-plugins.\n"
    "Get a key at: https://aistudio.google.com/apikey"
)


def _load_api_key(auth_config: AuthConfig) -> str:
    """Read the Gemini Developer API key. Raises AuthError if neither source has one.

    The plugin option (Claude Code secure storage) wins; otherwise the shell env var named by
    api_key_env, for users who export it themselves (#116)."""
    from_plugin = plugin_api_key()
    if from_plugin is not None:
        _log.debug("api_key loaded from the plugin option")
        return from_plugin
    env_var = auth_config.api_key_env or "GEMINI_API_KEY"
    if _looks_like_api_key(env_var):
        _log.error("api_key_env looks like a key value, not a variable name")
        raise AuthError(
            "Gemini config error: 'api_key_env' in config.json contains what looks like "
            "an API key, not an environment variable name.\n"
            'Fix: set api_key_env to the variable name (e.g. "GEMINI_API_KEY"), or remove '
            "the key from config.json and store it in the plugin's options instead.\n"
            + _CONFIGURE_HINT
        )
    key = os.environ.get(env_var, "").strip()
    if not key:
        _log.error("no api key: plugin option unset and env var %s not set", env_var)
        raise AuthError(
            f"Gemini auth error: no API key. The plugin's key option is empty and env var "
            f"{env_var!r} is not set.\n" + _CONFIGURE_HINT
        )
    _log.debug("api_key loaded from %s", env_var)
    return key


_LOADERS: dict[str, _CredLoader] = {
    "adc": _load_adc,
    "env": _load_env,
    "keychain": _load_keychain,
}


def build_credentials(auth_config: AuthConfig) -> google.auth.credentials.Credentials:
    """Build Agent Platform (Vertex AI) credentials from the given auth config. Raises AuthError on failure."""
    loader = _LOADERS.get(auth_config.method)
    if loader is None:
        raise ConfigError(f"Unknown auth method: {auth_config.method!r}")
    return loader(auth_config)


def build_auth(auth_config: AuthConfig) -> AuthResult:
    """Unified auth dispatch for all methods. Raises AuthError or ConfigError on failure."""
    if auth_config.method == "api_key":
        return AuthResult(api_key=_load_api_key(auth_config))
    loader = _LOADERS.get(auth_config.method)
    if loader is None:
        raise ConfigError(f"Unknown auth method: {auth_config.method!r}")
    return AuthResult(credentials=loader(auth_config))
