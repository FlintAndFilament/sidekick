"""
sidekick/tools/debug.py
-----------------------------
MCP tool: debug — hypothesis generation for bugs and failures.

Responsibilities:
  - Register the debug MCP tool with the server
  - Accept error description + optional context and thinking level
  - Return Gemini's evidence-driven root cause hypotheses and diagnostic steps

Design notes:
  - Single Responsibility: tool registration + debug persona only
  - Open/Closed: system prompt changes do not affect other tools
  - System prompt: evidence-based, not speculative — generates hypotheses from what's shown

Used by:  tools/__init__.py -> register_debug(), server.py (via tools/__init__)
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
    "You are a systematic debugging assistant working alongside Claude, another AI. "
    "Generate root cause hypotheses from the evidence provided. Reason through failure modes. "
    "Suggest specific diagnostic steps. Don't guess without basis — reason from what's shown."
)

_TOOL_NAME = "debug"
# Capability row (#68): read-only: findings go back to Claude, not to disk.
_WRITE = False

# This tool saves no artifact; only the transcript entry reaches disk.
_ARTIFACTS: ArtifactMode = "never"

# Advertised to the MCP client; capability_hint() appends the file-tool row (#74).
_DESCRIPTION = (
    "Ask the sidekick model for root cause hypotheses and diagnostic steps. Provide the error "
    "and any relevant context (code, recent changes, environment)."
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
    """Register debug with the MCP server."""
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
            files=client.surface.files,
        ),
        annotations=tool_annotations(workspace, write=_WRITE),
    )
    async def debug(
        ctx: Context,  # type: ignore[type-arg]
        error: Annotated[
            str, Field(description="The error message, stack trace, or failure description")
        ],
        context: Annotated[
            str,
            Field(
                description="Optional context: relevant code, recent changes, environment details."
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
                    "Let the model search the web (Gemini models only; ignored with a notice on OpenRouter) and fetch URLs for this call. Omit to use the "
                    "server default. Retrieved pages are untrusted text — treat what "
                    "the model concludes from them as a claim to check."
                )
            ),
        ] = None,
    ) -> ToolResult:
        full_prompt = error if not context else f"{error}\n\nContext:\n{context}"
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
        )
