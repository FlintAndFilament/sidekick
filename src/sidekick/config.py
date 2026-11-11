"""
sidekick/config.py
-----------------------
Load and validate configuration from ~/.config/sidekick/config.json.

Responsibilities:
  - Define Config, AuthConfig, and FileToolsConfig pydantic models
  - Load config from the standard path (~/.config/sidekick/config.json)
  - Provide sensible defaults for all optional fields
  - Raise ConfigError with actionable messages on validation failure

Design notes:
  - Single Responsibility: config loading/validation only; no credentials, no SDK init
  - Open/Closed: add a new field to Config — nothing else changes
  - Dependency Inversion: callers depend on Config/AuthConfig types, not on JSON schema

Raises:
  ConfigError — wraps all config load/parse/validation failures

Used by:  auth.py, client.py, transcript.py, server.py
Imports:  sandbox.py (DEFAULT_DENY — stdlib-only module), pydantic
"""

import json
import os
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from sidekick.sandbox import DEFAULT_DENY

CONFIG_PATH: Path = Path.home() / ".config" / "sidekick" / "config.json"

# The plugin's masked "Gemini API key" option (plugin.json userConfig). Claude Code keeps the
# value in the OS credential store (Keychain on macOS) and hands it to the server under this
# name. Deliberately not GEMINI_API_KEY: a plugin env entry would shadow a real shell export.
PLUGIN_API_KEY_VAR = "SIDEKICK_API_KEY"

ThinkingLevel = Literal["none", "low", "medium", "high"]
AuthMethod = Literal["adc", "env", "keychain", "api_key"]


class ModelFamily(str, Enum):
    """Model generation family — drives thinking-API selection in client.py."""

    GEMINI_2 = "gemini-2"
    GEMINI_3 = "gemini-3"


class ConfigError(Exception):
    """Raised when config cannot be loaded or validated."""


class AuthConfig(BaseModel):
    method: AuthMethod = "adc"
    # None for adc/env; defaults applied by validator when method="keychain"
    keychain_service: Optional[str] = None
    keychain_account: Optional[str] = None
    # Only read when method="api_key"; stores env var NAME, never the key value
    api_key_env: Optional[str] = "GEMINI_API_KEY"

    @model_validator(mode="after")
    def apply_keychain_defaults(self) -> "AuthConfig":
        if self.method == "keychain":
            if self.keychain_service is None:
                self.keychain_service = "sidekick"
            if self.keychain_account is None:
                self.keychain_account = "vertex-sa"
        return self


class FileToolsConfig(BaseModel):
    """Gemini's sandboxed file tools. `deny` replaces the default list when set."""

    enabled: bool = True  # master kill switch — false = no tools declared to Gemini
    deny: list[str] = Field(default_factory=lambda: list(DEFAULT_DENY))
    max_write_bytes: int = Field(default=262_144, gt=0)


class WebToolsConfig(BaseModel):
    """Gemini's server-side web tools. Off by default: grounding bills per request."""

    enabled: bool = False  # the default answer for a call's `web` argument


class Config(BaseModel):
    project: Optional[str] = None  # required for adc/env/keychain; unused for api_key
    location: str = "global"
    default_thinking: ThinkingLevel = "medium"
    # Optional per-deployment default model; overrides the built-in DEFAULT_MODEL when set.
    # Omit to use the server's built-in default. Individual calls override via the model= param.
    default_model: Optional[str] = None
    transcript_dir: str = "./session-summaries"
    # Where architect / review (and opted-in brainstorm) save their output.
    artifacts_dir: str = "./sidekick-artifacts"
    file_tools: FileToolsConfig = FileToolsConfig()
    web_tools: WebToolsConfig = WebToolsConfig()
    auth: AuthConfig = AuthConfig()

    @model_validator(mode="after")
    def project_required_for_vertex(self) -> "Config":
        if self.auth.method != "api_key" and not self.project:
            raise ValueError(
                "'project' is required for Vertex AI auth methods (adc, env, keychain). "
                "Add it to config.json or switch to method='api_key'."
            )
        return self


def plugin_api_key() -> Optional[str]:
    """The key saved in the plugin's options, or None when that option is unset.

    An unset option may arrive as "" or, on some builds, as the literal `${user_config.…}`
    placeholder; both mean "not configured here"."""
    value = os.environ.get(PLUGIN_API_KEY_VAR, "").strip()
    if not value or value.startswith("${"):
        return None
    return value


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Load and validate config from path. Raises ConfigError on any failure.

    With the plugin's API key option filled in, no file is needed and a file that names no
    auth method defaults to api_key: install, paste the key, done (#116)."""
    if not path.exists():
        if plugin_api_key() is not None:
            return Config(auth=AuthConfig(method="api_key"))
        raise ConfigError(f"Config file not found: {path}\nRun /sidekick:setup in Claude Code.")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is not valid JSON ({path}): {exc}") from exc
    if isinstance(raw, dict) and plugin_api_key() is not None:
        auth = raw.get("auth")
        if not (isinstance(auth, dict) and "method" in auth):
            raw = {**raw, "auth": {**(auth if isinstance(auth, dict) else {}), "method": "api_key"}}
    try:
        return Config(**raw)
    except Exception as exc:
        raise ConfigError(f"Config validation failed: {exc}") from exc
