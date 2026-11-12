"""
sidekick/tools/list_sessions.py
------------------------------------
MCP tool: list_sessions — the conversations this server holds in memory right now (#15).

Responsibilities:
  - Register the list_sessions MCP tool
  - Render each live session as tool, session_name, model and committed turns

Design notes:
  - Single Responsibility: formatting only; the session cache and its LRU order live in
    each backend, read through ModelSource.sessions()
  - No API call, no transcript, no artifact: it reads process memory and writes nothing
  - There is no new_session tool: a new session_name already starts a fresh conversation,
    and every generating tool's session_name description says so

Used by:  tools/__init__.py -> register_list_sessions(), server.py
Imports:  backends.py (ModelSource, Session), client.py (MAX_SESSIONS), tools/base.py (ToolResult)
"""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from sidekick.backends import ModelSource, Session
from sidekick.client import MAX_SESSIONS
from sidekick.tools.base import ToolResult

_TOOL_NAME = "list_sessions"
LIST_SESSIONS_TOOL_NAME = _TOOL_NAME

_HEADER = ("tool", "session_name", "model", "turns")


def _turns(session: Session) -> int:
    """Committed exchanges: each backend's ask() stores one user prompt per successful call."""
    return sum(1 for content in session.history if content.role == "user")


def format_session_list(sessions: list[Session]) -> str:
    """Render the sessions, most recently used first, as an aligned table."""
    if not sessions:
        return (
            "No conversations yet. Each generating tool starts one on its first call; "
            "the same session_name continues it, a new name starts fresh."
        )
    rows = [_HEADER]
    for session in sessions:
        tool, _, name = session.name.partition(":")
        rows.append((tool, name, session.model, str(_turns(session))))
    widths = [max(len(row[i]) for row in rows) for i in range(len(_HEADER))]
    lines = [
        f"{len(sessions)} conversation(s) in memory, most recently used first "
        f"(the least recently used is dropped past {MAX_SESSIONS}; all are lost on restart):",
        "",
    ]
    lines += [
        "  " + "  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in rows
    ]
    lines += [
        "",
        "To continue one, call the same tool with that session_name (and model, if you "
        "passed one). A new name starts fresh.",
    ]
    return "\n".join(lines)


def register(mcp: FastMCP, client: ModelSource) -> None:
    """Register list_sessions with the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,  # reads this process's memory, nothing outside it
        )
    )
    def list_sessions() -> ToolResult:
        """List the Gemini conversations this server holds in memory.

        Shows each one's tool, session_name, model and number of turns, most recently used
        first. Use it to find a conversation to continue after a gap or a context compaction.
        Makes no API call and writes nothing.
        """
        return format_session_list(client.sessions())
