"""
sidekick/models.py
-----------------------
Single source of truth for backend-aware model discoverability.

Responsibilities:
  - Define the curated, recommended model shortlist per backend
  - Detect the active backend from the auth method
  - Build the schema-hint string injected into every tool's `model` param
  - Filter a live models.list() down to chat-capable models
  - Resolve "latest" per family (flash / flash-lite / pro) from a live catalog, and map the
    bridge's short aliases and Google's '-latest' aliases to a family

Design notes:
  - Single Responsibility: pure data + small pure functions; no SDK, no network, no I/O
  - Dependency Inversion: depends on nothing else in the package (avoids a circular import
    with client.py, which owns DEFAULT_MODEL/FALLBACK_MODEL). Consumers that need both a
    model constant AND these helpers import each from its own module.
  - Open/Closed: adding a model means editing RECOMMENDED here and nowhere else — the docs
    went stale precisely because there was no single source.

Curated ids verified live 2026-07-08:
  - Developer API `-latest` aliases (gemini-flash-latest, gemini-pro-latest) confirmed present.
  - Vertex GA text models confirmed against Google Cloud model docs; gemini-3.5-flash is GA.
    Vertex has NO `-latest` aliases (they 404), so its shortlist is alias-free.

Used by:  tools/base.py (schema hints), tools/list_models.py (live filter)
Imports:  (stdlib only)
"""

import re
from collections.abc import Iterable
from typing import Optional, Protocol, Sequence


class ModelMeta(Protocol):
    """Structural shape of a models.list() entry.

    Matched by google.genai.types.Model at runtime; kept as a Protocol so this module
    depends on nothing (no SDK import) and tests can pass lightweight fakes.
    """

    name: str
    supported_actions: Optional[Sequence[str]]


# Backend identifiers.
DEVELOPER_API = "developer"  # api_key mode (Google AI Studio Developer API)
VERTEX = "vertex"  # adc/env/keychain modes (Vertex AI)

# Curated, recommended shortlist per backend: (model_id, one-line label). Since #69 the live
# default is resolved at startup (resolve_latest) and callers can use the flash / flash-lite /
# pro aliases; this list is the OFFLINE view — shown when the catalog can't be read — and so
# mirrors the pinned offline defaults in client.py.
# Order matters — the first entry is presented first and is the default family.
# Curated shortlists lead with the long-lived Gemini 3.x GA models. The Gemini 2.5 line
# (gemini-2.5-flash/pro/flash-lite) retires 2026-10-16, so 2.5-flash is dropped from the curated
# lists (still usable explicitly and shown by list_models); 2.5-pro is kept only on Vertex
# as its stable higher-capability Pro until it retires. Verified 2026-07-09.
RECOMMENDED: dict[str, list[tuple[str, str]]] = {
    DEVELOPER_API: [
        ("gemini-3.5-flash", "most intelligent Flash"),
        ("gemini-3.1-flash-lite", "fastest, most cost-efficient"),
        ("gemini-flash-latest", "auto-tracks newest Flash (Developer API alias)"),
        ("gemini-pro-latest", "auto-tracks newest Pro (Developer API alias)"),
    ],
    VERTEX: [
        ("gemini-3.5-flash", "most intelligent Flash (GA)"),
        ("gemini-3.1-flash-lite", "fastest, most cost-efficient (GA)"),
        ("gemini-3.1-pro-preview", "newest Pro — advanced reasoning (preview)"),
        ("gemini-2.5-pro", "stable higher-capability Pro (GA)"),
    ],
}

# Name substrings that mark a NON-chat model. image/tts models also report 'generateContent',
# so filtering on that action alone is insufficient (verified live 2026-07-08). Beyond the
# media modalities, these also exclude specialized gemini-prefixed variants that are not
# text-chat (computer-use, robotics, omni).
_NON_CHAT_MARKERS = (
    "image",
    "tts",
    "audio",
    "embedding",
    "live",
    "computer-use",
    "robotics",
    "omni",
)

# Allowlist of recognized Gemini chat generations. The live catalog also carries non-chat
# families (gemma, lyria, nano-banana, antigravity, deep-research) that a name blocklist can't
# anticipate, so we invert to an allowlist: only list models the bridge can actually run —
# i.e. the generations _model_family() accepts (Gemini 2 and every later major version).
# Kept in sync with client._model_family.
_CHAT_GENERATION = re.compile(r"gemini-(\d+)(?:[.-]|$)")


# Model families the bridge can resolve to "newest" (#69).
FAMILIES: tuple[str, ...] = ("flash", "flash-lite", "pro")

