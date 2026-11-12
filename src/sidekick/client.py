"""
sidekick/client.py
------------------------
Gemini chat session manager and unified ask() interface.

Responsibilities:
  - Build the google-genai Client from credentials and config
  - Create and cache named sessions (one per tool+session+model triple); the bridge owns each
    session's conversation history rather than delegating it to the SDK's chat object
  - Resolve the model for each call: explicit id, bridge alias (flash / flash-lite / pro),
    Google '-latest' alias, or — when omitted — the newest Flash found in the live catalog
  - Translate named thinking levels to model-appropriate API parameters, learning per model
    which levels it rejects (e.g. gemini-3.8-flash refuses MINIMAL)
  - Expose async ask() as the single interface all tools use

Design notes:
  - Single Responsibility: session lifecycle + inference only; credentials come in ready-made
  - Open/Closed: new session names need no code changes — sessions are created on demand
  - Interface Segregation: tools receive only GeminiClient; they cannot access config or credentials
  - Dependency Inversion: client depends on google.auth.credentials.Credentials abstraction
  - History is committed only when a call succeeds, so a failed call never leaves a session
    holding a half-finished exchange

Raises:
  ClientError — wraps inference and session failures with context for Claude to surface

Used by:  tools/base.py (via ask()), __main__.py (instantiates GeminiClient at startup)
Imports:  config.py (Config, ModelFamily, ThinkingLevel), models.py, errors.py (ClientError),
          tool_loop.py (run_tool_loop), web_tools.py (web_tool_set)
"""

import asyncio
import logging
import random
import re
import time
from collections import OrderedDict
from typing import Any, Optional

_log = logging.getLogger(__name__)

import google.auth.credentials
from google import genai
from google.genai import types
from google.genai.types import GenerateContentConfig, ThinkingConfig
from google.genai.types import ThinkingLevel as SDKThinkingLevel

from sidekick import models
from sidekick.backends import Elicit, Preflight, Session, Surface
from sidekick.config import Config, ModelFamily, ThinkingLevel
from sidekick.errors import ClientError
from sidekick.tool_loop import ToolCallRecord, ToolRegistry, run_tool_loop
from sidekick.web_tools import web_tool_set

__all__ = [
    "ClientError",
    "GeminiClient",
    "Session",
    "DEFAULT_MODEL",
    "FALLBACK_MODEL",
    "GEMINI_UNPAID_NOTICE",
]

# Offline defaults (#69). At startup the bridge resolves the newest Flash / Flash-Lite / Pro from
# the live catalog (refresh_latest); these pinned, known-good ids are used only when that catalog
# cannot be read. gemini-3.5-flash and gemini-3.1-flash-lite are GA on both backends.
DEFAULT_MODEL = "gemini-3.5-flash"
# Fallback for a terminal 503/429 on the requested model: the newest Flash-Lite when resolved,
# else this pin — cheap, highly available, and distinct from the default so it substitutes.
FALLBACK_MODEL = "gemini-3.1-flash-lite"
_PINNED_BY_FAMILY: dict[str, str] = {
    "flash": DEFAULT_MODEL,
    "flash-lite": FALLBACK_MODEL,
    "pro": "gemini-3.1-pro-preview",
}

# Thinking budget token counts for Gemini 2.x models
_THINKING_BUDGET_2X: dict[str, int] = {
    "none": 0,
    "low": 1024,
    "medium": 8192,
    "high": 32768,
}
# gemini-2.x Pro models enforce a minimum thinking budget of 128; budget=0 is rejected
_THINKING_BUDGET_2X_PRO_MIN = 128

# Thinking level enum values for Gemini 3+ models
_THINKING_LEVEL_3X: dict[str, SDKThinkingLevel] = {
    "none": SDKThinkingLevel.MINIMAL,
    "low": SDKThinkingLevel.LOW,
    "medium": SDKThinkingLevel.MEDIUM,
    "high": SDKThinkingLevel.HIGH,
}

# Ascending cost; when a model rejects a level, the bridge steps UP to the next one.
_LEVEL_ORDER: tuple[SDKThinkingLevel, ...] = (
    SDKThinkingLevel.MINIMAL,
    SDKThinkingLevel.LOW,
    SDKThinkingLevel.MEDIUM,
    SDKThinkingLevel.HIGH,
)
_MIN_NONZERO_BUDGET = 128
_MAX_THINKING_ADJUSTMENTS = 2  # per request: e.g. switch to budget, then raise its floor

