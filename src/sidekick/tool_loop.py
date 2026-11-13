"""
sidekick/tool_loop.py
---------------------------
The bridge's own function-calling loop and the registry of tools Gemini may call.

Responsibilities:
  - ToolRegistry: map tool names to declarations + async handlers; dispatch without raising
  - ToolCallRecord: one executed call, rendered as a transcript line
  - run_tool_loop(): request -> execute requested calls -> send results -> repeat, until Gemini
    answers in text or the round cap forces a final answer

Design notes:
  - Open/Closed: a tool source (file tools today, MCP servers in #70) only needs to add
    (declaration, handler) pairs — the loop never changes for a new source
  - dispatch() never raises: failures become {"error": ...} results returned to Gemini so the
    model can correct itself instead of the whole call failing
  - The loop is ours rather than the SDK's automatic function calling: every call is recorded
    as it happens, MALFORMED_FUNCTION_CALL turns are retried, and each model turn is kept
    verbatim (including thought signatures, which Gemini 3.x requires echoed back)
  - Calls within one turn run sequentially, in order, and are answered in a single message

Raises:
  ClientError — empty answer, or repeated MALFORMED_FUNCTION_CALL

Used by:  file_tools.py (registry source), client.py (loop), tools/base.py (records),
          openai_tool_loop.py (registry, records, constants)
Imports:  errors.py (ClientError), google-genai types
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from google.genai import types

from sidekick.errors import ClientError

_log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
# (contents, allow_tools) -> response. allow_tools=False forbids further calls.
GenerateFn = Callable[[list[types.Content], bool], Awaitable[types.GenerateContentResponse]]

MAX_ROUNDS = 20
MAX_MALFORMED_RETRIES = 2
BUDGET_EXHAUSTED_PROMPT = "Tool budget exhausted — answer now with what you have."

_ARG_PREVIEW_CHARS = 60
Source = tuple[str, str]  # (title, uri)


@dataclass(frozen=True)
class ToolCallRecord:
    """One tool call Gemini made and how it went — rendered into the transcript."""

    name: str
    args: dict[str, Any]
    ok: bool
    summary: str
    # Web pages this call drew on, as (title, uri); only server-side web calls carry any (#80).
    sources: tuple[Source, ...] = ()

    def render(self) -> str:
        args = ", ".join(f"{k}={_preview(v)}" for k, v in self.args.items())
        mark = "→" if self.ok else "✗"
        return f"{mark} {self.name}({args}) → {self.summary}"


def _preview(value: Any) -> str:
    text = repr(value)
    if len(text) > _ARG_PREVIEW_CHARS:
        return text[: _ARG_PREVIEW_CHARS - 1] + "…"
    return text


def _size(n: int) -> str:
    return f"{n / 1024:.1f} KiB" if n >= 1024 else f"{n} B"


def summarize(result: dict[str, Any]) -> tuple[bool, str]:
    """(ok, short summary) for a handler result: the error text, or what the call amounted to.

    For a write, that is the size of the file written. For everything else it is the size of
    the result returned into Gemini's context, which is what a read actually cost. Reporting
    the response size for writes too made a 5-byte file log as "50 B" — the size of the
    {"path", "bytes_written"} acknowledgement, which a reader takes for the file.
    """
    if "error" in result:
        return False, str(result["error"])
    if "bytes_written" in result:
        return True, f"wrote {_size(int(result['bytes_written']))}"
    return True, _size(len(json.dumps(result, ensure_ascii=False).encode()))


class ToolRegistry:
    """Named tools Gemini may call during one request."""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[types.FunctionDeclaration, Handler]] = {}

    def add(self, declaration: types.FunctionDeclaration, handler: Handler) -> None:
        name = declaration.name
        if not name:
            raise ValueError("tool declaration has no name")
        if name in self._tools:
            raise ValueError(f"duplicate tool name: {name}")
        self._tools[name] = (declaration, handler)

    @property
    def declarations(self) -> list[types.FunctionDeclaration]:
        return [decl for decl, _ in self._tools.values()]

    def openai_tools(self) -> list[dict[str, Any]]:
        """The same tools in OpenAI chat-completions `tools` format, for openai_tool_loop.py."""
        return [
            {
                "type": "function",
                "function": {
                    "name": decl.name,
                    "description": decl.description,
                    "parameters": decl.parameters_json_schema,
                },
            }
            for decl in self.declarations
        ]

    def __len__(self) -> int:
        return len(self._tools)

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run a tool by name. Never raises — unknown tools and failures return {"error": ...}."""
        entry = self._tools.get(name)
        if entry is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return await entry[1](args)
        except Exception as exc:
            _log.info("tool %s failed: %s: %s", name, type(exc).__name__, exc)
            return {"error": f"{type(exc).__name__}: {exc}"}


