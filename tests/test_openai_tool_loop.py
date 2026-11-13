"""Tests for run_openai_tool_loop() in sidekick/openai_tool_loop.py — driven by a scripted fake post."""

import copy
import json
from pathlib import Path
from typing import Any, Optional

import pytest

from sidekick.file_tools import READ_TOOL_NAMES, FileTools, build_file_registry
from sidekick.openai_tool_loop import run_openai_tool_loop
from sidekick.sandbox import Sandbox
from sidekick.tool_loop import BUDGET_EXHAUSTED_PROMPT, ToolCallRecord, ToolRegistry


def _answer(text: Optional[str], finish_reason: Optional[str] = "stop") -> dict[str, Any]:
    return {"message": {"role": "assistant", "content": text}, "finish_reason": finish_reason}


def _calls(*calls: tuple[str, str], content: Optional[str] = None) -> dict[str, Any]:
    """A tool turn; each call is (name, raw arguments string)."""
    return {
        "message": {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
                for i, (name, arguments) in enumerate(calls)
            ],
        },
        "finish_reason": "tool_calls",
    }


class FakePost:
    """Returns scripted turns; records (messages, allow_tools) for each request."""

    def __init__(self, *turns: dict[str, Any], repeat_last: bool = False) -> None:
        self._turns = list(turns)
        self._repeat_last = repeat_last
        self.requests: list[tuple[list[dict[str, Any]], bool]] = []

    async def __call__(self, messages: list[dict[str, Any]], allow_tools: bool) -> dict[str, Any]:
        self.requests.append((copy.deepcopy(messages), allow_tools))
        if len(self._turns) == 1 and self._repeat_last:
            return copy.deepcopy(self._turns[0])
        return self._turns.pop(0)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_text("alpha\n")
    (root / "b.txt").write_text("beta\n")
    return root


@pytest.fixture
def registry(repo: Path) -> ToolRegistry:
    return build_file_registry(FileTools(Sandbox(repo), max_write_bytes=1024), write=False)


def _prompt() -> list[dict[str, Any]]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "look"}]


def test_openai_tools_shape(registry: ToolRegistry) -> None:
    tools = registry.openai_tools()
    assert len(tools) == 4
    assert tuple(t["function"]["name"] for t in tools) == READ_TOOL_NAMES
    for tool, decl in zip(tools, registry.declarations):
        assert tool["type"] == "function"
        assert tool["function"]["description"] == decl.description
        assert isinstance(tool["function"]["parameters"], dict)
        assert tool["function"]["parameters"] == decl.parameters_json_schema
        assert tool["function"]["parameters"]["type"] == "object"


async def test_single_call_then_answer(registry: ToolRegistry) -> None:
    post = FakePost(_calls(("read_file", '{"path": "a.txt"}')), _answer("done"))
    records: list[ToolCallRecord] = []
    result = await run_openai_tool_loop(post, _prompt(), registry, records)
    assert result == ("done", "stop")
    assert len(records) == 1
    assert records[0].ok and records[0].name == "read_file"
    assert records[0].args == {"path": "a.txt"}
    second, allow = post.requests[1]
    assert allow is True
    assistant, tool_msg = second[-2], second[-1]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "call_0"
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_0"
    assert "alpha" in json.loads(tool_msg["content"])["content"]


async def test_two_calls_in_one_turn_run_in_order(registry: ToolRegistry) -> None:
    post = FakePost(
        _calls(("read_file", '{"path": "b.txt"}'), ("read_file", '{"path": "a.txt"}')),
        _answer("ok"),
    )
    records: list[ToolCallRecord] = []
    await run_openai_tool_loop(post, _prompt(), registry, records)
    assert [r.args["path"] for r in records] == ["b.txt", "a.txt"]
    second, _ = post.requests[1]
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_0", "call_1"]
    assert "beta" in tool_msgs[0]["content"]
    assert "alpha" in tool_msgs[1]["content"]


async def test_text_with_tool_calls_is_a_tool_turn(registry: ToolRegistry) -> None:
    turn = _calls(("read_file", '{"path": "a.txt"}'), content="Let me look first.")
    post = FakePost(turn, _answer("final"))
    records: list[ToolCallRecord] = []
    result = await run_openai_tool_loop(post, _prompt(), registry, records)
    assert result == ("final", "stop")
    assert len(post.requests) == 2
    assert len(records) == 1
    # The assistant turn is echoed back verbatim, text included.
    echoed = post.requests[1][0][2]
    assert echoed == turn["message"]


