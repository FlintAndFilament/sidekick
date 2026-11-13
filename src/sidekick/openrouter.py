"""
sidekick/openrouter.py
----------------------------
OpenRouter chat backend: catalog, sessions, chat completions, retries, clear errors.

Responsibilities:
  - Load the public model catalog (no key sent) and resolve model ids and Gemini aliases
  - Keep named sessions (LRU-capped); history is committed only when a call succeeds
  - Send chat completions over httpx and turn every failure into a ClientError

Design notes:
  - File tools on tool-capable models: with a registry and tools_supported(model), ask() sends
    `tools` plus provider.require_parameters and runs openai_tool_loop.py; otherwise it is a
    single conversation-only request. No web for any OpenRouter model (`web` is ignored)
  - With file tools on, the free default and free fallback prefer tool-capable models
  - Retries cover 429/502/503 and transport errors; free models retry once, paid ones more
  - Logs carry type names and statuses only, never headers, bodies or the key
  - _post and OpenRouterError are the shared seam for the privacy probe and free rotation

Used by:  __main__.py / tools (via the Backend protocol)
Imports:  config.py, backends.py, client.py (retry and session constants), openrouter_catalog.py,
          openai_tool_loop.py
"""

import asyncio
import logging
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

import httpx

from sidekick.backends import Elicit, Preflight, Session
from sidekick.client import _MAX_RETRIES, _PINNED_BY_FAMILY, _RETRY_BASE_DELAY, MAX_SESSIONS
from sidekick.config import (
    CONFIG_PATH,
    Config,
    ConfigError,
    ThinkingLevel,
    remember_accepted_model,
    remember_repo_access,
)
from sidekick.errors import ClientError
from sidekick.openai_tool_loop import run_openai_tool_loop
from sidekick.openrouter_catalog import (
    OPENROUTER_BASE_URL,
    PINNED_FREE_MODEL,
    ORModel,
    PrivacyStatus,
    acceptance_question,
    account_policy_message,
    blocked_message,
    declined_message,
    dismissed_message,
    first_use_notice,
    free_candidates,
    is_free,
    map_effort,
    parse_catalog,
    repo_access_question,
    resolve_gemini_alias,
    unattended_paid_refused_message,
    unattended_refused_message,
)

if TYPE_CHECKING:
    from sidekick.tool_loop import ToolCallRecord, ToolRegistry

_log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 16_000
CATALOG_TIMEOUT_SECONDS = 10.0
CHAT_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
ATTRIBUTION_HEADERS = {
    "HTTP-Referer": "https://github.com/FlintAndFilament/sidekick",
    "X-OpenRouter-Title": "sidekick",
}
FREE_MAX_RETRIES = 1
FREE_COOLDOWN_SECONDS = 600.0
PROBE_MAX_TOKENS = 16
_DENY = {"data_collection": "deny"}
_RETRY_STATUSES = frozenset({429, 502, 503})
_REQUIRE_PARAMETERS = "require_parameters"

_GATED_STEP = "Gate Free Endpoints by Agentic Harness"
_DATA_POLICY_STEP = "Filter by Data Policy"
_KEY_REJECTED = (
    "OpenRouter rejected the API key (HTTP 401). Fix: run /plugin configure sidekick@sidekick, "
    "paste your key on the OpenRouter API key row, press Enter, then run /reload-plugins."
)
_NO_CREDITS = (
    "OpenRouter says the account has too few credits for this request (HTTP 402). "
    "Add credits at https://openrouter.ai/settings/credits, or use a free model (see list_models)."
)


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