# Verbatim Gemini API error messages (live, 2026-09-17).
_LEVEL_REJECTED = re.compile(r"Thinking level (\w+) is not supported for this model")
_LEVEL_UNSUPPORTED = "Thinking level is not supported for this model"
_BUDGET_ZERO_REJECTED = re.compile(r"Budget 0 is invalid|only works in thinking mode")

GEMINI_UNPAID_NOTICE = (
    "[sidekick notice] If this Gemini API key's Cloud project has no billing, Google may use "
    "your prompts and responses to improve its products. block_data_collection does not apply "
    "to Gemini.\n\n"
)

_NO_OPENROUTER_KEY = (
    "`{model}` is an OpenRouter model id, but no OpenRouter key is set. Fix: run /plugin "
    "configure sidekick@sidekick, paste your key on the OpenRouter API key row, press Enter, "
    "then run /reload-plugins."
)

MAX_SESSIONS = 50  # LRU cap; oldest session evicted when exceeded
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds; doubles each attempt plus jitter


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).upper()
    return any(token in msg for token in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"))


def _warn_model_backend_mismatch(model: str, is_vertex: bool) -> None:
    """Log a warning when the model name looks mismatched with the active backend.

    Developer API (api_key): unversioned names and '-latest' aliases. Preview models
               (e.g. 'gemini-3.1-pro-preview') may not be available and can 404.
    Vertex AI: versioned IDs or stable names; '-latest' aliases are a Developer-API
               convention and will 404 on Vertex.
    """
    if not is_vertex and "preview" in model:
        _log.warning(
            "model %r is a preview model — availability via Google AI Studio API keys "
            "varies. If you get 404/503, try a GA model like 'gemini-3.5-flash' or "
            "'gemini-3.1-flash-lite'.",
            model,
        )
    elif is_vertex and "-latest" in model:
        _log.warning(
            "model %r uses a '-latest' alias, which is a Developer API convention. "
            "Vertex AI uses versioned IDs (e.g. 'gemini-2.0-flash-001'). "
            "This may 404 on the Vertex endpoint.",
            model,
        )


def _model_family(model: str) -> ModelFamily:
    """Which thinking parameter a concrete model id takes. Raises ClientError if unrecognized.

    Gemini 2.x takes thinking_budget (GEMINI_2); Gemini 3 and every later generation take
    thinking_level (GEMINI_3) — decided by the major version number, so gemini-4-flash works
    without a code change. Dotted (gemini-3.5-flash) and hyphenated (gemini-3-pro-preview)
    forms are both recognized. Callers resolve aliases first (GeminiClient.resolve_model);
    an unknown '-latest' alias that still reaches here is assumed to be current-generation.
    """
    match = re.match(r"gemini-(\d+)(?:[.-]|$)", model)
    if match:
        major = int(match.group(1))
        if major == 2:
            return ModelFamily.GEMINI_2
        if major >= 3:
            return ModelFamily.GEMINI_3
    elif model.endswith("-latest") or "-latest-" in model:
        _log.debug("unrecognized '-latest' alias %r; assuming thinking_level", model)
        return ModelFamily.GEMINI_3
    raise ClientError(
        f"Unrecognized model family: {model!r}. "
        "Expected a 'gemini-<version>-…' model, a '-latest' alias, or flash / flash-lite / pro."
    )


class GeminiClient:
    """Manages persistent Gemini chat sessions and provides a unified ask() interface."""

    name = "gemini"

    def __init__(
        self,
        config: Config,
        credentials: Optional[google.auth.credentials.Credentials] = None,
        api_key: Optional[str] = None,
        *,
        unpaid_tier_notice: bool = False,
    ) -> None:
        self._config = config
        self._notice_pending = unpaid_tier_notice
        if api_key:
            self._raw_client = genai.Client(api_key=api_key)
        else:
            self._raw_client = genai.Client(
                vertexai=True,
                project=config.project,
                location=config.location,
                credentials=credentials,
            )
        self._is_vertex = api_key is None
        # Keyed by "{name}:{model}" — sessions are model-specific
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        # family -> newest concrete id, filled by refresh_latest(); empty = use pinned ids.
        self._latest: dict[str, str] = {}
        # Learned per model from API rejections (see _learn_thinking).
        self._thinking_floor: dict[str, SDKThinkingLevel] = {}
        self._budget_models: set[str] = set()
        self._budget_floor: dict[str, int] = {}

    def get_or_create_session(
        self,
        name: str = "default",
        model: Optional[str] = None,
    ) -> Session:
        """Return existing session or create a new one (LRU-capped at MAX_SESSIONS).

        Sessions are keyed by (name, model) — changing the model creates a new session.
        Creating a session makes no API call.
        """
        effective_model = self.resolve_model(model)
        requested = model or self._config.default_model
        if requested and models.alias_family(requested) is None:
            # Only warn about ids the caller named; a preview chosen by resolution is intended.
            _warn_model_backend_mismatch(effective_model, self._is_vertex)
        cache_key = f"{name}:{effective_model}"
        if cache_key in self._sessions:
            self._sessions.move_to_end(cache_key)
            session = self._sessions[cache_key]
            session.used_at = time.monotonic()
            return session
        session = Session(model=effective_model, name=name)
        self._sessions[cache_key] = session
        if len(self._sessions) > MAX_SESSIONS:
            evicted, _ = self._sessions.popitem(last=False)
            _log.debug("session cache evicted (LRU): %s", evicted)
        return session

    def sessions(self) -> list[Session]:
        """The live sessions, most recently used first. Makes no API call."""
        return list(reversed(self._sessions.values()))

    @property
    def label(self) -> str:
        return self.auth_method

    @property
    def supports_files(self) -> bool:
        return True

    def backend_for(self, model: Optional[str]) -> "GeminiClient":
        """This client, unless `model` is an OpenRouter id this Gemini-only server cannot run."""
        if model and models.is_openrouter_id(model):
            raise ClientError(_NO_OPENROUTER_KEY.format(model=model))
        return self

    def backends(self) -> list["GeminiClient"]:
        return [self]

    def fallback_for(self, model: str) -> Optional[str]:
        """The model to retry on after a terminal 503/429, or None when already on it."""
        return None if model == self.fallback_model else self.fallback_model

    def can_fall_back(self, exc: ClientError) -> bool:
        return _is_retryable(exc)

    async def preflight(self, model: str, elicit: Optional[Elicit]) -> Preflight:
        """Nothing to confirm for Gemini; optionally a one-time unpaid-tier privacy notice."""
        if self._notice_pending:
            self._notice_pending = False
            return Preflight(True, notice=GEMINI_UNPAID_NOTICE)
        return Preflight(True)

    @property
    def surface(self) -> Surface:
        return Surface("gemini", True, self.web_supported, self.web_default, True)

    def model_hint(self) -> str:
        """Backend-aware description for the `model` param, computed once at tool registration.

        api_key mode surfaces the Developer-API shortlist (incl. '-latest' aliases); Vertex
        modes surface the alias-free Vertex shortlist.
        """
        return models.schema_hint(
            models.backend_for(self.auth_method), self.default_model, self.resolved_latest
        )

    @property
    def default_thinking(self) -> ThinkingLevel:
        return self._config.default_thinking

    @property
    def web_supported(self) -> bool:
        """False on Vertex: the SDK raises on include_server_side_tool_invocations there.

        google_search and url_context themselves convert for Vertex, but the flag that lets
        them share a request with our function declarations is Developer-API only, and the
        file tools mean declarations are almost always present. Rather than fail the call,
        callers drop web access and say so (#76).
        """
        return not self._is_vertex

    @property
    def web_default(self) -> bool:
        """Config answer for a call's `web` argument when it is omitted (#76)."""
        return self._config.web_tools.enabled

    @property
    def default_model(self) -> str:
        """Concrete model for calls that omit `model`: the config's `default_model` (which may
        itself be an alias) if set, else the newest Flash, else the pinned DEFAULT_MODEL."""
        return self.resolve_model(None)

    @property
    def fallback_model(self) -> str:
        """Model used after a terminal 503/429: newest Flash-Lite, else FALLBACK_MODEL."""
        return self._latest.get("flash-lite", FALLBACK_MODEL)

    @property
    def resolved_latest(self) -> dict[str, str]:
        """family -> newest concrete id found at startup (empty if the catalog was unreadable)."""
        return dict(self._latest)

    def resolve_model(self, model: Optional[str]) -> str:
        """Concrete model id for a request. Precedence: per-call `model`, then config
        `default_model`, then newest Flash. Aliases (flash / flash-lite / pro and Google's
        '-latest' names) resolve to the newest id of their family — or its pinned id when the
        catalog was unreadable. Concrete ids pass through untouched."""
        requested = model or self._config.default_model
        if not requested:
            return self._latest.get("flash", DEFAULT_MODEL)
        family = models.alias_family(requested)
        if family is not None:
            return self._latest.get(family, _PINNED_BY_FAMILY[family])
        return requested

    def refresh_latest(self) -> dict[str, str]:
        """Resolve the newest model per family from the live catalog (call once at startup).

        On failure keeps the pinned ids and returns {}. A restart picks up new releases; the
        choice never changes mid-process, so a session stays on one model."""
        try:
            catalog = self.list_models()
        except ClientError as exc:
            _log.warning("could not resolve latest models (%s); using pinned defaults", exc)
            return {}
        self._latest = models.resolve_latest(getattr(m, "name", "") or "" for m in catalog)
        if not self._latest:
            _log.warning("no Flash/Pro models found in the catalog; using pinned defaults")
        return dict(self._latest)

    @property
    def auth_method(self) -> str:
        """The configured auth method (e.g. 'api_key', 'adc'). Public accessor so tools
        need not reach into ._config; models.backend_for() maps it to a backend."""
        return self._config.auth.method

    def list_models(self) -> list[Any]:
        """Return the backend's model catalog (raw google-genai Model objects).

        Thin pass-through so tools never touch ._raw_client. Raises ClientError on failure
        so callers can degrade gracefully (e.g. fall back to a static shortlist).
        """
        try:
            return list(self._raw_client.models.list())
        except Exception as exc:
            _log.error("models.list failed: %s", exc)
            raise ClientError(f"Failed to list models: {exc}") from exc

    def build_config(
        self,
        thinking: ThinkingLevel,
        system_instruction: Optional[str] = None,
        model: Optional[str] = None,
        declarations: Optional[list[types.FunctionDeclaration]] = None,
        allow_tools: bool = True,
        web: bool = False,
    ) -> GenerateContentConfig:
        """Build the request config: thinking for the model family, system instruction, and —
        when declarations are given — the tool set. SDK automatic function calling is always
        disabled (the bridge runs its own loop). allow_tools=False keeps the declarations but
        forbids calls.

        `web` attaches Gemini's server-side google_search and url_context (#76). Those run in
        the API, not here. Combining them with function declarations requires
        tool_config.include_server_side_tool_invocations — without it the API returns 400."""
        effective_model = self.resolve_model(model)
        si: dict[str, Any] = {
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True)
        }
        if system_instruction:
            si["system_instruction"] = system_instruction
        web = web and self.web_supported  # the flag below is Developer-API only
        tools: list[types.Tool] = list(web_tool_set()) if web else []
        tool_config_args: dict[str, Any] = {}
        if web:
            # Required whenever built-in tools share a request with our declarations.
            tool_config_args["include_server_side_tool_invocations"] = True
        if declarations:
            tools.append(types.Tool(function_declarations=declarations))
            if not allow_tools:
                tool_config_args["function_calling_config"] = types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.NONE
                )
        if tools:
            si["tools"] = tools
        if tool_config_args:
            si["tool_config"] = types.ToolConfig(**tool_config_args)
        family = (
            ModelFamily.GEMINI_2
            if effective_model in self._budget_models
            else _model_family(effective_model)
        )
        if family == ModelFamily.GEMINI_2:
            budget = max(_THINKING_BUDGET_2X[thinking], self._budget_floor.get(effective_model, 0))
            if "pro" in effective_model and budget < _THINKING_BUDGET_2X_PRO_MIN:
                _log.debug(
                    "thinking=none clamped to %d for Pro model %r",
                    _THINKING_BUDGET_2X_PRO_MIN,
                    effective_model,
                )
                budget = _THINKING_BUDGET_2X_PRO_MIN
            return GenerateContentConfig(
                thinking_config=ThinkingConfig(thinking_budget=budget),
                **si,
            )
        if family == ModelFamily.GEMINI_3:
            level = _THINKING_LEVEL_3X[thinking]
            floor = self._thinking_floor.get(effective_model)
            if floor is not None and _LEVEL_ORDER.index(level) < _LEVEL_ORDER.index(floor):
                level = floor
            return GenerateContentConfig(
                thinking_config=ThinkingConfig(thinking_level=level),
                **si,
            )
        raise ClientError(
            f"Unrecognized model family: {effective_model!r}. "
            "Expected 'gemini-2.*' or 'gemini-3.*'. Check your model name."
        )

    # Kept for callers/tests written against the original name.
    _build_generation_config = build_config

    async def generate(
        self,
        model: str,
        contents: list[types.Content],
        config: GenerateContentConfig,
    ) -> types.GenerateContentResponse:
        """One generate_content request with retry/backoff on 503/429. Raises ClientError."""
        for attempt in range(1, _MAX_RETRIES + 2):
            try:
                return await self._raw_client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except Exception as exc:
                is_last = attempt > _MAX_RETRIES
                if not is_last and _is_retryable(exc):
                    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    _log.warning(
                        "inference attempt %d/%d failed (retryable) — retrying in %.1fs: %s",
                        attempt,
                        _MAX_RETRIES + 1,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                suffix = f" after {attempt} attempt(s)" if attempt > 1 else ""
                # Warning, not error: the caller may still recover (thinking self-heal, model
                # fallback); tools/base.py logs the final failure at ERROR.
                _log.warning("inference failed%s: %s", suffix, exc)
                hint = ""
                if _is_retryable(exc):
                    hint = (
                        " The model appears overloaded or quota-limited. "
                        "Try again shortly, or pass a different model "
                        "(e.g. model='gemini-3.1-flash-lite') to avoid the busy endpoint."
                    )
                raise ClientError(f"Gemini inference failed{suffix}: {exc}{hint}") from exc
        raise AssertionError("unreachable")  # pragma: no cover

    async def ask(
        self,
        session: Session,
        prompt: str,
        thinking: Optional[ThinkingLevel] = None,
        system_instruction: Optional[str] = None,
        registry: Optional[ToolRegistry] = None,
        records: Optional[list[ToolCallRecord]] = None,
        web: bool = False,
    ) -> str:
        """Send prompt in the session's context and return the answer text.

        With a non-empty `registry`, Gemini may call its tools; each executed call is appended
        to `records` as it happens. Only [prompt, final answer] is committed to
        session.history, and only on success. Raises ClientError.

        `web` attaches the server-side web tools (#76); the API runs them, so they produce no
        entries in `records` — their queries and sources arrive in grounding metadata instead.
        """
        effective_thinking: ThinkingLevel = thinking or self._config.default_thinking
        declarations = registry.declarations if registry else None
        _log.debug(
            "ask: model=%s thinking=%s prompt_len=%d tools=%d web=%s",
            session.model,
            effective_thinking,
            len(prompt),
            len(declarations or []),
            web,
        )

        async def generate(
            contents: list[types.Content], allow_tools: bool
        ) -> types.GenerateContentResponse:
            for attempt in range(_MAX_THINKING_ADJUSTMENTS + 1):
                config = self.build_config(
                    effective_thinking,
                    system_instruction,
                    session.model,
                    declarations,
                    allow_tools,
                    web=web,
                )
                try:
                    return await self.generate(session.model, contents, config)
                except ClientError as exc:
                    if attempt == _MAX_THINKING_ADJUSTMENTS or not self._learn_thinking(
                        session.model, exc
                    ):
                        raise
            raise AssertionError("unreachable")  # pragma: no cover

        user_content = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        result = await run_tool_loop(
            generate,
            [*session.history, user_content],
            registry or ToolRegistry(),
            records if records is not None else [],
        )
        session.history.extend([user_content, result.content])
        _log.debug("response_len=%d", len(result.text))
        return result.text

    def _learn_thinking(self, model: str, exc: Exception) -> bool:
        """Adapt to a thinking-config rejection for `model`; True means retry the request.

        Handles the three rejections the API returns: a level the model does not support
        (step up to the next level and remember it), a model that takes no level at all
        (switch it to thinking_budget), and a model that cannot run with budget 0 (raise its
        budget floor). Each adjustment is logged and applies for the rest of the process.

        If the needed adjustment is already in place — another concurrent request learned it
        after this one was built — returns True as well: the retry rebuilds the config with it.
        The caller's attempt cap bounds retries either way."""
        message = str(exc)
        rejected = _LEVEL_REJECTED.search(message)
        if rejected:
            try:
                index = _LEVEL_ORDER.index(SDKThinkingLevel(rejected.group(1).upper()))
            except ValueError:
                return False
            current = self._thinking_floor.get(model)
            if current is not None and _LEVEL_ORDER.index(current) > index:
                return True  # already learned (concurrent request); retry uses the floor
            if index + 1 >= len(_LEVEL_ORDER):
                return False
            self._thinking_floor[model] = _LEVEL_ORDER[index + 1]
            _log.warning(
                "model %s rejects thinking level %s; using %s instead for this model",
                model,
                rejected.group(1),
                _LEVEL_ORDER[index + 1].value,
            )
            return True
        if _LEVEL_UNSUPPORTED in message:
            if model in self._budget_models:
                return True  # already learned (concurrent request)
            self._budget_models.add(model)
            _log.warning("model %s takes no thinking level; switching it to thinking_budget", model)
            return True
        if _BUDGET_ZERO_REJECTED.search(message):
            if self._budget_floor.get(model, 0) >= _MIN_NONZERO_BUDGET:
                return True  # already learned (concurrent request)
            self._budget_floor[model] = _MIN_NONZERO_BUDGET
            _log.warning(
                "model %s cannot run with thinking budget 0; using %d", model, _MIN_NONZERO_BUDGET
            )
            return True
        return False
