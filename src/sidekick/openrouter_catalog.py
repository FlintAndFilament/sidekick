"""
sidekick/openrouter_catalog.py
------------------------------
Pure helpers for the OpenRouter backend: parse the live model catalog, format prices,
pick free candidates, and map sidekick's thinking levels to OpenRouter reasoning efforts.

Responsibilities:
  - Parse /models entries into frozen ORModel records (bad entries are skipped, never raised)
  - Format per-token prices per million tokens, and context sizes
  - Rank free (':free') text models for the fallback chain
  - Map a ThinkingLevel to a model's supported reasoning effort
  - Resolve Gemini aliases (flash / pro / -latest) to 'google/...' catalog ids

Design notes:
  - No HTTP and no import from client.py; everything here is a pure function of its inputs
  - Free means the id ends in ':free'; a zero price alone is not free (openrouter/free)

Used by:  OpenRouter client and setup tooling
Imports:  sidekick.models, sidekick.config
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Optional

from sidekick import models
from sidekick.config import ThinkingLevel

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

SETUP_PICKS: tuple[tuple[str, str], ...] = (
    ("deepseek/deepseek-v4.1-flash-1017", "best value (Artificial Analysis 33)"),
    ("moonshotai/kimi-k3.1-pro", "best quality (Artificial Analysis 46)"),
    ("x-ai/grok-4.7", "Artificial Analysis 35"),
    ("qwen/qwen3.8-max-0902", "Artificial Analysis 34"),
    ("google/gemini-3.8-flash", "Artificial Analysis 31"),
)
FREE_PREFERENCES = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3.5-super-vl:free",
)
PINNED_FREE_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
FREE_LIMITS = "Free models allow 20 requests a minute and 50 a day (1,000 a day once you have bought at least 10 credits)."
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

_MILLION = Decimal(1_000_000)
_CENT = Decimal("0.01")


def model_page(model_id: str) -> str:
    return "https://openrouter.ai/" + model_id


class PrivacyStatus(str, Enum):
    ALL_MAY_COLLECT = "all"
    SOME_MAY_COLLECT = "some"
    UNKNOWN = "unknown"


def privacy_sentence(model_id: str, status: PrivacyStatus, *, block: bool) -> str:
    if status is PrivacyStatus.ALL_MAY_COLLECT:
        return "Every provider serving it may store or train on your prompts, including any code sent with them."
    if status is PrivacyStatus.SOME_MAY_COLLECT:
        text = (
            "Some providers serving it may store or train on your prompts, "
            "and OpenRouter may route this call to one of them."
        )
        if block:
            text += (
                " Your block_data_collection setting keeps it to providers that don't collect data."
            )
        return text
    return (
        f"Whether its providers store or train on prompts is unknown; see {model_page(model_id)}."
    )


def blocked_message(model_id: str) -> str:
    return (
        "Blocked by your setting `block_data_collection`: every provider serving "
        f"`{model_id}` may store or train on prompts."
    )


def unattended_refused_message(model: str, status: PrivacyStatus) -> str:
    why = (
        "Every provider serving it may store or train on prompts."
        if status is PrivacyStatus.ALL_MAY_COLLECT
        else "Its privacy status could not be checked."
    )
    return (
        f"[sidekick] Not sent: `{model}` hasn't been accepted yet and sidekick couldn't ask "
        f"you here. {why} Accept it once in an interactive session "
        "(/sidekick:setup or any call there)."
    )


def account_policy_message(model_id: str) -> str:
    return (
        f"OpenRouter found no provider for `{model_id}` that your OpenRouter account's "
        "privacy settings allow (https://openrouter.ai/settings/privacy). "
        "Change them there, or pick another model."
    )


def is_free(model_id: str) -> bool:
    return model_id.endswith(":free")


@dataclass(frozen=True)
class ORModel:
    id: str
    name: str
    prompt_price: Optional[Decimal]
    completion_price: Optional[Decimal]
    context_length: Optional[int]
    max_completion_tokens: Optional[int]
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    reasoning_supported: bool
    reasoning_mandatory: bool
    supported_efforts: tuple[str, ...]

    @property
    def free(self) -> bool:
        return is_free(self.id)

    @property
    def text_chat(self) -> bool:
        return (
            "text" in self.input_modalities
            and self.output_modalities == ("text",)
            and not self.id.endswith(":batch")
        )


def _price(value: Any) -> Optional[Decimal]:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return price if price.is_finite() and price >= 0 else None


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_model(raw: Mapping[str, Any]) -> Optional[ORModel]:
    """One catalog entry as an ORModel, or None when it has no string id."""
    model_id = raw.get("id")
    if not isinstance(model_id, str):
        return None
    pricing = _dict(raw.get("pricing"))
    arch = _dict(raw.get("architecture"))
    reasoning = _dict(raw.get("reasoning"))
    name = raw.get("name")
    return ORModel(
        id=model_id,
        name=name if isinstance(name, str) else model_id,
        prompt_price=_price(pricing["prompt"]) if "prompt" in pricing else None,
        completion_price=_price(pricing["completion"]) if "completion" in pricing else None,
        context_length=_int(raw.get("context_length")),
        max_completion_tokens=_int(_dict(raw.get("top_provider")).get("max_completion_tokens")),
        input_modalities=_strings(arch.get("input_modalities")),
        output_modalities=_strings(arch.get("output_modalities")),
        reasoning_supported="reasoning" in _strings(raw.get("supported_parameters")),
        reasoning_mandatory=reasoning.get("mandatory") is True,
        supported_efforts=_strings(reasoning.get("supported_efforts")),
    )


def parse_catalog(payload: Any) -> list[ORModel]:
    """All parseable entries of a /models payload; ValueError when it has no 'data' list."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("OpenRouter catalog payload has no 'data' list")
    parsed = (parse_model(entry) for entry in data if isinstance(entry, dict))
    return [m for m in parsed if m is not None]


