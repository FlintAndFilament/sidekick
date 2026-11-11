"""
sidekick/setup_cli.py
---------------------------
Non-interactive setup for the sidekick plugin (#102). Replaces setup.sh.

`/sidekick:setup` asks the questions in Claude Code and calls `write` with the answers as
argv, so no answer is ever parsed as shell, python or JSON text. `status` gives the command
the saved values to offer as defaults. Only the keys the wizard owns are changed (#85); the
write is atomic (#95); an unreadable config is backed up under a timestamp (#96).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from sidekick.auth import AuthError, _load_keychain, _looks_like_api_key
from sidekick.config import CONFIG_PATH, AuthConfig, Config

DEFAULTS: dict[str, Any] = {
    "auth_method": "adc",
    "project": "",
    "location": "global",
    "default_thinking": "medium",
    "default_model": "",
    "transcript_dir": "./session-summaries",
    "keychain_service": "sidekick",
    "keychain_account": "vertex-sa",
    "api_key_env": "GEMINI_API_KEY",
}
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class SetupError(Exception):
    """A reason to refuse the write. The config file is never touched."""


def _read(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """(config, error). A missing file is ({}, None); an unreadable one is (None, reason)."""
    if not path.exists():
        return {}, None
    try:
        loaded = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        return None, str(exc)
    if not isinstance(loaded, dict):
        return None, "top level is not a JSON object"
    return loaded, None


def _auth_dict(config: dict[str, Any]) -> dict[str, Any]:
    auth = config.get("auth")
    return auth if isinstance(auth, dict) else {}


def _valid_env_name(value: Any) -> bool:
    """True if value is a safe env var NAME — never a pasted key or other secret-shaped text."""
    return (
        isinstance(value, str) and not _looks_like_api_key(value) and bool(_ENV_NAME.match(value))
    )


def _values(config: dict[str, Any]) -> dict[str, Any]:
    auth = _auth_dict(config)
    # A saved api_key_env that isn't a valid NAME (e.g. a hand-edited or pasted key) is never
    # surfaced here: this dict feeds both `status`'s stdout and `_merge`'s prev-value lookups,
    # and a secret must never round-trip through either.
    raw_api_key_env = auth.get("api_key_env")
    api_key_env = raw_api_key_env if _valid_env_name(raw_api_key_env) else None
    saved = {
        "auth_method": auth.get("method"),
        "project": config.get("project"),
        "location": config.get("location"),
        "default_thinking": config.get("default_thinking"),
        "default_model": config.get("default_model"),
        "transcript_dir": config.get("transcript_dir"),
        "keychain_service": auth.get("keychain_service"),
        "keychain_account": auth.get("keychain_account"),
        "api_key_env": api_key_env,
    }
    return {k: (saved[k] if saved[k] is not None else DEFAULTS[k]) for k in DEFAULTS}


def _auth_methods() -> list[str]:
    methods = ["adc", "env", "api_key"]
    if sys.platform == "darwin":
        methods.insert(2, "keychain")
    return methods


def _has_gcloud() -> bool:
    # shutil.which() consults sys.platform itself and reaches for the Windows-only
    # _winapi module when it reads "win32" — which only exists on real Windows. Tests
    # patch sys.platform (the shared module, not a setup_cli-local copy) to check the
    # darwin-only keychain gating, so that lookup can raise here even off Windows.
    try:
        return shutil.which("gcloud") is not None
    except AttributeError:
        return False


def _adc_ok() -> bool:
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [gcloud, "auth", "application-default", "print-access-token"],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _keychain_problem(service: str, account: str) -> Optional[str]:
    try:
        _load_keychain(
            AuthConfig(method="keychain", keychain_service=service, keychain_account=account)
        )
    # AuthError is the documented failure; a keychain item that is valid JSON but not a
    # service account (or missing required fields) surfaces as ValueError/KeyError from
    # from_service_account_info instead — both must be caught here, not left to crash (#102).
    except (AuthError, ValueError, KeyError) as exc:
        return str(exc)
    return None


def status(path: Path) -> dict[str, Any]:
    config, error = _read(path)
    config = config or {}
    values = _values(config)
    raw_api_key_env = _auth_dict(config).get("api_key_env")
    return {
        "config_path": str(path),
        "config_exists": path.exists(),
        "config_error": error,
        "platform": sys.platform,
        "auth_methods": _auth_methods(),
        "values": values,
        "checks": {
            "api_key_env_set": bool(os.environ.get(values["api_key_env"], "").strip()),
            "google_application_credentials": os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or None,
            "gcloud": _has_gcloud(),
            # True when the saved api_key_env failed validation in _values() and was replaced
            # by the default — flags the problem without ever printing the invalid value itself.
            "api_key_env_invalid": isinstance(raw_api_key_env, str)
            and not _valid_env_name(raw_api_key_env),
        },
    }


def _merge(config: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    """Apply the answers to a copy of config. Raises SetupError; returns (new config, warnings)."""
    prev = _values(config)

    def pick(name: str) -> Any:
        value = getattr(args, name)
        return value if value is not None else prev[name]

    method = args.auth_method
    warnings: list[str] = []
    out = dict(config)
    auth = dict(out["auth"]) if isinstance(out.get("auth"), dict) else {}
    auth["method"] = method

    if method == "keychain":
        if sys.platform != "darwin":
            raise SetupError("Keychain auth requires macOS. Use adc, env or api_key here.")
        service, account = pick("keychain_service"), pick("keychain_account")
        problem = _keychain_problem(service, account)
        if problem:
            raise SetupError(problem)
        auth["keychain_service"], auth["keychain_account"] = service, account
    elif method == "api_key":
        name = pick("api_key_env")
        if _looks_like_api_key(name) or not _ENV_NAME.match(name):
            raise SetupError(
                "The API key env var must be a variable NAME like GEMINI_API_KEY, not the key itself."
            )
        auth["api_key_env"] = name
        # The plugin's key option is invisible here (plugin env reaches only the MCP server,
        # not the Bash tool), so a plugin-sourced key must not trigger env-var advice (#116).
        if args.key_source != "plugin" and not os.environ.get(name, "").strip():
            warnings.append(
                f"{name} is not set here. Set it in the environment Claude Code starts from, or put "
                "the key in the plugin's Gemini API key option instead."
            )
    elif method == "adc":
        if not _adc_ok():
            raise SetupError("ADC is not configured. Run: gcloud auth application-default login")
    elif method == "env" and args.sa_file_source != "plugin":
        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if not creds:
            warnings.append(
                "GOOGLE_APPLICATION_CREDENTIALS is not set. Export it before starting Claude Code."
            )
        elif not Path(creds).is_file():
            raise SetupError(f"GOOGLE_APPLICATION_CREDENTIALS points to a missing file: {creds}")
    out["auth"] = auth

    if method != "api_key":
        out["project"] = pick("project")
        out["location"] = pick("location")
    out["default_thinking"] = args.thinking or prev["default_thinking"]
    out["transcript_dir"] = pick("transcript_dir")
    model = args.default_model
    if model == "-":
        out.pop("default_model", None)
    elif model:
        out["default_model"] = model

    try:
        Config.model_validate(out)
    except ValidationError as exc:
        raise SetupError(exc.errors()[0]["msg"]) from exc
    return out, warnings


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    attempt = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.{stamp}-{attempt}.bak")
        attempt += 1
    path.replace(backup)
    return backup


def write(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    config, error = _read(path)
    new, warnings = _merge(config or {}, args)  # raises before any file is touched
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = _backup(path) if error is not None else None
        tmp.write_text(json.dumps(new, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SetupError(f"Could not write {path}: {exc}") from exc
    return {
        "ok": True,
        "config_path": str(path),
        "warnings": warnings,
        "backup": str(backup) if backup else None,
    }


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sidekick-setup")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("status", "write"):
        s = sub.add_parser(name)
        s.add_argument("--config", type=Path, default=CONFIG_PATH)
    w = sub.choices["write"]
    w.add_argument("--auth-method", required=True, choices=["adc", "env", "keychain", "api_key"])
    w.add_argument("--project")
    w.add_argument("--location")
    w.add_argument("--thinking", choices=["none", "low", "medium", "high"])
    w.add_argument("--default-model")
    w.add_argument("--transcript-dir")
    w.add_argument("--keychain-service")
    w.add_argument("--keychain-account")
    w.add_argument("--api-key-env")
    # Where the secret comes from. "plugin" = the plugin's own options, which this process
    # cannot see, so the env-var checks are skipped.
    w.add_argument("--key-source", choices=["plugin", "env"], default="env")
    w.add_argument("--sa-file-source", choices=["plugin", "env"], default="env")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "status":
        print(json.dumps(status(args.config), indent=2))
        return 0
    try:
        result = write(args.config, args)
    except SetupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