@dataclass(frozen=True)
class LoopResult:
    """The final answer text and the model turn that produced it.

    `content` is committed to session history, and carries only answer parts — server-side
    tool traffic is stripped by _answer_only() so untrusted web content does not persist.
    """

    text: str
    content: types.Content


def record_grounding(candidate: types.Candidate, records: list[ToolCallRecord]) -> None:
    """Log server-side web activity into `records`, beside the locally executed calls (#76).

    Web tools run inside the Gemini API, so they never reach ToolRegistry.dispatch and would
    otherwise leave no trace. The queries, the URLs fetched and the sources Gemini grounded on
    are the audit trail for a web-enabled call — and, since retrieved pages are untrusted, the
    thing a reader most needs to see.

    The two built-ins report separately: searches land in `grounding_metadata`, URL fetches in
    `url_context_metadata`. A pure fetch produces no grounding chunks, so it must be read from
    its own field or it goes unrecorded.

    Sources are named by `title` (e.g. "python.org") in the summary, where the opaque
    vertexaisearch redirect `uri` would tell a reader nothing. The (title, uri) pairs ride on
    the records as `sources`, once per turn rather than once per query, for sources.py.
    """
    for url, status in _fetched_urls(candidate):
        ok = status is None or "SUCCESS" in str(status).upper()
        records.append(
            ToolCallRecord(
                name="url_context",
                args={"url": url},
                ok=ok,
                summary="retrieved" if ok else f"not retrieved ({status})",
                sources=((url, url),) if ok else (),
            )
        )

    meta = getattr(candidate, "grounding_metadata", None)
    if not meta:
        return
    sources: list[Source] = []
    for chunk in meta.grounding_chunks or []:
        web = getattr(chunk, "web", None)
        if not web:
            continue
        uri = getattr(web, "uri", None) or ""
        # No title and no domain leaves the name empty on purpose: the redirect uri is not a
        # name, and sources.py names the source after its resolved URL instead (#90).
        name = getattr(web, "title", None) or getattr(web, "domain", None) or ""
        if (name or uri) and all(s != (name, uri) for s in sources):
            sources.append((name, uri))
    # Count the sources, name only the ones that have a name: since #90 an untitled source
    # carries an empty name, and counting names would report it as no source at all.
    titles = list(dict.fromkeys(name for name, _ in sources if name))
    summary = f"{len(sources)} source(s)" + (f": {', '.join(titles[:5])}" if titles else "")
    queries = list(meta.web_search_queries or [])
    if not queries and sources:
        queries = ["(query not reported)"]
    for i, query in enumerate(queries):
        records.append(
            ToolCallRecord(
                name="google_search",
                args={"query": query},
                ok=True,
                summary=summary,
                sources=tuple(sources) if i == 0 else (),
            )
        )


def _fetched_urls(candidate: types.Candidate) -> list[tuple[str, Optional[str]]]:
    """(url, retrieval status) for every url_context fetch reported on this turn."""
    meta = getattr(candidate, "url_context_metadata", None)
    out: list[tuple[str, Optional[str]]] = []
    for entry in (getattr(meta, "url_metadata", None) or []) if meta else []:
        url = getattr(entry, "retrieved_url", None)
        if url:
            status = getattr(entry, "url_retrieval_status", None)
            out.append((url, str(status) if status is not None else None))
    return out