# Short, backend-agnostic aliases the bridge accepts anywhere a model id is accepted, plus
# Google's Developer-API '-latest' aliases (which 404 on Vertex — translating them here makes
# them work on both backends and records the concrete model that actually answered).
_ALIASES: dict[str, str] = {
    "flash": "flash",
    "flash-lite": "flash-lite",
    "pro": "pro",
    "gemini-flash-latest": "flash",
    "gemini-flash-lite-latest": "flash-lite",
    "gemini-pro-latest": "pro",
}

# Plain versioned ids only: gemini-<major>[.<minor>]-<family>[-preview]. Excludes dated builds
# (-001, -preview-09-2025) and specialized variants (-customtools, -image, -tts, -transcribe).
_VERSIONED_ID = re.compile(r"^gemini-(\d+)(?:\.(\d+))?-(flash-lite|flash|pro)(-preview)?$")


_GEMINI_PATH_PREFIXES = ("models/", "publishers/", "projects/", "tunedModels/")


def is_openrouter_id(model: str) -> bool:
    """True for an OpenRouter `<vendor>/<model>` id; Gemini resource paths are not."""
    return "/" in model and not model.startswith(_GEMINI_PATH_PREFIXES)


def alias_family(model: str) -> Optional[str]:
    """The family a bridge or '-latest' alias stands for, or None for a concrete model id."""
    return _ALIASES.get(model.strip().lower())


def resolve_latest(model_ids: Iterable[str]) -> dict[str, str]:
    """Newest model id per family from a catalog of ids (any 'models/' prefix is ignored).

    Highest (major, minor) wins, compared numerically (3.10 > 3.8). At equal version a GA id
    beats its '-preview'; a newer preview beats an older GA (Google's own '-latest' aliases
    behave the same way — verified live 2026-09-17). Families absent from the catalog are
    omitted.
    """
    best: dict[str, tuple[tuple[int, int, int], str]] = {}
    for raw in model_ids:
        model_id = raw.split("/")[-1]
        match = _VERSIONED_ID.match(model_id)
        if not match:
            continue
        major, minor, family, preview = match.groups()
        key = (int(major), int(minor or 0), 0 if preview else 1)
        if family not in best or key > best[family][0]:
            best[family] = (key, model_id)
    return {family: model_id for family, (_, model_id) in best.items()}


def backend_for(auth_method: str) -> str:
    """Map a config auth method to a backend identifier.

    Only "api_key" is the Developer API; every other method (adc/env/keychain) is Vertex.
    """
    return DEVELOPER_API if auth_method == "api_key" else VERTEX


def shortlist(backend: str) -> list[tuple[str, str]]:
    """Return the curated (id, label) shortlist for a backend.

    Unknown backends fall back to the Developer API list (the safe, verified default).
    """
    return RECOMMENDED.get(backend, RECOMMENDED[DEVELOPER_API])


def schema_hint(backend: str, default_model: str, latest: Optional[dict[str, str]] = None) -> str:
    """Build the one-line `model` param description for a backend.

    Leads with the bridge aliases (and, when resolved, the concrete id each maps to), then the
    recommended ids, the active default, and a pointer to list_models.
    """
    latest = latest or {}
    aliases = ", ".join(
        f"{family} (→ {latest[family]})" if family in latest else family for family in FAMILIES
    )
    ids = ", ".join(model_id for model_id, _ in shortlist(backend))
    return (
        f"Optional Gemini model. Aliases that track the newest release: {aliases}. "
        f"Or any model id, e.g. {ids}. "
        f"Omit to use the server default ({default_model}). "
        "Call list_models for the full, live list."
    )


def is_chat_capable(meta: ModelMeta) -> bool:
    """True if a models.list() entry is a Gemini text/chat model the bridge can run.

    `meta` is a google.genai.types.Model (or any object with `.name` and `.supported_actions`).
    Three gates, in order:
      1. when supported_actions is populated, must include `generateContent` (Vertex AI returns
         None for this field, so the gate is skipped entirely for Vertex models);
      2. must not carry a non-chat marker (image/tts/audio/embedding/live/computer-use/robotics/omni);
      3. must be a recognized Gemini chat generation (gemini-2 or any later major) or a '-latest' alias —
         an allowlist mirroring _model_family(), so non-chat families the bridge can't run
         (gemma, lyria, nano-banana, antigravity, deep-research) never appear.
    Previews (e.g. gemini-3-pro-preview) are intentionally included — they are valid, usable models.
    """
    name = (getattr(meta, "name", "") or "").split("/")[-1]
    actions = getattr(meta, "supported_actions", None)
    if actions is not None and "generateContent" not in actions:
        return False
    if any(marker in name for marker in _NON_CHAT_MARKERS):
        return False
    generation = _CHAT_GENERATION.match(name)
    if generation:
        return int(generation.group(1)) >= 2
    return name.endswith("-latest")
