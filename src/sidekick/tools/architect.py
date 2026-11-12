"""
sidekick/tools/architect.py
---------------------------------
MCP tool: architect — system design and tradeoff analysis.

Responsibilities:
  - Register the architect MCP tool with the server
  - Accept system description + optional focused question and thinking level
  - Return Gemini's opinionated architecture guidance with explicit tradeoffs

Design notes:
  - Single Responsibility: tool registration + architect persona only
  - Open/Closed: system prompt changes do not affect other tools
  - System prompt: opinionated when a clearly better path exists; names tradeoffs explicitly

Used by:  tools/__init__.py -> register_architect(), server.py (via tools/__init__)
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
    "You are a software architecture advisor working alongside Claude, another AI. "
    "Evaluate system designs, suggest patterns, identify scalability and maintainability concerns. "
    "Be opinionated when a clearly better path exists. "
    "Name tradeoffs explicitly when the choice is genuinely context-dependent."
)

_TOOL_NAME = "architect"
# Capability row (#68): read + write_file.
_WRITE = True

# Artifact saved unless the caller passes write_artifact=false.
_ARTIFACTS: ArtifactMode = "default"

# Advertised to the MCP client; capability_hint() appends the file-tool row (#74).
_DESCRIPTION = (
    "Ask the sidekick model to evaluate a system design or architecture. It will be opinionated "
    "where warranted and name tradeoffs explicitly when choices are context-dependent."
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
    """Register architect with the MCP server."""
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
    async def architect(
        ctx: Context,  # type: ignore[type-arg]
        description: Annotated[
            str, Field(description="The system design, architecture, or approach to evaluate")
        ],
        question: Annotated[
            str,
            Field(description="Optional specific architecture question or concern to focus on."),
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
                    "directory (default true). Set false to skip."
                )
            ),
        ] = True,
    ) -> ToolResult:
        full_prompt = description if not question else f"{description}\n\nQuestion: {question}"
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
            artifact_topic=(question or description) if write_artifact else None,
        )