async def _generate_turn(
    generate: GenerateFn, history: list[types.Content], allow_tools: bool
) -> types.Candidate:
    """One model turn, retrying MALFORMED_FUNCTION_CALL up to MAX_MALFORMED_RETRIES times."""
    for attempt in range(1, MAX_MALFORMED_RETRIES + 2):
        response = await generate(history, allow_tools)
        if not response.candidates:
            _log.warning("Gemini returned no candidates (finish_reason=UNKNOWN)")
            raise ClientError("Gemini returned no text (finish_reason=UNKNOWN).")
        candidate = response.candidates[0]
        if candidate.finish_reason != types.FinishReason.MALFORMED_FUNCTION_CALL:
            return candidate
        _log.warning("MALFORMED_FUNCTION_CALL (attempt %d/%d)", attempt, MAX_MALFORMED_RETRIES + 1)
    raise ClientError(
        f"Gemini returned MALFORMED_FUNCTION_CALL {MAX_MALFORMED_RETRIES + 1} times in a row."
    )


def _answer_only(content: types.Content) -> types.Content:
    """The model turn with server-side tool traffic stripped, for committing to history.

    #68 D7 commits only the prompt and the final answer, never intermediate tool turns —
    but the API returns server-side tool_call/tool_response parts *inside the same content
    object* as the answer, so they ride along unless removed. Those parts hold retrieved web
    page text. Leaving them in history would carry untrusted content into the next call in
    the session, which may be write-capable — defeating the web-XOR-write rule (#76 D4)
    across two calls even though each call honours it. Text parts keep their
    thought_signature.
    """
    kept = [
        p
        for p in content.parts or []
        if p.tool_call is None and p.tool_response is None and p.function_call is None
    ]
    return types.Content(role=content.role, parts=kept)


def _answer(candidate: types.Candidate) -> LoopResult:
    content = candidate.content or types.Content(role="model", parts=[])
    text = "".join(p.text for p in content.parts or [] if p.text and not p.thought)
    if not text:
        reason = candidate.finish_reason.name if candidate.finish_reason else "UNKNOWN"
        _log.warning("Gemini returned no text (finish_reason=%s)", reason)
        raise ClientError(f"Gemini returned no text (finish_reason={reason}).")
    return LoopResult(text=text, content=_answer_only(content))


async def run_tool_loop(
    generate: GenerateFn,
    contents: list[types.Content],
    registry: ToolRegistry,
    records: list[ToolCallRecord],
    *,
    max_rounds: int = MAX_ROUNDS,
) -> LoopResult:
    """Drive Gemini until it answers in text. Appends every executed call to `records` as it
    happens (so callers can log them even if the loop later fails). `contents` is not mutated."""
    history = list(contents)
    for _ in range(max_rounds):
        candidate = await _generate_turn(generate, history, allow_tools=True)
        record_grounding(candidate, records)
        content = candidate.content
        calls = (
            [p.function_call for p in (content.parts or []) if p.function_call] if content else []
        )
        if not calls:
            return _answer(candidate)
        assert content is not None
        history.append(content)
        response_parts: list[types.Part] = []
        for call in calls:
            name = call.name or ""
            args = dict(call.args or {})
            result = await registry.dispatch(name, args)
            ok, summary = summarize(result)
            records.append(ToolCallRecord(name=name, args=args, ok=ok, summary=summary))
            response_parts.append(types.Part.from_function_response(name=name, response=result))
        history.append(types.Content(role="user", parts=response_parts))
    _log.warning("tool loop hit the %d-round cap; forcing a final answer", max_rounds)
    history.append(
        types.Content(role="user", parts=[types.Part.from_text(text=BUDGET_EXHAUSTED_PROMPT)])
    )
    final = await _generate_turn(generate, history, allow_tools=False)
    record_grounding(final, records)
    return _answer(final)
