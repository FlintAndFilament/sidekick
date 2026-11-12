"""Tests for the short server instructions and the help detail behind them (#78).

Claude Code keeps only about the first 2048 characters of a server's instructions and drops
the rest silently. These tests pin two things: the instructions fit that budget in every
config variant while still carrying the rules Claude must not miss, and everything cut from
them is still reachable through help.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pytest

from sidekick.backends import Surface
from sidekick.guide import HELP_TOPICS, INSTRUCTIONS_BUDGET, help_text, server_instructions
from sidekick.tools import CAPABILITIES, HELP_TOOL_NAME, LIST_MODELS_TOOL_NAME
from sidekick.transcript import TranscriptWriter
from sidekick.workspace import Workspace
from tests.test_capability_metadata import _server, _tools, _workspace

GENERATING = [c.name for c in CAPABILITIES]
PARAMS = ("session_name", "thinking", "model", "web", "write_artifact")


def _long_root(tmp_path: Path) -> Path:
    """A deep checkout path, so the budget is not only met for short tmp dirs."""
    root = tmp_path / ("very-long-directory-name-" * 6) / "projects" / ("repo-" * 8)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _variants(tmp_path: Path) -> dict[str, dict[str, Any]]:
    long_deny = [f"**/generated-secret-bundle-{i}/**" for i in range(40)]
    return {
        "files-on": {"workspace": _workspace(tmp_path)},
        "files-on-web-default": {"workspace": _workspace(tmp_path), "web_default": True},
        "files-off": {"workspace": _workspace(tmp_path, enabled=False)},
        "no-workspace": {"workspace": None},
        "vertex": {"workspace": _workspace(tmp_path), "web_supported": False},
        "long-root": {"workspace": _workspace(_long_root(tmp_path))},
        "long-deny": {"workspace": _workspace(tmp_path, deny=long_deny)},
        "openrouter": {
            "workspace": _workspace(tmp_path),
            "surface": Surface("openrouter", False, False, False, False),
        },
        "openrouter+gemini": {
            "workspace": _workspace(tmp_path),
            "surface": Surface("openrouter", False, False, False, True),
        },
        "openrouter-long-root": {
            "workspace": _workspace(_long_root(tmp_path)),
            "surface": Surface("openrouter", False, False, False, True),
        },
    }


def _instructions(tmp_path: Path, name: str) -> str:
    return server_instructions(**_variants(tmp_path)[name])


VARIANTS = (
    "files-on",
    "files-on-web-default",
    "files-off",
    "no-workspace",
    "vertex",
    "long-root",
    "long-deny",
    "openrouter",
    "openrouter+gemini",
    "openrouter-long-root",
)


class TestInstructionsFitTheClientCap:
    @pytest.mark.parametrize("variant", VARIANTS)
    def test_fits_the_budget(self, tmp_path: Path, variant: str) -> None:
        text = _instructions(tmp_path, variant)
        assert len(text) <= INSTRUCTIONS_BUDGET, f"{variant}: {len(text)} chars"

    def test_budget_leaves_margin_under_the_observed_cap(self) -> None:
        assert INSTRUCTIONS_BUDGET <= 2000

    @pytest.mark.parametrize("variant", VARIANTS)
    def test_names_every_tool_and_the_help_tool(self, tmp_path: Path, variant: str) -> None:
        text = _instructions(tmp_path, variant)
        for name in [*GENERATING, LIST_MODELS_TOOL_NAME, HELP_TOOL_NAME]:
            assert name in text, name

    @pytest.mark.parametrize("variant", VARIANTS)
    def test_names_every_behavior_parameter(self, tmp_path: Path, variant: str) -> None:
        text = _instructions(tmp_path, variant)
        for param in PARAMS:
            assert f"{param}" in text, param

    @pytest.mark.parametrize("variant", VARIANTS)
    def test_points_to_help_with_its_topics(self, tmp_path: Path, variant: str) -> None:
        text = _instructions(tmp_path, variant)
        for topic in ("files", "web", "sessions", "disk"):
            assert topic in text


INTRO = (
    "sidekick: a second opinion from the model you choose (OpenRouter, or Google Gemini directly)."
)


class TestOpenRouterSurface:
    @pytest.mark.parametrize("variant", VARIANTS)
    def test_every_variant_leads_with_the_choice(self, tmp_path: Path, variant: str) -> None:
        assert _instructions(tmp_path, variant).startswith(INTRO)

    def test_openrouter_instructions_say_paste_and_name_no_file_tools(self, tmp_path: Path) -> None:
        text = _instructions(tmp_path, "openrouter")
        assert "conversation only" in text
        assert "list_dir" not in text and "write_file" not in text
        assert "name paths" not in text and "Gemini reads" not in text

    def test_openrouter_with_gemini_points_to_the_override(self, tmp_path: Path) -> None:
        text = _instructions(tmp_path, "openrouter+gemini")
        assert "model=flash, flash-lite, pro or gemini-* runs on Gemini" in text

    def test_openrouter_web_row(self, tmp_path: Path) -> None:
        text = _instructions(tmp_path, "openrouter")
        assert "Web: none for OpenRouter models; web=true is ignored with a notice." in text

    def test_gemini_only_keeps_the_gemini_wording(self, tmp_path: Path) -> None:
        text = _instructions(tmp_path, "files-on")
        assert "Gemini reads files" in text and "name paths" in text

    @pytest.mark.parametrize("topic", ["files", "web"])
    def test_openrouter_help_topics_lead_with_the_sentence(
        self, tmp_path: Path, topic: str
    ) -> None:
        surface = Surface("openrouter", False, False, False, False)
        text = help_text(_workspace(tmp_path), topic=topic, surface=surface)
        assert "OpenRouter" in text and "Gemini reads" not in text

    def test_models_topic_explains_routing_prices_and_privacy(self, tmp_path: Path) -> None:
        surface = Surface("openrouter", False, False, False, True)
        text = help_text(_workspace(tmp_path), topic="models", surface=surface)
        for needle in (
            'provider.data_collection: "deny"',
            "Safety Retention",
            "not guaranteed",
            "openrouter.ai/settings/privacy",
            "20 requests a minute",
            "accepted_models",
            "google/<id>",
        ):
            assert needle in text, needle

    def test_models_topic_on_a_gemini_only_server_does_not_promise_openrouter(
        self, tmp_path: Path
    ) -> None:
        for surface in (None, Surface("gemini", True, True, False, True)):
            text = help_text(_workspace(tmp_path), topic="models", surface=surface)
            assert "need an OpenRouter key" in text and "/sidekick:setup" in text
            assert "unpaid tier" in text and "flash-lite" in text
            assert "openrouter.default_model" not in text and "goes to OpenRouter" not in text

    def test_models_is_a_help_topic(self) -> None:
        assert "models" in HELP_TOPICS


class TestKeyRulesSurviveTheCut:
    """The rules #77 added, which the old 4.6k-char text put past the cap."""

    def test_when_to_use_web(self, tmp_path: Path) -> None:
        text = _instructions(tmp_path, "files-on")
        assert "web=true" in text
        assert "training data" in text

    def test_web_and_write_are_exclusive_and_need_a_new_session(self, tmp_path: Path) -> None:
        text = _instructions(tmp_path, "files-on")
        assert "removes write_file" in text
        assert "new session_name" in text

    def test_names_which_tools_can_write(self, tmp_path: Path) -> None:
        text = _instructions(tmp_path, "files-on")
        writers = [c.name for c in CAPABILITIES if c.write]
        line = next(ln for ln in text.splitlines() if "write_file" in ln and "create" in ln)
        for cap in CAPABILITIES:
            assert (cap.name in line) == (cap.name in writers), cap.name

    def test_steers_away_from_pasting(self, tmp_path: Path) -> None:
        assert "name paths" in _instructions(tmp_path, "files-on")

    def test_files_off_offers_no_write(self, tmp_path: Path) -> None:
        text = _instructions(tmp_path, "files-off")
        assert "write_file" not in text
        assert "file_tools.enabled=false" in text

    def test_vertex_says_web_is_unavailable_and_drops_the_exclusion(self, tmp_path: Path) -> None:
        text = _instructions(tmp_path, "vertex")
        assert "UNAVAILABLE" in text
        assert "removes write_file" not in text

    def test_web_default_state_is_derived(self, tmp_path: Path) -> None:
        assert "Web: OFF by default" in _instructions(tmp_path, "files-on")
        assert "Web: ON by default" in _instructions(tmp_path, "files-on-web-default")

    def test_long_deny_list_is_summarised_not_listed(self, tmp_path: Path) -> None:
        text = _instructions(tmp_path, "long-deny")
        assert "generated-secret-bundle-39" not in text
        assert "40" in text


