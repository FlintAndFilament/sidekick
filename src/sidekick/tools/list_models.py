"""
sidekick/tools/list_models.py
------------------------------------
MCP tool: list_models — the authoritative, live, backend-scoped model list.

Responsibilities:
  - Register the list_models MCP tool
  - Render the OpenRouter section (featured, query=, check=) beside the Gemini one
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

import asyncio
from typing import Annotated, Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from sidekick import models
from sidekick.backends import ModelSource
from sidekick.client import ClientError, GeminiClient
from sidekick.openrouter import OpenRouterClient
from sidekick.openrouter_catalog import (
    FREE_LIMITS,
    PINNED_FREE_MODEL,
    ORModel,
    PrivacyStatus,
    featured,
    format_rows,
    free_candidates,
    privacy_sentence,
    search,
)
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
    omit_hint: Optional[str] = None,
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
            backend,
            default_model,
            "the live list returned no chat-capable models",
            omit_hint,
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
        _pass_line(default_model, omit_hint),
        "Or pass flash / flash-lite / pro to get the newest release of that family.",
    ]
    return "\n".join(lines)


def _pass_line(default_model: str, omit_hint: Optional[str]) -> str:
    """The model= footer; under a Router the omitted-model default is OpenRouter's, not Gemini's."""
    omit = omit_hint or f"Omit to use the default ({default_model})."
    return f"Pass model='<id>' to any tool. {omit}"


def format_static_fallback(
    backend: str, default_model: str, reason: str, omit_hint: Optional[str] = None
) -> str:
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
    lines += ["", _pass_line(default_model, omit_hint)]
    return "\n".join(lines)


def render_model_list(client: GeminiClient, backend: str, omit_hint: Optional[str] = None) -> str:
    """Fetch + render the live list, degrading to the static shortlist on any ClientError."""
    try:
        raw = client.list_models()
    except ClientError as exc:
        return format_static_fallback(backend, client.default_model, str(exc), omit_hint)
    return format_model_list(raw, backend, client.default_model, client.resolved_latest, omit_hint)


_FEATURED_HEADER = "OpenRouter models (USD per 1M tokens, input / output, from the live catalog):"
_QUERY_CAP = 40
_OR_ONLY_NOTICE = (
    "[sidekick notice] query= and check= apply to OpenRouter models only; "
    "no OpenRouter key is set.\n\n"
)


def _free_default(client: OpenRouterClient) -> str:
    free = free_candidates(client.catalog)
    return free[0] if free else PINNED_FREE_MODEL


def _privacy_map(client: OpenRouterClient, rows: list[ORModel]) -> dict[str, PrivacyStatus]:
    known = ((m.id, client.cached_privacy(m.id)) for m in rows)
    return {model_id: status for model_id, status in known if status is not None}


def _rows(client: OpenRouterClient, rows: list[ORModel]) -> list[str]:
    return format_rows(
        rows,
        default_model=client.default_model,
        free_default=_free_default(client),
        privacy=_privacy_map(client, rows),
    )


async def render_openrouter(
    client: OpenRouterClient, *, query: Optional[str], check: Optional[str]
) -> str:
    """The OpenRouter section: a check of one model, a search, or the featured list."""
    if not client.catalog_loaded:
        return (
            "[sidekick notice] OpenRouter's catalog is unavailable right now, so prices are "
            f"unknown. Default model: {client.default_model}."
        )
    if check:
        found = client.entry(check)
        if found is None:
            return (
                f"[sidekick notice] `{check}` is not in OpenRouter's catalog. "
                "Check the id, or search with query=."
            )
        try:
            status = await client.privacy_status(check)
        except ClientError as exc:
            return f"[sidekick error] {exc}"
        row = _rows(client, [found])[0]
        text = row + "\n" + privacy_sentence(check, status, block=client.block_data_collection)
        return text + " " + FREE_LIMITS if found.free else text
    if query:
        hits = search(client.catalog, query)
        if not hits:
            return f"No OpenRouter text model matches {query!r}."
        lines = _rows(client, hits[:_QUERY_CAP])
        if len(hits) > _QUERY_CAP:
            lines.append(f"…and {len(hits) - _QUERY_CAP} more; narrow the query.")
        return "\n".join([_FEATURED_HEADER, "", *lines])
    shown = featured(client.catalog, client.default_model, free_candidates(client.catalog))
    more = sum(1 for m in client.catalog if m.text_chat) - len(shown)
    lines = [_FEATURED_HEADER, "", *_rows(client, shown), ""]
    lines.append(
        f"{more} more text models: pass query='…' to search ids and names, or check='<id>' "
        "for one model's price and privacy status (sends one tiny routing check)."
    )
    return "\n".join(lines)


_NO_GEMINI = "[sidekick notice] No Gemini backend is configured on this server."


def register(mcp: FastMCP, client: ModelSource, transcript: TranscriptWriter) -> None:
    """Register list_models with the MCP server.

    Lists each backend's models, primary first (OpenRouter, then Gemini). `transcript` is
    accepted for registration-signature parity with the other tools but is unused — this is a
    metadata call, not an inference, so it is not logged to transcripts.
    """
    backends = list(client.backends())
    gemini = next((b for b in backends if isinstance(b, GeminiClient)), None)
    openrouter = next((b for b in backends if isinstance(b, OpenRouterClient)), None)
    backend = models.backend_for(gemini.auth_method) if gemini is not None else ""
    omit_hint = (
        f"Omit model to use the OpenRouter default ({client.default_model})."
        if openrouter is not None
        else None
    )
    cache: dict[str, str] = {}

    def gemini_section(refresh: bool) -> str:
        assert gemini is not None
        if refresh or "text" not in cache:
            text = render_model_list(gemini, backend, omit_hint)
            # Only cache authoritative live results; a degraded fallback should retry next call.
            if not text.startswith("[sidekick notice]"):
                cache["text"] = text
            return text
        return cache["text"]

    @mcp.tool(
        # Reads and writes no files and keeps no transcript, but `check` sends one tiny routing
        # probe to OpenRouter, so repeating a call is not free of effects: not idempotent (#74).
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,  # it queries the live backend
        )
    )
    async def list_models(
        refresh: Annotated[
            bool,
            Field(
                description="Re-fetch the live lists, bypassing the per-process Gemini cache and "
                "reloading the OpenRouter catalog. Default False reuses the first successful "
                "Gemini fetch."
            ),
        ] = False,
        query: Annotated[
            Optional[str],
            Field(
                description="OpenRouter only: search model ids and names (case-insensitive) "
                "and list matches with prices."
            ),
        ] = None,
        check: Annotated[
            Optional[str],
            Field(
                description="OpenRouter only: one model id to show its price and privacy status. "
                "Sends one tiny routing check."
            ),
        ] = None,
    ) -> ToolResult:
        """List the models this server can use, with live prices for OpenRouter.

        With no arguments: the setup picks and free models; query= searches OpenRouter ids and
        names; check=<id> shows one model's price and privacy status. Gemini models are listed
        when Gemini is configured. Use this to find values for the model= parameter.
        """
        sections: list[str] = []
        if openrouter is not None:
            if refresh:
                await asyncio.to_thread(openrouter.refresh_catalog)
            sections.append(await render_openrouter(openrouter, query=query, check=check))
        if gemini is not None:
            sections.append(gemini_section(refresh))
        if not sections:
            return _NO_GEMINI
        text = "\n\n".join(sections)
        if openrouter is None and (query or check):
            text = _OR_ONLY_NOTICE + text
        return text
