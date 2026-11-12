"""
sidekick/openrouter.py
----------------------------
OpenRouter chat backend: catalog, sessions, chat completions, retries, clear errors.

Responsibilities:
  - Load the public model catalog (no key sent) and resolve model ids and Gemini aliases
  - Keep named sessions (LRU-capped); history is committed only when a call succeeds
  - Send chat completions over httpx and turn every failure into a ClientError

Design notes:
  - Conversation only: no file tools, no web, no write_file (ask() ignores registry/records/web)
  - Retries cover 429/502/503 and transport errors; free models retry once, paid ones more
  - Logs carry type names and statuses only, never headers, bodies or the key
  - _post and OpenRouterError are the shared seam for the privacy probe and free rotation

Used by:  __main__.py / tools (via the Backend protocol)
Imports:  config.py, backends.py, client.py (retry and session constants), openrouter_catalog.py
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
)
from sidekick.errors import ClientError
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
    resolve_gemini_alias,
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
        self._gated: set[str] = set()
        self._cooling: dict[str, float] = {}

    # --- Backend surface -------------------------------------------------------------------

    @property
    def supports_files(self) -> bool:
        return False

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

    @property
    def free_candidates(self) -> list[str]:
        """Free models to try now: not gated for this key, not cooling after a 429."""
        now = self._clock()
        return [
            m
            for m in free_candidates(self._catalog)
            if m not in self._gated and self._cooling.get(m, 0.0) <= now
        ]

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

    async def preflight(self, model: str, elicit: Optional[Elicit]) -> Preflight:
        """First-use price and privacy check for `model`; 401/402 from the probe propagate."""
        if model in self._accepted or model in self._notified:
            return Preflight(True)
        status = await self.privacy_status(model)
        block = self._config.openrouter.block_data_collection
        if block and status is PrivacyStatus.ALL_MAY_COLLECT:
            return Preflight(
                False, message=f"[sidekick] {blocked_message(model)} Nothing was sent."
            )
        entry = self.entry(model)
        outcome = (
            await elicit(acceptance_question(model, entry, status, block=block))
            if elicit
            else "unavailable"
        )
        if outcome == "accept":
            self._accepted.add(model)
            try:
                remember_accepted_model(model, self._config_path)
            except (ConfigError, OSError) as exc:
                _log.warning(
                    "accepted %s for this session only; config not updated (%s)",
                    model,
                    type(exc).__name__,
                )
            return Preflight(True)
        if outcome == "decline":
            return Preflight(False, message=declined_message(model))
        if outcome == "cancel":
            return Preflight(False, message=dismissed_message(model))
        if status is not PrivacyStatus.SOME_MAY_COLLECT:
            return Preflight(False, message=unattended_refused_message(model, status))
        self._notified.add(model)
        self._deny_models.add(model)  # unattended: privacy-only routing for this model
        return Preflight(
            True, notice=first_use_notice(model, entry, status, block=block, forced_deny=True)
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
        messages: list[dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages += [{"role": m.role, "content": m.content} for m in session.history]
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        block = self._config.openrouter.block_data_collection
        if block or model in self._deny_models:
            body["provider"] = dict(_DENY)
        effort = map_effort(thinking or self.default_thinking, entry)
        if effort is not None:
            body["reasoning"] = {"effort": effort, "exclude": True}
        retries = FREE_MAX_RETRIES if is_free(model) else _MAX_RETRIES
        try:
            data = await self._post(body, retries=retries)
        except OpenRouterError as exc:
            if exc.data_policy:
                if block:
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

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        text = message.get("content") if isinstance(message, dict) else None
        reason = choice.get("finish_reason") if isinstance(choice, dict) else None
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
                if is_last:
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
