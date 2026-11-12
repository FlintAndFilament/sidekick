"""
sidekick/setup_cli.py
---------------------------
Non-interactive setup for the sidekick plugin (#102). Replaces setup.sh.

`/sidekick:setup` asks the questions in Claude Code and calls `write` with the answers as
argv, so no answer is ever parsed as shell, python or JSON text. `status` gives the command
the saved values to offer as defaults. Only the keys the wizard owns are changed (#85); the
write is atomic (#95); an unreadable config is backed up under a timestamp (#96).

`keychain-store` puts a service-account key file into the macOS Keychain (#123). It takes the
file's PATH; this process reads the key and hands it to `security` on stdin, so the key never
reaches argv (where `ps` sees it), the chat, or the model.
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
from sidekick.config import (
    CONFIG_PATH,
    OPENROUTER_ENV_VAR,
    OPENROUTER_ID,
    AuthConfig,
    Config,
    OpenRouterConfig,
)

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
_OPENROUTER_ID = OPENROUTER_ID


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


# Keychain names go inside a single-quoted `security -i` command line, so allow no quoting
# or escape characters at all. Real service/account names never need them.
_KEYCHAIN_NAME = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
# `security -i` reads one command per line and fails past ~4096 characters (measured on
# macOS 27); stay well under so a key is refused rather than stored cut short.
_SECURITY_LINE_MAX = 4000
_SA_REQUIRED = ("private_key", "client_email", "token_uri")


def _read_sa_key(file: Path) -> str:
    """The key file as compact JSON, after checking it is a service-account key.

    Raises SetupError. No message ever includes the file's contents."""
    try:
        info = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SetupError(f"No such file: {file}") from exc
    except (ValueError, OSError) as exc:
        raise SetupError(f"{file} is not a readable JSON key file ({type(exc).__name__}).") from exc
    if not isinstance(info, dict) or info.get("type") != "service_account":
        found = info.get("type") if isinstance(info, dict) else None
        raise SetupError(
            f"{file} is not a service-account key (type {found!r}, expected 'service_account')."
        )
    missing = [k for k in _SA_REQUIRED if not isinstance(info.get(k), str) or not info[k]]
    if missing:
        raise SetupError(f"{file} is missing service-account fields: {', '.join(missing)}.")
    return json.dumps(info, separators=(",", ":"))


def _security_store_line(service: str, account: str, secret: str) -> str:
    """One `security -i` command that stores `secret`. Its parser treats backslash as an escape
    even inside single quotes, so backslashes and quotes are escaped to round-trip exactly."""
    escaped = secret.replace("\\", "\\\\").replace("'", "\\'")
    return f"add-generic-password -U -s '{service}' -a '{account}' -w '{escaped}'\n"


