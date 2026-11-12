"""
sidekick/backends.py
----------------------------
The seam between the tools and the model backends (Gemini, OpenRouter).

Responsibilities:
  - Own the Session dataclass (one named conversation) shared by every backend
  - Define the Backend protocol (one backend: ask, preflight, fallback rules)
  - Define the ModelSource protocol (what the tools see: routing, sessions, surface)
  - Define Preflight / Surface / Elicit value types

Design notes:
  - Dependency Inversion: tools depend on these protocols, never on a concrete client
  - Pure types only: no SDK imports, so every backend can import this without a cycle

Used by:  client.py, tools/base.py
Imports:  config.py (ThinkingLevel), errors.py (ClientError); tool_loop.py under TYPE_CHECKING
"""

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional, Protocol, runtime_checkable

from sidekick.config import ThinkingLevel
from sidekick.errors import ClientError

if TYPE_CHECKING:
    from sidekick.tool_loop import ToolCallRecord, ToolRegistry

__all__ = [
    "Backend",
    "Elicit",
    "ElicitOutcome",
    "ModelSource",
    "Preflight",
    "Session",
    "Surface",
]


@dataclass
class Session:
    """One named conversation: its model and the committed history (prompts + final answers)."""

    model: str
    history: list[Any] = field(default_factory=list)
    name: str = ""  # the cache name, "<tool>:<session_name>" — for list_sessions
    used_at: float = field(default_factory=lambda: time.monotonic())


ElicitOutcome = Literal["accept", "decline", "cancel", "unavailable"]
Elicit = Callable[[str], Awaitable[ElicitOutcome]]


@dataclass(frozen=True)
class Preflight:
    """Result of a backend's pre-call check: go ahead (maybe with a notice) or stop with a message."""

    proceed: bool
    notice: str = ""
    message: str = ""


@dataclass(frozen=True)
class Surface:
    """What the tool surface may offer, given the backends available."""

    primary: Literal["gemini", "openrouter"]
    files: bool
    web_supported: bool
    web_default: bool
    gemini_available: bool


@runtime_checkable
class Backend(Protocol):
    name: str

    @property
    def label(self) -> str: ...

    @property
    def supports_files(self) -> bool: ...

    @property
    def web_supported(self) -> bool: ...

    @property
    def default_thinking(self) -> ThinkingLevel: ...

    def resolve_model(self, model: Optional[str]) -> str: ...

    def get_or_create_session(
        self, name: str = "default", model: Optional[str] = None
    ) -> Session: ...

    def fallback_for(self, model: str) -> Optional[str]: ...

    def can_fall_back(self, exc: ClientError) -> bool: ...

    async def preflight(self, model: str, elicit: Optional[Elicit]) -> Preflight: ...

    async def ask(
        self,
        session: Session,
        prompt: str,
        thinking: Optional[ThinkingLevel] = None,
        system_instruction: Optional[str] = None,
        registry: Optional["ToolRegistry"] = None,
        records: Optional[list["ToolCallRecord"]] = None,
        web: bool = False,
    ) -> str: ...


@runtime_checkable
class ModelSource(Protocol):
    @property
    def default_thinking(self) -> ThinkingLevel: ...

    @property
    def web_default(self) -> bool: ...

    @property
    def web_supported(self) -> bool: ...

    @property
    def default_model(self) -> str: ...

    @property
    def auth_method(self) -> str: ...

    @property
    def resolved_latest(self) -> dict[str, str]: ...

    @property
    def surface(self) -> Surface: ...

    def backend_for(self, model: Optional[str]) -> Backend: ...

    def backends(self) -> Sequence[Backend]: ...

    def sessions(self) -> list[Session]: ...

    def model_hint(self) -> str: ...
