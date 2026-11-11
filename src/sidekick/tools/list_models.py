"""
sidekick/tools/list_models.py
------------------------------------
MCP tool: list_models — the authoritative, live, backend-scoped model list.

Responsibilities:
  - Register the list_models MCP tool
  - Fetch the live catalog via GeminiClient.list_models(), filter to chat-capable models
  - Render a compact table (id, label, (default)/(alias) markers) headed by the active backend
  - Degrade gracefully to the curated static shortlist when the live list is unavailable

Design notes:
  - Single Responsibility: discovery/formatting only; no inference, no session, no transcript
  - Dependency Inversion: goes through client.list_models() + models.is_chat_capable — never
    touches ._raw_client or hardcodes the model taxonomy (that lives in models.py)
  - Open/Closed: the chat filter and shortlist come from models.py; this module only renders

Used by:  tools/__init__.py -> register_list_models(), server.py
Imports:  models.py (filter + shortlist), client.py (GeminiClient, ClientError),
          tools/base.py (ToolResult), transcript.py (TranscriptWriter, signature parity)
"""

from typing import Annotated, Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from sidekick import models
from sidekick.client import ClientError, GeminiClient
from sidekick.tools.base import ToolResult
from sidekick.transcript import TranscriptWriter

_TOOL_NAME = "list_models"
# Exported so server.py can say, truthfully, that this one call writes nothing (#74).
LIST_MODELS_TOOL_NAME = _TOOL_NAME

_BACKEND_LABEL = {
    models.DEVELOPER_API: "Developer API (Google AI Studio)",
    models.VERTEX: "Google Cloud Agent Platform (Vertex AI)",
}


def _model_id(meta: object) -> str:
    """Strip any 'models/' or 'publishers/.../models/' prefix to the bare id."""
    return (getattr(meta, "name", "") or "").split("/")[-1]


def _is_alias(model_id: str) -> bool:
    return model_id.endswith("-latest") or "-latest-" in model_id


def _markers(model_id: str, default_model: str, latest: Optional[dict[str, str]] = None) -> str:
    tags = []
    if model_id == default_model:
        tags.append("default")
    tags += [f"latest {family}" for family, mid in (latest or {}).items() if mid == model_id]
    if _is_alias(model_id):
        tags.append("alias")
    return f"  ({', '.join(tags)})" if tags else ""


def format_model_list(
    raw_models: list[Any],
    backend: str,
    default_model: str,
    latest: Optional[dict[str, str]] = None,
) -> str:
    """Render the live catalog: chat-capable only, default first, then alphabetical by id."""
    ids_seen: set[str] = set()
    rows: list[tuple[str, str]] = []  # (id, display_name)
    for meta in raw_models:
        if not models.is_chat_capable(meta):
            continue
        model_id = _model_id(meta)
        if not model_id or model_id in ids_seen:
            continue
        ids_seen.add(model_id)
        display = (getattr(meta, "display_name", "") or "").strip()
        rows.append((model_id, display))

    if not rows:
        return format_static_fallback(
            backend, default_model, "the live list returned no chat-capable models"
        )

    rows.sort(key=lambda r: (r[0] != default_model, r[0]))  # default first, then alphabetical

    backend_label = _BACKEND_LABEL.get(backend, backend)
    width = max(len(model_id) for model_id, _ in rows)
    lines = [f"Gemini chat models on {backend_label} ({len(rows)} available):", ""]
    for model_id, display in rows:
        suffix = f"  — {display}" if display else ""
        lines.append(
            f"  {model_id.ljust(width)}{_markers(model_id, default_model, latest)}{suffix}"
        )
    lines += [
        "",
        f"Pass model='<id>' to any tool. Omit to use the default ({default_model}).",
        "Or pass flash / flash-lite / pro to get the newest release of that family.",
    ]
    return "\n".join(lines)


def format_static_fallback(backend: str, default_model: str, reason: str) -> str:
    """Render the curated static shortlist when the live catalog is unavailable."""
    backend_label = _BACKEND_LABEL.get(backend, backend)
    rows = models.shortlist(backend)
    width = max(len(model_id) for model_id, _ in rows)
    lines = [
        f"[sidekick notice] Live model list unavailable ({reason}). "
        "Showing the curated recommended shortlist instead.",
        "",
        f"Recommended Gemini chat models on {backend_label}:",
        "",
    ]
    for model_id, label in rows:
        lines.append(f"  {model_id.ljust(width)}{_markers(model_id, default_model)}  — {label}")
    lines += [
        "",
        f"Pass model='<id>' to any tool. Omit to use the default ({default_model}).",
    ]
    return "\n".join(lines)


def render_model_list(client: GeminiClient, backend: str) -> str:
    """Fetch + render the live list, degrading to the static shortlist on any ClientError."""
    try:
        raw = client.list_models()
    except ClientError as exc:
        return format_static_fallback(backend, client.default_model, str(exc))
    return format_model_list(raw, backend, client.default_model, client.resolved_latest)


def register(mcp: FastMCP, client: GeminiClient, transcript: TranscriptWriter) -> None:
    """Register list_models with the MCP server.

    `transcript` is accepted for registration-signature parity with the other tools but is
    unused — this is a metadata call, not an inference, so it is not logged to transcripts.
    """
    backend = models.backend_for(client.auth_method)
    cache: dict[str, str] = {}

    @mcp.tool(
        # The one tool that writes nothing at all: no transcript entry, no artifact, no file
        # access. Absent annotations make clients assume the opposite, so state it (#74).
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,  # it queries the live backend
        )
    )
    def list_models(
        refresh: Annotated[
            bool,
            Field(
                description="Re-fetch the live list, bypassing the per-process cache. "
                "Default False reuses the first successful fetch."
            ),
        ] = False,
    ) -> ToolResult:
        """List the Gemini models available on this bridge's active backend.

        Returns a chat-only, backend-scoped catalog (image/tts/audio/embedding/live models are
        excluded), marking the server default and any '-latest' aliases. Use this to discover
        valid values for the `model=` parameter accepted by every sidekick tool. Falls back to
        a curated shortlist if the live catalog cannot be fetched.
        """
        if refresh or "text" not in cache:
            text = render_model_list(client, backend)
            # Only cache authoritative live results; a degraded fallback should retry next call.
            if not text.startswith("[sidekick notice]"):
                cache["text"] = text
            return text
        return cache["text"]
