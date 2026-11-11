"""
sidekick/server.py
------------------------
MCP server construction and tool registration.

Responsibilities:
  - Build the FastMCP server instance
  - Accept pre-constructed GeminiClient, TranscriptWriter, and Workspace (injected by __main__.py)
  - Register all 8 tools by calling each tool module's register() function
  - Send the short instructions from guide.py and wire help to its full detail (#74, #78)
  - Return the configured server instance for running

Design notes:
  - Single Responsibility: server wiring only; no config loading, no credential logic
  - Open/Closed: add a new tool by importing its register() + one function call — nothing else changes
  - Dependency Inversion: depends on GeminiClient and TranscriptWriter abstractions, not concrete init

Raises:
  (none) — startup errors surface from __main__.py where they are caught and reported

Used by:  __main__.py -> build_server()
Imports:  client.py (GeminiClient), guide.py, transcript.py (TranscriptWriter),
          tools/__init__.py, workspace.py (Workspace)
"""

from typing import Optional

from mcp.server.fastmcp import FastMCP

from sidekick.client import GeminiClient
from sidekick.guide import HELP_TOPICS, help_text, server_instructions
from sidekick.tools import (
    register_architect,
    register_ask,
    register_brainstorm,
    register_debug,
    register_help,
    register_list_models,
    register_list_sessions,
    register_review,
)
from sidekick.transcript import TranscriptWriter
from sidekick.workspace import Workspace

__all__ = ["build_server", "build_setup_server", "server_instructions"]

_SERVER_NAME = "sidekick"


def build_server(
    client: GeminiClient,
    transcript: TranscriptWriter,
    workspace: Optional[Workspace] = None,
) -> FastMCP:
    """Construct and return the configured MCP server with all tools registered.

    `workspace` gives the generating tools repo file access; None disables it entirely."""
    mcp = FastMCP(
        _SERVER_NAME,
        instructions=server_instructions(
            workspace, transcript, client.web_default, client.web_supported
        ),
    )
    register_ask(mcp, client, transcript, workspace)
    register_brainstorm(mcp, client, transcript, workspace)
    register_review(mcp, client, transcript, workspace)
    register_debug(mcp, client, transcript, workspace)
    register_architect(mcp, client, transcript, workspace)
    register_list_models(mcp, client, transcript)
    register_list_sessions(mcp, client)
    register_help(
        mcp,
        lambda topic: help_text(
            workspace, transcript, client.web_default, client.web_supported, topic=topic
        ),
        HELP_TOPICS,
    )
    return mcp


def setup_needed_text(problem: str) -> str:
    """What Claude is told when the server could not start for real (#116)."""
    return (
        "sidekick is installed but not set up yet, so its Gemini tools are unavailable.\n\n"
        f"Problem:\n{problem}\n\n"
        "Tell the user this in one or two lines and offer to run /sidekick:setup. After they fix "
        "it, they run /reload-plugins (or /mcp → plugin:sidekick:mcp → Reconnect)."
    )


def build_setup_server(problem: str) -> FastMCP:
    """A server that stays connected when config or auth is broken.

    Exiting would leave only "Connection closed" in /mcp with no hint; this way Claude can
    read the problem and walk the user through the fix."""
    text = setup_needed_text(problem)
    mcp = FastMCP(_SERVER_NAME, instructions=text)

    @mcp.tool(
        name="setup_status",
        description="Why sidekick's Gemini tools are unavailable and how to fix it.",
    )
    def setup_status() -> str:
        return text

    return mcp