def format_price(per_token: Optional[Decimal]) -> str:
    """A per-token price as dollars per million tokens."""
    if per_token is None:
        return "varies"
    if per_token == 0:
        return "$0"
    per_million = (per_token * _MILLION).quantize(_CENT, rounding=ROUND_HALF_UP)
    return f"${per_million}" if per_million >= _CENT else "<$0.01"


def price_pair(model: ORModel) -> str:
    if model.free:
        return "free"
    if model.prompt_price is None or model.completion_price is None:
        return f"price varies, see {model_page(model.id)}"
    return f"{format_price(model.prompt_price)} in / {format_price(model.completion_price)} out per 1M tokens"


def format_context(tokens: Optional[int]) -> str:
    if tokens is None:
        return "?"
    if tokens >= 1_000_000:
        return f"{round(tokens / 1_000_000, 1):g}M"
    return f"{tokens // 1000}k"


def free_candidates(catalog: Sequence[ORModel]) -> list[str]:
    """Free text-chat model ids: the preferred ones first, then the rest by context size."""
    free = {m.id: m for m in catalog if m.free and m.text_chat}
    preferred = [i for i in FREE_PREFERENCES if i in free]
    rest = sorted(
        (m for i, m in free.items() if i not in preferred),
        key=lambda m: (-(m.context_length or 0), m.id),
    )
    return preferred + [m.id for m in rest]


def map_effort(level: ThinkingLevel, model: Optional[ORModel]) -> Optional[str]:
    """The reasoning effort to send for a sidekick level, or None to omit the field."""
    if model is None:
        return None if level == "none" else level
    if not model.reasoning_supported:
        return None
    efforts = model.supported_efforts
    if not efforts:
        return None if level == "none" else level
    if level in efforts:
        return level
    ranked = sorted((e for e in efforts if e in EFFORT_ORDER), key=EFFORT_ORDER.index)
    if level == "none":
        # Unsupported "none": a mandatory-reasoning model gets its lowest effort, others omit.
        return ranked[0] if ranked and model.reasoning_mandatory else None
    if not ranked:
        return level
    above = [e for e in ranked if EFFORT_ORDER.index(e) > EFFORT_ORDER.index(level)]
    return above[0] if above else ranked[-1]


def resolve_gemini_alias(model: str, catalog: Sequence[ORModel], pinned: Mapping[str, str]) -> str:
    """A bridge or '-latest' alias, or a bare Gemini id, as a 'google/...' OpenRouter id."""
    family = models.alias_family(model)
    if family is None:
        return "google/" + model
    google_ids = [m.id for m in catalog if m.id.startswith("google/")]
    bare = models.resolve_latest(google_ids).get(family) or pinned[family]
    return "google/" + bare


def subject_phrase(model_id: str, entry: Optional[ORModel]) -> str:
    """What the user is being asked to use: a free model, a priced one, or an unpriced one."""
    if is_free(model_id):
        return f"the free model `{model_id}`"
    if entry is not None and entry.prompt_price is not None and entry.completion_price is not None:
        return f"`{model_id}` at {price_pair(entry)}"
    return f"`{model_id}` (price not shown in OpenRouter's catalog: see {model_page(model_id)})"


def acceptance_question(
    model_id: str, entry: Optional[ORModel], status: PrivacyStatus, *, block: bool
) -> str:
    question = (
        f"Use {subject_phrase(model_id, entry)}? {privacy_sentence(model_id, status, block=block)}"
    )
    return question + " " + FREE_LIMITS if is_free(model_id) else question


def first_use_notice(
    model_id: str,
    entry: Optional[ORModel],
    status: PrivacyStatus,
    *,
    block: bool,
    forced_deny: bool = False,
) -> str:
    if forced_deny and status is PrivacyStatus.SOME_MAY_COLLECT:
        privacy = "Some providers serving it may store or train on your prompts."
    else:
        privacy = privacy_sentence(model_id, status, block=block)
    limits = " " + FREE_LIMITS if is_free(model_id) else ""
    forced = (
        " Sent with privacy-only routing (providers that don't collect data)."
        if forced_deny
        else ""
    )
    return (
        f"[sidekick notice] First use of {subject_phrase(model_id, entry)}. "
        f"{privacy}{limits}{forced} Shown once; accept it in /sidekick:setup to skip this.\n\n"
    )


def declined_message(model_id: str) -> str:
    return (
        f"[sidekick] Not sent: you declined `{model_id}`, so nothing went to OpenRouter. "
        "Pass another model= (list_models shows prices and privacy), or omit model= for the default."
    )


def dismissed_message(model_id: str) -> str:
    return (
        f"[sidekick] Not sent: the question about `{model_id}` was dismissed, so nothing went "
        "to OpenRouter. To use this model without being asked (for example in a non-interactive "
        "session), accept it once in /sidekick:setup."
    )
