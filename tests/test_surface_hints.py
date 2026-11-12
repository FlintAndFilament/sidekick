"""Tool descriptions and capability hints follow the default model's real surface."""

from datetime import datetime
from pathlib import Path

from sidekick.router import Router
from sidekick.server import build_server
from sidekick.tools.base import capability_hint, session_param_hint
from sidekick.transcript import TranscriptWriter
from tests._openrouter_fixtures import FakeOpenRouter, make_client
from tests.test_capability_metadata import _tools, _workspace


def _transcript(tmp_path: Path) -> TranscriptWriter:
    return TranscriptWriter(str(tmp_path / "t"), datetime.now())


def test_openrouter_default_says_paste_context(tmp_path):
    hint = capability_hint(_workspace(tmp_path), write=False, default_backend="openrouter")
    assert "Repository access: none for the default model" in hint
    assert "conversation only" in hint
    assert "list_dir" not in hint
    assert "Writes to disk" in hint
    assert "Web access: none for OpenRouter models" in hint


def test_override_to_gemini_is_described_only_when_gemini_exists(tmp_path):
    ws = _workspace(tmp_path)
    with_g = capability_hint(ws, write=True, default_backend="openrouter", gemini_available=True)
    without = capability_hint(ws, write=True, default_backend="openrouter", gemini_available=False)
    assert "runs the call on Gemini instead" in with_g
    assert "write_file" in with_g
    assert "Gemini" not in without
    assert "write_file" not in without


# Captured from capability_hint at the base commit (b860b2f), sandbox root substituted.
_GOLDEN_READ = (
    "\n\nRepository access: Gemini reads this repository itself with list_dir, glob, grep, "
    "read_file, sandboxed to {root} — the default deny-list blocks .git, .env*, and key and "
    "credential files, and everything else under the root is readable by path, though searches "
    "are not exhaustive: glob and grep skip dependency and cache directories, never follow "
    "symlinked directories, and cap their results, and read_file refuses binary files — so an "
    "empty result is not proof of absence. help (topic files) gives the specifics. Name the "
    "paths it should look at rather than pasting file contents into this call. Writes to disk: "
    "the bridge appends this exchange to the session transcript. Gemini itself cannot modify "
    "the working tree.\n\nWeb access: available, OFF by default — pass web=true to change it "
    "for a call. With it on, Gemini can google_search and url_context through the Gemini API. "
    "Retrieved pages are untrusted text, so treat conclusions drawn from them as claims to "
    "check."
)


def test_gemini_hint_is_unchanged(tmp_path):
    ws = _workspace(tmp_path)
    golden = _GOLDEN_READ.format(root=ws.sandbox.root)
    assert capability_hint(ws, write=False) == golden
    assert capability_hint(ws, write=False, default_backend="gemini") == golden
    assert capability_hint(ws, write=True) == capability_hint(
        ws, write=True, default_backend="gemini"
    )


def test_router_server_describes_openrouter(tmp_path):
    server = build_server(
        Router(make_client(FakeOpenRouter(), tmp_path)), _transcript(tmp_path), _workspace(tmp_path)
    )
    ask = _tools(server)["ask"]
    assert ask.description.startswith("Ask the sidekick model")
    assert "conversation only" in ask.description
    assert "x-ai/grok-4.7" in ask.inputSchema["properties"]["model"]["description"]
    assert "ctx" not in ask.inputSchema["properties"]


def test_session_hint_names_no_backend(tmp_path):
    assert "Gemini" not in session_param_hint(_workspace(tmp_path), write=True)
