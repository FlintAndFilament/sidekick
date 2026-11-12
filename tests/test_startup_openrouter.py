"""Startup wiring: which ModelSource _start() hands to build_server (#133)."""

from pathlib import Path
from typing import Callable, Optional

import pytest

from sidekick import __main__ as entry
from sidekick.auth import AuthError
from sidekick.client import GEMINI_UNPAID_NOTICE, GeminiClient
from sidekick.config import Config
from sidekick.router import Router
from tests._fakes import FAKE_OPENROUTER_KEY


def _boot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config: Config,
    build_auth: Optional[Callable[..., object]] = None,
) -> tuple[object, list[str]]:
    captured: list[object] = []
    served: list[str] = []
    monkeypatch.setattr("sidekick.config.load_config", lambda: config)
    monkeypatch.setattr(
        "sidekick.server.build_server", lambda source, *a, **k: captured.append(source)
    )
    monkeypatch.setattr("sidekick.openrouter.OpenRouterClient.refresh_catalog", lambda self: False)
    monkeypatch.setattr("sidekick.client.GeminiClient.refresh_latest", lambda self: {})
    monkeypatch.setattr("sidekick.workspace.project_root", lambda: tmp_path)
    monkeypatch.setattr("google.genai.Client", lambda *a, **k: object())
    if build_auth is not None:
        monkeypatch.setattr("sidekick.auth.build_auth", build_auth)
    monkeypatch.setattr(entry, "_serve_setup_problem", served.append)
    entry._start()
    return (captured[0] if captured else None), served


def test_openrouter_key_only_serves_openrouter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SIDEKICK_OPENROUTER_KEY", FAKE_OPENROUTER_KEY)
    cfg = Config.model_validate({"transcript_dir": str(tmp_path)}, context={"openrouter_key": True})
    src, _ = _boot(monkeypatch, tmp_path, cfg)
    assert isinstance(src, Router) and [b.name for b in src.backends()] == ["openrouter"]


def test_both_configured_serves_both_openrouter_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SIDEKICK_OPENROUTER_KEY", FAKE_OPENROUTER_KEY)
    monkeypatch.setenv("SIDEKICK_API_KEY", "g")
    src, _ = _boot(
        monkeypatch, tmp_path, Config(auth={"method": "api_key"}, transcript_dir=str(tmp_path))
    )
    assert isinstance(src, Router)
    assert [b.name for b in src.backends()] == ["openrouter", "gemini"]


def test_broken_gemini_with_openrouter_key_still_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SIDEKICK_OPENROUTER_KEY", FAKE_OPENROUTER_KEY)

    def boom(_: object) -> object:
        raise AuthError("Gemini auth error: Keychain item not found")

    src, served = _boot(
        monkeypatch,
        tmp_path,
        Config(project="p", auth={"method": "keychain"}, transcript_dir=str(tmp_path)),
        build_auth=boom,
    )
    assert isinstance(src, Router)
    assert "Keychain item not found" in (src.gemini_problem or "") and served == []


def test_without_openrouter_key_the_source_is_the_gemini_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SIDEKICK_OPENROUTER_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SIDEKICK_API_KEY", "g")
    src, _ = _boot(
        monkeypatch, tmp_path, Config(auth={"method": "api_key"}, transcript_dir=str(tmp_path))
    )
    assert type(src) is GeminiClient


async def test_api_key_mode_turns_on_the_unpaid_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SIDEKICK_OPENROUTER_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SIDEKICK_API_KEY", "g")
    src, _ = _boot(
        monkeypatch, tmp_path, Config(auth={"method": "api_key"}, transcript_dir=str(tmp_path))
    )
    assert isinstance(src, GeminiClient)
    assert (await src.preflight("gemini-3.5-flash", None)).notice == GEMINI_UNPAID_NOTICE
