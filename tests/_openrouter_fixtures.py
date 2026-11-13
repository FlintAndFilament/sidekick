"""OpenRouter test data shaped like the live catalog (fields verified 2026-11-11)."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from sidekick.config import Config
from sidekick.openrouter import OpenRouterClient
from tests._fakes import FAKE_OPENROUTER_KEY


def entry(
    id: str,
    prompt: str = "0.000002",
    completion: str = "0.000006",
    *,
    ctx: int = 500_000,
    max_out: int | None = 450_000,
    inputs: tuple[str, ...] = ("text",),
    outputs: tuple[str, ...] = ("text",),
    efforts: tuple[str, ...] = ("high", "medium", "low"),
    mandatory: bool = False,
    reasoning: bool = True,
    tools: bool = False,
) -> dict[str, Any]:
    return {
        "id": id,
        "name": f"Name of {id}",
        "pricing": {"prompt": prompt, "completion": completion},
        "context_length": ctx,
        "architecture": {"input_modalities": list(inputs), "output_modalities": list(outputs)},
        "top_provider": {"max_completion_tokens": max_out},
        "supported_parameters": ["max_tokens"]
        + (["reasoning"] if reasoning else [])
        + (["tools"] if tools else []),
        "reasoning": {"mandatory": mandatory, "supported_efforts": list(efforts)}
        if reasoning
        else None,
    }


GROK = entry("x-ai/grok-4.7", mandatory=True, efforts=("xhigh", "high", "medium", "low"))
DEEPSEEK = entry(
    "deepseek/deepseek-v4.1-flash-1017",
    "0.000000098",
    "0.000000294",
    ctx=1_048_576,
    efforts=("max", "high", "low", "none"),
)
KIMI = entry(
    "moonshotai/kimi-k3.1-pro",
    "0.000002",
    "0.00001",
    ctx=1_048_576,
    efforts=("max", "high", "low", "none"),
)
QWEN = entry(
    "qwen/qwen3.8-max-0902",
    ctx=1_000_000,
    max_out=131_072,
    mandatory=True,
    efforts=("xhigh", "high", "medium", "low", "minimal"),
)
GEMINI_FLASH = entry(
    "google/gemini-3.8-flash",
    "0.000000825",
    "0.000004125",
    ctx=1_048_576,
    max_out=65_536,
    mandatory=True,
)
GEMINI_LITE = entry("google/gemini-3.5-flash-lite", "0.0000001", "0.0000004")
NEMOTRON = entry(
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "0",
    "0",
    ctx=1_000_000,
    max_out=65_536,
    efforts=("high", "medium", "none"),
)
GEMMA = entry("google/gemma-4-31b-it:free", "0", "0", ctx=262_144, max_out=8192, reasoning=False)
INKLING = entry("thinkingmachines/inkling-2-small:free", "0", "0", ctx=128_000, reasoning=False)
AUTO = entry("openrouter/auto", "-1", "-1", outputs=("text", "image"), reasoning=False)
OR_FREE = entry("openrouter/free", "0", "0", ctx=200_000, reasoning=False)
BATCH = entry("google/gemini-3.8-flash:batch", "0.0000004", "0.000002")
IMAGE = entry("google/gemini-3.1-flash-image", outputs=("image", "text"))
CATALOG = [
    GROK,
    DEEPSEEK,
    KIMI,
    QWEN,
    GEMINI_FLASH,
    GEMINI_LITE,
    NEMOTRON,
    GEMMA,
    INKLING,
    AUTO,
    OR_FREE,
    BATCH,
    IMAGE,
]

DATA_POLICY_404 = {
    "error": {
        "code": 404,
        "message": "No endpoints found matching your data policy (Free model training)",
        "metadata": {"failed_routing_step": "Filter by Data Policy"},
    }
}
GATED_403 = {
    "error": {
        "code": 403,
        "message": "This model is only available on agentic harnesses",
        "metadata": {"failed_routing_step": "Gate Free Endpoints by Agentic Harness"},
    }
}


def reply(text: str | None = "fine", finish: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}]
    }


def tool_reply(*calls: tuple[str, str]) -> dict[str, Any]:
    """A chat reply that asks for tool calls; each call is (name, raw JSON arguments)."""
    tool_calls = [
        {"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": a}}
        for i, (n, a) in enumerate(calls)
    ]
    message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
    return {"choices": [{"message": message, "finish_reason": "tool_calls"}]}


@dataclass
class FakeOpenRouter:
    """A MockTransport handler: serves the catalog and replays scripted chat replies."""

    catalog: list[dict[str, Any]] = field(default_factory=lambda: list(CATALOG))
    catalog_status: int = 200
    replies: list[tuple[int, Any]] = field(default_factory=lambda: [(200, reply())])
    requests: list[httpx.Request] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    _served: int = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path == "/api/v1/models":
            return httpx.Response(self.catalog_status, json={"data": self.catalog})
        assert request.url.path == "/api/v1/chat/completions"
        status, body = self.replies[min(self._served, len(self.replies) - 1)]
        self._served += 1
        if isinstance(body, Exception):
            raise body
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers={"content-type": "text/html"})
        return httpx.Response(status, json=body)

    @property
    def posts(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]

    @property
    def gets(self) -> int:
        return sum(1 for r in self.requests if r.method == "GET")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def make_client(
    fake: FakeOpenRouter,
    tmp_path: Path,
    *,
    clock: Callable[[], float] | None = None,
    file_tools: dict[str, Any] | None = None,
    **openrouter: Any,
) -> OpenRouterClient:
    cfg = Config.model_validate(
        {"openrouter": openrouter, "transcript_dir": str(tmp_path), "file_tools": file_tools or {}},
        context={"openrouter_key": True},
    )
    t = fake.transport()

    async def sleep(delay: float) -> None:
        fake.sleeps.append(delay)

    client = OpenRouterClient(
        cfg,
        FAKE_OPENROUTER_KEY,
        transport=t,
        sync_transport=t,
        config_path=tmp_path / "config.json",
        sleep=sleep,
        clock=clock or time.monotonic,
    )
    client.refresh_catalog()
    return client
