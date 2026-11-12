"""
sidekick/router.py
------------------------
Picks the backend for each call when an OpenRouter key is set: OpenRouter by default,
Gemini directly for Gemini aliases and ids when a Gemini client is available too.

Responsibilities:
  - route(): the pure routing rule from a model id to a backend name
  - Router: the ModelSource the tools see — routes, merges sessions, describes the surface

Design notes:
  - Dependency Inversion: tools depend on ModelSource; they never know a Router is behind it
  - OpenRouter owns the defaults (model, thinking, web); Gemini only answers what is routed to it

Used by:  __main__.py (builds it), tools via the ModelSource protocol
Imports:  backends.py, client.py (GeminiClient), models.py, openrouter.py (OpenRouterClient)
"""

from typing import Literal, Optional

from sidekick import models
from sidekick.backends import Backend, Session, Surface
from sidekick.client import GeminiClient
from sidekick.config import ThinkingLevel
from sidekick.openrouter import OpenRouterClient

__all__ = ["Router", "route"]


def route(model: Optional[str], *, has_gemini: bool) -> Literal["gemini", "openrouter"]:
    """Omitted or OpenRouter ids go to OpenRouter; anything else to Gemini when it is there."""
    if not model or models.is_openrouter_id(model):
        return "openrouter"
    return "gemini" if has_gemini else "openrouter"


class Router:
    """ModelSource over an OpenRouter client and, optionally, a Gemini client."""

    def __init__(
        self,
        openrouter: OpenRouterClient,
        gemini: Optional[GeminiClient] = None,
        gemini_problem: Optional[str] = None,
    ) -> None:
        self._openrouter = openrouter
        self._gemini = gemini
        self._gemini_problem = gemini_problem

    @property
    def openrouter(self) -> OpenRouterClient:
        return self._openrouter

    @property
    def gemini_problem(self) -> Optional[str]:
        """Why Gemini is not available alongside OpenRouter, when it was configured but failed."""
        return self._gemini_problem

    def backend_for(self, model: Optional[str]) -> Backend:
        if self._gemini is not None and route(model, has_gemini=True) == "gemini":
            return self._gemini
        return self._openrouter

    def backends(self) -> list[Backend]:
        return [self._openrouter] + ([self._gemini] if self._gemini is not None else [])

    def sessions(self) -> list[Session]:
        merged = self._openrouter.sessions() + (self._gemini.sessions() if self._gemini else [])
        return sorted(merged, key=lambda s: s.used_at, reverse=True)

    @property
    def default_thinking(self) -> ThinkingLevel:
        return self._openrouter.default_thinking

    @property
    def web_default(self) -> bool:
        return self._openrouter.web_default

    @property
    def web_supported(self) -> bool:
        return False

    @property
    def default_model(self) -> str:
        return self._openrouter.default_model

    @property
    def auth_method(self) -> str:
        return "openrouter"

    @property
    def resolved_latest(self) -> dict[str, str]:
        return self._gemini.resolved_latest if self._gemini is not None else {}

    @property
    def surface(self) -> Surface:
        return Surface("openrouter", False, False, self.web_default, self._gemini is not None)

    def model_hint(self) -> str:
        hint = (
            f"Optional model. Omit for the default ({self.default_model}). Any OpenRouter id "
            "from list_models, e.g. deepseek/deepseek-v4.1-flash-1017, moonshotai/kimi-k3.1-pro "
            "or x-ai/grok-4.7: conversation only, it sees this prompt and the session, not your "
            "files or the web. "
        )
        if self._gemini is not None:
            hint += (
                "flash, flash-lite, pro or a gemini-* id runs on Gemini directly, which can read "
                "the repository when repository access is on."
            )
        else:
            hint += (
                "flash, flash-lite, pro or a gemini-* id runs as google/<id> through OpenRouter."
            )
        return hint + " list_models shows prices."