def _help(workspace: Optional[Workspace], topic: Optional[str] = None, **kw: Any) -> str:
    return help_text(workspace, topic=topic, **kw)


class TestHelpText:
    def test_topics_cover_every_generating_tool(self) -> None:
        for name in GENERATING:
            assert name in HELP_TOPICS

    @pytest.mark.parametrize("topic", HELP_TOPICS)
    def test_every_topic_renders(self, tmp_path: Path, topic: str) -> None:
        assert _help(_workspace(tmp_path), topic).strip()

    def test_full_help_holds_the_detail_cut_from_the_instructions(self, tmp_path: Path) -> None:
        transcript = TranscriptWriter(str(tmp_path / "transcripts"), datetime.now())
        text = _help(_workspace(tmp_path), transcript=transcript)
        for detail in (
            str(transcript.path),
            ".env.*",
            "node_modules",
            "symlinked directories",
            "truncated=true",
            "MUTUALLY EXCLUSIVE",
            "attacker-controlled",
        ):
            assert detail in text, detail

    def test_full_help_contains_every_topic(self, tmp_path: Path) -> None:
        ws = _workspace(tmp_path)
        full = _help(ws)
        for topic in HELP_TOPICS:
            assert _help(ws, topic) in full, topic

    def test_topic_is_case_and_space_insensitive(self, tmp_path: Path) -> None:
        ws = _workspace(tmp_path)
        assert _help(ws, "  Web ") == _help(ws, "web")

    def test_unknown_topic_lists_the_valid_ones(self, tmp_path: Path) -> None:
        text = _help(_workspace(tmp_path), "nope")
        assert "nope" in text
        for topic in HELP_TOPICS:
            assert topic in text

    def test_tool_topic_carries_its_full_description(self, tmp_path: Path) -> None:
        for cap in CAPABILITIES:
            assert cap.summary in _help(_workspace(tmp_path), cap.name)

    def test_sessions_topic_gives_write_advice_only_where_writes_exist(
        self, tmp_path: Path
    ) -> None:
        assert "new name" in _help(_workspace(tmp_path), "sessions")
        assert "before asking for writes" in _help(_workspace(tmp_path), "sessions")
        off = _help(_workspace(tmp_path, enabled=False), "sessions")
        assert "before asking for writes" not in off


class TestHelpTool:
    def test_is_registered_and_writes_nothing(self, tmp_path: Path) -> None:
        tool = _tools(_server(tmp_path, _workspace(tmp_path)))[HELP_TOOL_NAME]
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False

    def test_description_lists_the_topics(self, tmp_path: Path) -> None:
        tool = _tools(_server(tmp_path, _workspace(tmp_path)))[HELP_TOOL_NAME]
        for topic in ("files", "web", "sessions", "disk"):
            assert topic in (tool.description or "")

    def test_call_returns_the_topic(self, tmp_path: Path) -> None:
        mcp = _server(tmp_path, _workspace(tmp_path))
        result = asyncio.run(mcp.call_tool(HELP_TOOL_NAME, {"topic": "web"}))
        blocks = result[0] if isinstance(result, tuple) else result
        text = "".join(getattr(b, "text", "") for b in blocks)  # type: ignore[union-attr]
        assert "web=true" in text

    def test_built_server_instructions_fit(self, tmp_path: Path) -> None:
        mcp = _server(tmp_path, _workspace(tmp_path))
        assert mcp.instructions is not None
        assert len(mcp.instructions) <= INSTRUCTIONS_BUDGET