# Raised before the request reaches OpenRouter, so a retry cannot double-bill.
_SAFE_RETRY_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class OpenRouterError(ClientError):
    """An OpenRouter failure, with the HTTP status and routing step when the API gave them."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        routing_step: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.routing_step = routing_step

    @property
    def gated(self) -> bool:
        return self.status == 403 and self.routing_step == _GATED_STEP

    @property
    def data_policy(self) -> bool:
        return self.status == 404 and self.routing_step == _DATA_POLICY_STEP


def _error_parts(payload: Any, status: int) -> tuple[str, Optional[str], Optional[int]]:
    """(message, failed routing step, error code) from a possibly malformed body."""
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return f"HTTP {status}", None, None
    message = err.get("message")
    meta = err.get("metadata")
    step = meta.get("failed_routing_step") if isinstance(meta, dict) else None
    code = err.get("code")
    return (
        message if isinstance(message, str) and message else f"HTTP {status}",
        step if isinstance(step, str) else None,
        code if isinstance(code, int) else None,
    )


def _choice_parts(data: dict[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
    """The first choice's message (a dict, possibly empty) and its finish_reason."""
    choice = (data.get("choices") or [{}])[0]
    if not isinstance(choice, dict):
        return {}, None
    message = choice.get("message")
    reason = choice.get("finish_reason")
    return (
        message if isinstance(message, dict) else {},
        reason if isinstance(reason, str) else None,
    )