def _keychain_value(service: str, account: str) -> Optional[str]:
    """The item's stored value, hex-decoded when macOS returns it that way; None if unreadable."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.rstrip("\n")
    try:
        return bytes.fromhex(raw).decode("utf-8")
    except ValueError:
        return raw


def keychain_store(file: Path, service: str, account: str, delete_file: bool) -> dict[str, Any]:
    """Store a service-account key file in the macOS Keychain, then read it back. Raises SetupError."""
    if sys.platform != "darwin":
        raise SetupError("Keychain storage requires macOS. Use adc, env or api_key here.")
    for label, name in (("service", service), ("account", account)):
        if not _KEYCHAIN_NAME.match(name):
            raise SetupError(
                f"Keychain {label} name must be 1-64 letters, digits, '.', '_', '@' or '-'."
            )
    file = file.expanduser()
    secret = _read_sa_key(file)
    line = _security_store_line(service, account, secret)
    if len(line) > _SECURITY_LINE_MAX:
        raise SetupError(
            f"{file} is too large to store through the security tool "
            f"({len(line)} > {_SECURITY_LINE_MAX} characters). Store it by hand; see docs/auth.md."
        )
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv; the key goes on stdin only
            ["security", "-i"], input=line, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"Could not run the macOS security tool ({type(exc).__name__}).") from exc
    # Never relay security's stderr: on a parse error it echoes the command line, key included.
    if result.returncode != 0 or result.stderr.strip():
        raise SetupError(
            f"The security tool refused to store the key (exit {result.returncode}). "
            "Nothing was written to config."
        )
    # The item must now hold exactly this key. A credentials check alone would pass on an
    # older item at the same names, and --delete-file would then delete the only new copy.
    if _keychain_value(service, account) != secret:
        raise SetupError(
            "The key was not stored: the Keychain item does not hold this file's key. "
            "The file was kept."
        )
    problem = _keychain_problem(service, account)
    if problem:
        raise SetupError(f"Stored, but reading it back failed: {problem}")
    deleted = False
    if delete_file:
        try:
            file.unlink()
            deleted = True
        except OSError as exc:
            raise SetupError(
                f"Stored and verified, but {file} could not be deleted ({type(exc).__name__}). "
                "Delete it yourself."
            ) from exc
    return {
        "ok": True,
        "keychain_service": service,
        "keychain_account": account,
        "file": str(file),
        "file_deleted": deleted,
    }


def _openrouter_values(config: dict[str, Any]) -> dict[str, Any]:
    """The saved OpenRouter answers. Every id passes _OPENROUTER_ID, so a key saved where a
    model id belongs is never surfaced."""
    section = config.get("openrouter")
    section = section if isinstance(section, dict) else {}
    default = section.get("default_model")
    accepted = section.get("accepted_models")
    return {
        "openrouter_default_model": default
        if isinstance(default, str) and _OPENROUTER_ID.match(default)
        else "",
        "block_data_collection": section.get("block_data_collection") is True,
        "openrouter_accepted_models": [
            m
            for m in (accepted if isinstance(accepted, list) else [])
            if isinstance(m, str) and _OPENROUTER_ID.match(m)
        ],
    }


def status(path: Path) -> dict[str, Any]:
    config, error = _read(path)
    config = config or {}
    values = _values(config)
    values.update(_openrouter_values(config))
    raw_default = (
        (config.get("openrouter") or {}).get("default_model")
        if isinstance(config.get("openrouter"), dict)
        else None
    )
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
            # The plugin's key option is invisible here; this sees only a shell export.
            "openrouter_key_env_set": bool(os.environ.get(OPENROUTER_ENV_VAR, "").strip()),
            "openrouter_default_model_invalid": raw_default is not None
            and not values["openrouter_default_model"],
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


def _save(path: Path, new: dict[str, Any], error: Optional[str]) -> Optional[Path]:
    """Write new atomically; back up an unreadable old file first. Returns the backup path."""
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = _backup(path) if error is not None else None
        tmp.write_text(json.dumps(new, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SetupError(f"Could not write {path}: {exc}") from exc
    return backup


def write(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    config, error = _read(path)
    new, warnings = _merge(config or {}, args)  # raises before any file is touched
    backup = _save(path, new, error)
    return {
        "ok": True,
        "config_path": str(path),
        "warnings": warnings,
        "backup": str(backup) if backup else None,
    }


def openrouter_write(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Merge the OpenRouter answers into config.json. Raises SetupError; never sees a key."""
    config, error = _read(path)
    accept = args.accept or []
    if args.default_model is None and not accept and args.block_data_collection is None:
        raise SetupError(
            "Nothing to change: pass --default-model, --accept or --block-data-collection."
        )
    ids = ([args.default_model] if args.default_model not in (None, "-") else []) + accept
    if not all(_OPENROUTER_ID.match(i) for i in ids):
        raise SetupError(
            "That is not an OpenRouter model id (author/model, e.g. x-ai/grok-4.7). If you "
            "pasted an API key, it belongs in the plugin's masked OpenRouter API key option, "
            "never in the chat."
        )
    out = dict(config or {})
    section = dict(out["openrouter"]) if isinstance(out.get("openrouter"), dict) else {}
    if args.default_model == "-":
        section.pop("default_model", None)
    elif args.default_model:
        section["default_model"] = args.default_model
    if accept:
        saved = section.get("accepted_models")
        merged = [m for m in (saved if isinstance(saved, list) else []) if isinstance(m, str)]
        merged += [m for m in dict.fromkeys(accept) if m not in merged]
        section["accepted_models"] = merged
    if args.block_data_collection is not None:
        section["block_data_collection"] = args.block_data_collection == "on"
    try:
        OpenRouterConfig.model_validate(section)
    except ValidationError as exc:
        raise SetupError(exc.errors()[0]["msg"]) from exc
    out["openrouter"] = section
    backup = _save(path, out, error)
    return {
        "ok": True,
        "config_path": str(path),
        "openrouter": section,
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
    o = sub.add_parser("openrouter")
    o.add_argument("--config", type=Path, default=CONFIG_PATH)
    o.add_argument("--default-model")
    o.add_argument("--accept", action="append")
    o.add_argument("--block-data-collection", choices=["on", "off"])
    k = sub.add_parser("keychain-store")
    k.add_argument("--file", type=Path, required=True)
    k.add_argument("--keychain-service", default=DEFAULTS["keychain_service"])
    k.add_argument("--keychain-account", default=DEFAULTS["keychain_account"])
    k.add_argument("--delete-file", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "status":
        print(json.dumps(status(args.config), indent=2))
        return 0
    try:
        if args.command == "keychain-store":
            result = keychain_store(
                args.file, args.keychain_service, args.keychain_account, args.delete_file
            )
        elif args.command == "openrouter":
            result = openrouter_write(args.config, args)
        else:
            result = write(args.config, args)
    except SetupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
