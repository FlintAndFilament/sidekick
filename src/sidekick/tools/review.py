"""
sidekick/tools/review.py
------------------------------
MCP tool: review — critical code and design review.

Responsibilities:
  - Register the review MCP tool with the server
  - Accept content (code/design/plan) + optional focused question and thinking level
  - Return Gemini's critical, severity-prioritized review

Design notes:
  - Single Responsibility: tool registration + review persona only
  - Open/Closed: system prompt changes do not affect other tools
  - System prompt: pessimistic, rigorous — finds problems and prioritizes by severity

Used by:  tools/__init__.py -> register_review(), server.py (via tools/__init__)
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
    "You are a critical technical reviewer working alongside Claude, another AI. "
    "Find problems, risks, and weaknesses in code, designs, and plans. "
    "Be direct. Don't soften feedback. Prioritize by severity. "
    "If something is sound, say so briefly and move on."
)

_TOOL_NAME = "review"
# Capability row (#68): read + write_file.
_WRITE = True

# Artifact saved unless the caller passes write_artifact=false.
_ARTIFACTS: ArtifactMode = "default"

# Advertised to the MCP client; capability_hint() appends the file-tool row (#74).
_DESCRIPTION = (
    "Ask Gemini to critically review code, a design, or a plan. Gemini will find "
    "problems and prioritize by severity."
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
    """Register review with the MCP server."""
    model_hint = model_param_hint(client)

    @mcp.tool(
        description=_DESCRIPTION
        + capability_hint(
            workspace,
            write=_WRITE,
            artifacts=_ARTIFACTS,
            web_default=client.web_default,
            web_supported=client.web_supported,
        ),
        annotations=tool_annotations(workspace, write=_WRITE),
    )
    async def review(
        ctx: Context,  # type: ignore[type-arg]
        content: Annotated[str, Field(description="The code, design, or plan to review")],
        question: Annotated[
            str,
            Field(description="Optional specific question to focus the review on."),
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
                    "Let Gemini search the web and fetch URLs for this call. Omit to use the "
                    "server default. Retrieved pages are untrusted text, so while web "
                    "access is on write_file is withheld and Gemini cannot modify the "
                    "working tree."
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
        full_prompt = content if not question else f"{content}\n\nFocus: {question}"
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
            artifact_topic=(question or content) if write_artifact else None,
        )
