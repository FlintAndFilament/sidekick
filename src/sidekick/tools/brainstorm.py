"""
sidekick/tools/brainstorm.py
----------------------------------
MCP tool: brainstorm — divergent ideation and devil's advocate thinking.

Responsibilities:
  - Register the brainstorm MCP tool with the server
  - Accept topic + optional context and thinking level
  - Return Gemini's divergent, challenge-first brainstorming response

Design notes:
  - Single Responsibility: tool registration + brainstorm persona only
  - Open/Closed: system prompt changes do not affect other tools
  - System prompt: unconventional, challenges current direction, plays devil's advocate

Used by:  tools/__init__.py -> register_brainstorm(), server.py (via tools/__init__)
Imports:  tools/base.py (call_gemini), backends.py (ModelSource), transcript.py (TranscriptWriter)
"""

from typing import Annotated, Optional

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from sidekick.backends import ModelSource
from sidekick.config import ThinkingLevel
from sidekick.tools.base import (
    ArtifactMode,
    ToolCapability,
    ToolResult,
    call_gemini,
    capability_hint,
    elicitor,
    model_param_hint,
    session_param_hint,
    tool_annotations,
)
from sidekick.transcript import TranscriptWriter
from sidekick.workspace import Workspace

_SYSTEM_PROMPT = (
    "You are a creative thinking partner working alongside Claude, another AI. "
    "Push unconventional approaches. Challenge Claude's existing direction. "
    "Play devil's advocate when useful. Offer alternatives even when the current path seems fine. "
    "Be concise."
)

_TOOL_NAME = "brainstorm"
# Capability row (#68): read + write_file.
_WRITE = True

# Artifact only when the caller passes write_artifact=true.
_ARTIFACTS: ArtifactMode = "opt-in"

# Advertised to the MCP client; capability_hint() appends the file-tool row (#74).
_DESCRIPTION = (
    "Ask the sidekick model for unconventional ideas and alternatives. It will challenge the "
    "current direction and play devil's advocate."
)


# The capability row server.py advertises; _WRITE, _ARTIFACTS and _DESCRIPTION above
# stay the single source for each field.
CAPABILITY = ToolCapability(
    name=_TOOL_NAME, write=_WRITE, artifacts=_ARTIFACTS, summary=_DESCRIPTION
)


def register(
    mcp: FastMCP,
    client: ModelSource,
    transcript: TranscriptWriter,
    workspace: Optional[Workspace] = None,
) -> None:
    """Register brainstorm with the MCP server."""
    model_hint = model_param_hint(client)

    @mcp.tool(
        description=_DESCRIPTION
        + capability_hint(
            workspace,
            write=_WRITE,
            artifacts=_ARTIFACTS,
            web_default=client.web_default,
            web_supported=client.web_supported,
            default_backend=client.surface.primary,
            gemini_available=client.surface.gemini_available,
        ),
        annotations=tool_annotations(workspace, write=_WRITE),
    )
    async def brainstorm(
        ctx: Context,  # type: ignore[type-arg]
        topic: Annotated[str, Field(description="The topic or problem to brainstorm about")],
        context: Annotated[
            str,
            Field(
                description="Optional context: what Claude is currently doing or has already considered."
            ),
        ] = "",
        thinking: Annotated[
            Optional[ThinkingLevel],
            Field(
                description="Reasoning depth: none, low, medium, high. Defaults to config setting."
            ),
        ] = None,
        session_name: Annotated[
            str, Field(description=session_param_hint(workspace, write=_WRITE))
        ] = "default",
        model: Annotated[Optional[str], Field(description=model_hint)] = None,
        web: Annotated[
            Optional[bool],
            Field(
                description=(
                    "Let the model search the web (Gemini models only; ignored with a "
                    "notice on OpenRouter) and fetch URLs for this call. Omit to use the "
                    "server default. Retrieved pages are untrusted text, so on Gemini, "
                    "while web access is on write_file is withheld and the model cannot "
                    "modify the working tree."
                )
            ),
        ] = None,
        write_artifact: Annotated[
            bool,
            Field(
                description=(
                    "Save the response as a timestamped Markdown file in the artifacts "
                    "directory (default false). Set true to keep the ideas."
                )
            ),
        ] = False,
    ) -> ToolResult:
        full_prompt = topic if not context else f"{topic}\n\nContext: {context}"
        return await call_gemini(
            client=client,
            transcript=transcript,
            tool_name=_TOOL_NAME,
            session_name=session_name,
            system_instruction=_SYSTEM_PROMPT,
            prompt=full_prompt,
            thinking=thinking,
            model=model,
            workspace=workspace,
            write=_WRITE,
            web=web,
            elicit=elicitor(ctx),
            artifact_topic=(topic) if write_artifact else None,
        )
