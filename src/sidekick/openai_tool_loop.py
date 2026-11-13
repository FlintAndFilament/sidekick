"""
sidekick/openai_tool_loop.py
---------------------------
The function-calling loop for OpenAI-format chat completions (OpenRouter), over the shared
ToolRegistry.

Responsibilities:
  - run_openai_tool_loop(): post -> execute requested tool_calls -> send `tool` messages ->
    repeat, until the model answers without calls or the round cap forces a final answer

Design notes:
  - Backend-neutral and HTTP-free: the caller supplies `post`, which sends the messages and
    returns one choice's message plus its finish_reason. allow_tools=False forbids more calls.
  - Tool semantics are not duplicated: dispatch, records, summaries, the round cap and the
    budget prompt all come from tool_loop.py, so a call behaves the same on either backend
  - A turn with non-empty tool_calls is a tool turn even if it also carries text; the
    assistant message is echoed back verbatim so the provider sees its own turn
  - Arguments arrive as a JSON string; an empty one means no arguments ({}), and one that
    does not parse to an object becomes an {"error": ...} result returned to the model
  - A call without an id gets a stable synthesized one ("call_<round>_<index>"), used in
    both the echoed assistant turn and the matching `tool` message
  - Records are appended as each call executes, so callers keep them if a later round fails
  - Logs carry counts only, never arguments or results

Raises:
  Nothing of its own — an empty answer is returned as None and the caller decides; errors
  raised by `post` propagate

Used by:  openrouter.py (Task 4)
Imports:  tool_loop.py (ToolRegistry, ToolCallRecord, summarize, MAX_ROUNDS,
          BUDGET_EXHAUSTED_PROMPT)
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from sidekick.tool_loop import (
    BUDGET_EXHAUSTED_PROMPT,
    MAX_ROUNDS,
    ToolCallRecord,
    ToolRegistry,
    summarize,
)

_log = logging.getLogger(__name__)

# (messages, allow_tools) -> {"message": {...}, "finish_reason": str | None}
PostFn = Callable[[list[dict[str, Any]], bool], Awaitable[dict[str, Any]]]

_BAD_ARGUMENTS = "arguments were not valid JSON"


def _parse_arguments(raw: Any) -> Optional[dict[str, Any]]:
    """The call's JSON-string arguments as a dict, or None when they are not a JSON object.

    An empty or whitespace-only string is a no-argument call ({})."""
    if isinstance(raw, str) and not raw.strip():
        return {}
    try:
        args = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return args if isinstance(args, dict) else None


def _final(turn: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    content = (turn.get("message") or {}).get("content")
    text = content if isinstance(content, str) and content else None
    return text, turn.get("finish_reason")


async def run_openai_tool_loop(
    post: PostFn,
    messages: list[dict[str, Any]],
    registry: ToolRegistry,
    records: list[ToolCallRecord],
    *,
    max_rounds: int = MAX_ROUNDS,
) -> tuple[Optional[str], Optional[str]]:
    """Drive the model until it answers without tool calls; return (final_text, finish_reason).

    final_text is None when the answer is empty. Appends every executed call to `records` as
    it happens. `messages` is not mutated."""
    history = list(messages)
    for round_no in range(max_rounds):
        turn = await post(history, True)
        message = turn.get("message") or {}
        raw_calls = message.get("tool_calls") or []
        if not raw_calls:
            return _final(turn)
        calls = [
            {**call, "id": call.get("id") or f"call_{round_no}_{index}"}
            for index, call in enumerate(raw_calls)
        ]
        history.append({**message, "tool_calls": calls})
        for call in calls:
            function = call.get("function") or {}
            name = function.get("name") or ""
            args = _parse_arguments(function.get("arguments"))
            if args is None:
                _log.info("tool %s: arguments were not a JSON object", name)
                result: dict[str, Any] = {"error": _BAD_ARGUMENTS}
                args = {}
            else:
                result = await registry.dispatch(name, args)
            ok, summary = summarize(result)
            records.append(ToolCallRecord(name=name, args=args, ok=ok, summary=summary))
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
    _log.warning("tool loop hit the %d-round cap; forcing a final answer", max_rounds)
    history.append({"role": "user", "content": BUDGET_EXHAUSTED_PROMPT})
    return _final(await post(history, False))