async def test_malformed_arguments_become_tool_error(registry: ToolRegistry) -> None:
    post = FakePost(_calls(("read_file", "{not json")), _answer("recovered"))
    records: list[ToolCallRecord] = []
    result = await run_openai_tool_loop(post, _prompt(), registry, records)
    assert result == ("recovered", "stop")
    assert len(records) == 1
    assert not records[0].ok
    assert records[0].args == {}
    assert records[0].summary == "arguments were not valid JSON"
    tool_msg = post.requests[1][0][-1]
    assert json.loads(tool_msg["content"]) == {"error": "arguments were not valid JSON"}


async def test_non_object_arguments_become_tool_error(registry: ToolRegistry) -> None:
    post = FakePost(_calls(("read_file", '["a.txt"]')), _answer("x"))
    records: list[ToolCallRecord] = []
    await run_openai_tool_loop(post, _prompt(), registry, records)
    assert not records[0].ok and records[0].args == {}


async def test_unknown_tool_on_read_only_call(registry: ToolRegistry, repo: Path) -> None:
    before = sorted(p.name for p in repo.iterdir())
    post = FakePost(
        _calls(("write_file", '{"path": "evil.txt", "content": "x"}')), _answer("no write")
    )
    records: list[ToolCallRecord] = []
    await run_openai_tool_loop(post, _prompt(), registry, records)
    assert not records[0].ok
    assert records[0].summary == "unknown tool: write_file"
    assert sorted(p.name for p in repo.iterdir()) == before


async def test_round_cap_forces_final_answer(registry: ToolRegistry) -> None:
    post = FakePost(_calls(("read_file", '{"path": "a.txt"}')), repeat_last=True)
    records: list[ToolCallRecord] = []
    result = await run_openai_tool_loop(post, _prompt(), registry, records, max_rounds=3)
    assert len(post.requests) == 4
    assert [allow for _, allow in post.requests] == [True, True, True, False]
    last_messages = post.requests[-1][0]
    assert last_messages[-1] == {"role": "user", "content": BUDGET_EXHAUSTED_PROMPT}
    assert len(records) == 3
    # The forced turn still came back as a tool turn: it has no text to return.
    assert result == (None, "tool_calls")


async def test_empty_content_returns_none(registry: ToolRegistry) -> None:
    for content in (None, ""):
        post = FakePost(_answer(content, "length"))
        assert await run_openai_tool_loop(post, _prompt(), registry, []) == (None, "length")


async def test_input_messages_not_mutated(registry: ToolRegistry) -> None:
    messages = _prompt()
    snapshot = copy.deepcopy(messages)
    post = FakePost(_calls(("read_file", '{"path": "a.txt"}')), _answer("done"))
    await run_openai_tool_loop(post, messages, registry, [])
    assert messages == snapshot


@pytest.mark.parametrize("raw", ["", "   "])
async def test_empty_arguments_are_a_no_arg_call(registry: ToolRegistry, raw: str) -> None:
    post = FakePost(_calls(("list_dir", raw)), _answer("listed"))
    records: list[ToolCallRecord] = []
    result = await run_openai_tool_loop(post, _prompt(), registry, records)
    assert result == ("listed", "stop")
    assert records[0].ok and records[0].args == {}
    assert "a.txt" in post.requests[1][0][-1]["content"]


async def test_missing_call_id_is_synthesized_consistently(registry: ToolRegistry) -> None:
    turn = _calls(("read_file", '{"path": "a.txt"}'), ("read_file", '{"path": "b.txt"}'))
    for call in turn["message"]["tool_calls"]:
        del call["id"]
    post = FakePost(turn, _answer("ok"))
    await run_openai_tool_loop(post, _prompt(), registry, [])
    second = post.requests[1][0]
    echoed_ids = [c["id"] for c in second[2]["tool_calls"]]
    tool_ids = [m["tool_call_id"] for m in second if m["role"] == "tool"]
    assert echoed_ids == tool_ids == ["call_0_0", "call_0_1"]