def _first_choice(data: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """The first choice's text content (None unless a string) and its finish_reason."""
    message, reason = _choice_parts(data)
    text = message.get("content")
    return (text if isinstance(text, str) else None), reason


def _round_body(
    body: dict[str, Any], history: list[dict[str, Any]], allow_tools: bool
) -> dict[str, Any]:
    """One tool-loop round's request: `body` with this round's messages. allow_tools=False
    keeps `tools` and provider.require_parameters (the history holds tool messages, so the
    provider must still speak tools) and adds tool_choice="none" to forbid more calls."""
    round_body = {**body, "messages": history}
    if not allow_tools:
        round_body["tool_choice"] = "none"
    return round_body


class OpenRouterClient:
    name = "openrouter"
    label = "OpenRouter"

    def __init__(
        self,
        config: Config,
        api_key: str,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sync_transport: Optional[httpx.BaseTransport] = None,
        config_path: Path = CONFIG_PATH,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._transport = transport
        self._sync_transport = sync_transport
        self._config_path = config_path
        self._sleep = sleep
        self._clock = clock
        self._http: Optional[httpx.AsyncClient] = None
        self._catalog: list[ORModel] = []
        self._catalog_loaded = False
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._privacy: dict[str, tuple[PrivacyStatus, Optional[float]]] = {}
        self._deny_models: set[str] = set()
        self._accepted: set[str] = set(config.openrouter.accepted_models)
        self._notified: set[str] = set()
        self._repo_access: set[str] = set(config.openrouter.repo_access_models)
        self._repo_declined: set[str] = set()  # asked and declined: not asked again this process
        self._gated: set[str] = set()
        self._cooling: dict[str, float] = {}

    # --- Backend surface -------------------------------------------------------------------

    @property
    def supports_files(self) -> bool:
        # True for the backend; whether a given model gets the file tools is tools_supported().
        return True

    @property
    def web_supported(self) -> bool:
        return False

    @property
    def web_default(self) -> bool:
        return False

    @property
    def default_thinking(self) -> ThinkingLevel:
        return self._config.default_thinking

    @property
    def default_model(self) -> str:
        return self.resolve_model(None)

    @property
    def block_data_collection(self) -> bool:
        return self._config.openrouter.block_data_collection

    @property
    def catalog(self) -> list[ORModel]:
        return self._catalog

    @property
    def catalog_loaded(self) -> bool:
        return self._catalog_loaded

    # --- Catalog and models ----------------------------------------------------------------

    def refresh_catalog(self) -> bool:
        """Fetch the public catalog (no key sent). On any failure keep the previous one."""
        try:
            with httpx.Client(
                timeout=CATALOG_TIMEOUT_SECONDS, transport=self._sync_transport
            ) as http:
                resp = http.get(f"{OPENROUTER_BASE_URL}/models")
            resp.raise_for_status()
            catalog = parse_catalog(resp.json())
        except httpx.HTTPStatusError as exc:
            _log.warning("OpenRouter catalog unavailable (HTTP %d)", exc.response.status_code)
            return False
        except (httpx.HTTPError, ValueError) as exc:
            _log.warning("OpenRouter catalog unavailable (%s)", type(exc).__name__)
            return False
        self._catalog = catalog
        self._catalog_loaded = True
        return True

    def entry(self, model_id: str) -> Optional[ORModel]:
        return next((m for m in self._catalog if m.id == model_id), None)

    def tools_supported(self, model: str) -> bool:
        """True when file tools are on and the catalog says `model` accepts tool definitions."""
        if not self._config.file_tools.enabled:
            return False
        entry = self.entry(model)
        return entry is not None and entry.tools_supported

    @property
    def free_candidates(self) -> list[str]:
        """Free models to try now: not gated for this key, not cooling after a 429.

        With file tools on, tool-capable models come first."""
        now = self._clock()
        ids = free_candidates(self._catalog)
        if self._config.file_tools.enabled:
            tooled = free_candidates(self._catalog, tools=True)
            ids = tooled + [m for m in ids if m not in tooled]
        return [m for m in ids if m not in self._gated and self._cooling.get(m, 0.0) <= now]

    def fallback_for(self, model: str) -> Optional[str]:
        if not is_free(model):
            return None
        return next((m for m in self.free_candidates if m != model), None)

    @staticmethod
    def can_fall_back(exc: ClientError) -> bool:
        return isinstance(exc, OpenRouterError) and (exc.status in (429, 503) or exc.gated)

    def cached_privacy(self, model: str) -> Optional[PrivacyStatus]:
        entry = self._privacy.get(model)
        if entry is None:
            return None
        status, expires = entry
        if expires is not None and expires <= self._clock():
            return None
        return status

    async def privacy_status(self, model: str) -> PrivacyStatus:
        """Probe whether any provider for `model` may collect data (one tiny deny-request).

        Caveat: a 404 "Filter by Data Policy" under deny is read as ALL_MAY_COLLECT, but it can
        also reflect the account's own provider restrictions (deny intersected with the account's
        allowed set is empty). Definite answers are cached for the process; UNKNOWN only for
        FREE_COOLDOWN_SECONDS, so a transient failure does not block a model for good.
        """
        cached = self.cached_privacy(model)
        if cached is not None:
            return cached
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "."}],
            "max_tokens": PROBE_MAX_TOKENS,
            "provider": dict(_DENY),
        }
        try:
            await self._post(body, retries=0)
            status = PrivacyStatus.SOME_MAY_COLLECT
        except OpenRouterError as exc:
            if exc.status in (401, 402):
                raise
            status = PrivacyStatus.ALL_MAY_COLLECT if exc.data_policy else PrivacyStatus.UNKNOWN
        expires = self._clock() + FREE_COOLDOWN_SECONDS if status is PrivacyStatus.UNKNOWN else None
        self._privacy[model] = (status, expires)
        return status

    async def preflight(
        self, model: str, elicit: Optional[Elicit], *, want_tools: bool = False
    ) -> Preflight:
        """First-use price and privacy check for `model`, then (with `want_tools`) repository
        consent. Fails closed when sidekick can't ask; 401/402 from the probe propagate.

        `tools` in the result is True only when the user has let `model` read repository files."""
        if model in self._accepted or model in self._notified:
            if not want_tools:
                return Preflight(True, tools=False)
            return Preflight(True, tools=await self._repo_consent(model, elicit))
        if elicit is None and not is_free(model):  # even the probe is billed: send nothing (#141)
            return Preflight(False, message=unattended_paid_refused_message(model))
        status = await self.privacy_status(model)
        block = self._config.openrouter.block_data_collection
        if block and status is PrivacyStatus.ALL_MAY_COLLECT:
            return Preflight(
                False, message=f"[sidekick] {blocked_message(model)} Nothing was sent."
            )
        entry = self.entry(model)
        outcome = (
            await elicit(
                acceptance_question(model, entry, status, block=block, repo_access=want_tools)
            )
            if elicit
            else "unavailable"
        )
        if outcome == "accept":
            self._accepted.add(model)
            self._remember(remember_accepted_model, model, "accepted")
            if want_tools:  # the question carried REPO_ACCESS_SENTENCE: one answer covers both
                self._allow_repo(model)
            return Preflight(True, tools=want_tools)
        if outcome == "decline":
            return Preflight(False, message=declined_message(model))
        if outcome == "cancel":
            return Preflight(False, message=dismissed_message(model))
        if not is_free(model):  # can't ask about a price: never charge unasked (#141)
            return Preflight(False, message=unattended_paid_refused_message(model))
        if status is not PrivacyStatus.SOME_MAY_COLLECT:
            return Preflight(False, message=unattended_refused_message(model, status))
        self._notified.add(model)
        self._deny_models.add(model)  # unattended: privacy-only routing for this model
        # Text notice, no consent needed: free (no charge) and deny-routed (no collection).
        return Preflight(
            True,
            notice=first_use_notice(
                model, entry, status, block=block, forced_deny=True, repo_access=False
            ),
            tools=False,
        )

    async def _repo_consent(self, model: str, elicit: Optional[Elicit]) -> bool:
        """Whether an already-accepted `model` may read repository files, asking once if needed."""
        if model in self._repo_access:
            return True
        if model in self._repo_declined or elicit is None:
            return False
        status = await self.privacy_status(model)
        block = self._config.openrouter.block_data_collection
        outcome = await elicit(repo_access_question(model, self.entry(model), status, block=block))
        if outcome == "accept":
            self._allow_repo(model)
            return True
        if outcome in ("decline", "cancel"):
            self._repo_declined.add(model)
        return False

    def _allow_repo(self, model: str) -> None:
        self._repo_access.add(model)
        self._remember(remember_repo_access, model, "repository access granted")

    def _remember(self, write: Callable[[str, Path], None], model: str, what: str) -> None:
        try:
            write(model, self._config_path)
        except (ConfigError, OSError) as exc:
            _log.warning(
                "%s for %s this session only; config not updated (%s)",
                what,
                model,
                type(exc).__name__,
            )

    def resolve_model(self, model: Optional[str]) -> str:
        if model is None:
            configured = self._config.openrouter.default_model
            if configured:
                return configured
            free = self.free_candidates
            return free[0] if free else PINNED_FREE_MODEL
        if "/" in model:
            return model
        return resolve_gemini_alias(model, self._catalog, _PINNED_BY_FAMILY)

    # --- Sessions --------------------------------------------------------------------------

    def get_or_create_session(self, name: str = "default", model: Optional[str] = None) -> Session:
        key = f"{name}:{model or '@default'}"
        session = self._sessions.get(key)
        if session is not None:
            self._sessions.move_to_end(key)
            session.used_at = self._clock()
            if model is None:
                session.model = self.resolve_model(None)
            return session
        session = Session(model=self.resolve_model(model), name=name, used_at=self._clock())
        self._sessions[key] = session
        if len(self._sessions) > MAX_SESSIONS:
            evicted, _ = self._sessions.popitem(last=False)
            _log.debug("session cache evicted (LRU): %s", evicted)
        return session

    def sessions(self) -> list[Session]:
        return list(reversed(self._sessions.values()))

    # --- Chat ------------------------------------------------------------------------------

    async def ask(
        self,
        session: Session,
        prompt: str,
        thinking: Optional[ThinkingLevel] = None,
        system_instruction: Optional[str] = None,
        registry: Optional["ToolRegistry"] = None,
        records: Optional[list["ToolCallRecord"]] = None,
        web: bool = False,
    ) -> str:
        model = session.model
        entry = self.entry(model)
        max_tokens = min(
            DEFAULT_MAX_TOKENS, (entry and entry.max_completion_tokens) or DEFAULT_MAX_TOKENS
        )
        messages: list[dict[str, Any]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages += [{"role": m.role, "content": m.content} for m in session.history]
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        block = self._config.openrouter.block_data_collection
        deny = block or model in self._deny_models
        tool_registry = registry if registry is not None and self.tools_supported(model) else None
        provider: dict[str, Any] = dict(_DENY) if deny else {}
        if tool_registry is not None:
            body["tools"] = tool_registry.openai_tools()
            provider[_REQUIRE_PARAMETERS] = True
        if provider:
            body["provider"] = provider
        effort = map_effort(thinking or self.default_thinking, entry)
        if effort is not None:
            body["reasoning"] = {"effort": effort, "exclude": True}
        retries = FREE_MAX_RETRIES if is_free(model) else _MAX_RETRIES
        try:
            if tool_registry is None:
                text, reason = _first_choice(await self._post(body, retries=retries))
            else:

                async def post(history: list[dict[str, Any]], allow_tools: bool) -> dict[str, Any]:
                    round_body = _round_body(body, history, allow_tools)
                    message, finish = _choice_parts(await self._post(round_body, retries=retries))
                    return {"message": message, "finish_reason": finish}

                text, reason = await run_openai_tool_loop(
                    post, messages, tool_registry, records if records is not None else []
                )
        except OpenRouterError as exc:
            if exc.data_policy:
                if tool_registry is not None:
                    clause = " and doesn't collect data" if deny else ""
                    refusal = (
                        f"[sidekick] Not sent: no provider for `{model}` that supports tool "
                        f"calling{clause} is available right now. Retry without repository "
                        "access by pasting the code, or pick another model (list_models)."
                    )
                elif block:
                    refusal = blocked_message(model)
                elif model in self._deny_models:
                    refusal = (
                        f"[sidekick] Not sent: no provider for `{model}` that doesn't "
                        "collect data is available right now."
                    )
                else:
                    refusal = account_policy_message(model)
                raise OpenRouterError(refusal, status=404, routing_step=exc.routing_step) from exc
            if is_free(model):
                if exc.gated:
                    self._gated.add(model)
                elif exc.status in (429, 503):
                    self._cooling[model] = self._clock() + FREE_COOLDOWN_SECONDS
            raise

        if not isinstance(text, str) or not text.strip():
            if reason == "length":
                raise ClientError(
                    f"{model} spent its whole output budget (max_tokens={max_tokens}) on "
                    "reasoning and returned no answer. Retry with thinking='low' or "
                    "thinking='none', or use another model."
                )
            raise ClientError(f"{model} returned an empty answer (finish_reason={reason}).")
        session.history += [ChatMessage("user", prompt), ChatMessage("assistant", text)]
        return text

    async def _post(self, body: dict[str, Any], *, retries: int) -> dict[str, Any]:
        """POST /chat/completions with retries; return the parsed 2xx body or raise."""
        model = str(body.get("model", ""))
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=OPENROUTER_BASE_URL,
                timeout=CHAT_TIMEOUT,
                transport=self._transport,
                headers={"Authorization": f"Bearer {self._api_key}", **ATTRIBUTION_HEADERS},
            )
        for attempt in range(1, retries + 2):
            is_last = attempt > retries
            try:
                resp = await self._http.post("/chat/completions", json=body)
            except httpx.TransportError as exc:
                # Only errors raised before the request left are safe to resend: a read or
                # write timeout may already have been billed (a paid call up to 4x).
                if is_last or not isinstance(exc, _SAFE_RETRY_ERRORS):
                    raise OpenRouterError(
                        f"Could not reach OpenRouter ({type(exc).__name__})."
                    ) from exc
                _log.warning("OpenRouter transport error (%s), retrying", type(exc).__name__)
            else:
                if resp.status_code in _RETRY_STATUSES and not is_last:
                    _log.warning("OpenRouter HTTP %d, retrying", resp.status_code)
                else:
                    return self._parse(resp, model)
            delay = _RETRY_BASE_DELAY * 2 ** (attempt - 1) + random.uniform(0, 0.5)
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _parse(resp: httpx.Response, model: str) -> dict[str, Any]:
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = None
        status = resp.status_code
        if 200 <= status < 300:
            if isinstance(payload, dict) and payload.get("choices"):
                return payload
            if not (isinstance(payload, dict) and isinstance(payload.get("error"), dict)):
                raise OpenRouterError(
                    f"OpenRouter returned an unreadable reply for {model} (HTTP {status}).",
                    status=status,
                )
        message, step, code = _error_parts(payload, status)
        status = code if 200 <= status < 300 and code is not None else status
        if status == 401:
            raise OpenRouterError(_KEY_REJECTED, status=401, routing_step=step)
        if status == 402:
            raise OpenRouterError(_NO_CREDITS, status=402, routing_step=step)
        raise OpenRouterError(
            f"OpenRouter request for {model} failed (HTTP {status}): {message}",
            status=status,
            routing_step=step,
        )
