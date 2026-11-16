"""
sidekick/tools/__init__.py
---------------------------------
Tool package root — re-exports all MCP tool registration callables.

Responsibilities:
  - Collect tool modules into a single importable namespace
  - Collect the generating tools' capability rows so server.py can advertise them (#74)

Design notes:
  - Open/Closed: new tools add a module here + one import line — server.py unchanged

Used by:  server.py (registers all tools)
Imports:  tools/ask.py, tools/brainstorm.py, tools/review.py, tools/debug.py, tools/architect.py,
          tools/list_models.py, tools/list_sessions.py, tools/help.py
"""

from collections.abc import Mapping
from dataclasses import replace

from sidekick.config import AccessLevel, ToolName
from sidekick.tools.architect import CAPABILITY as CAPABILITY_ARCHITECT
from sidekick.tools.architect import register as register_architect
from sidekick.tools.ask import CAPABILITY as CAPABILITY_ASK
from sidekick.tools.ask import register as register_ask
from sidekick.tools.base import ToolCapability
from sidekick.tools.brainstorm import CAPABILITY as CAPABILITY_BRAINSTORM
from sidekick.tools.brainstorm import register as register_brainstorm
from sidekick.tools.debug import CAPABILITY as CAPABILITY_DEBUG
from sidekick.tools.debug import register as register_debug
from sidekick.tools.help import HELP_TOOL_NAME
from sidekick.tools.help import register as register_help
from sidekick.tools.list_models import LIST_MODELS_TOOL_NAME
from sidekick.tools.list_models import register as register_list_models
from sidekick.tools.list_sessions import LIST_SESSIONS_TOOL_NAME
from sidekick.tools.list_sessions import register as register_list_sessions
from sidekick.tools.review import CAPABILITY as CAPABILITY_REVIEW
from sidekick.tools.review import register as register_review

# Registration order, and the order the capability rows are advertised in.
CAPABILITIES: tuple[ToolCapability, ...] = (
    CAPABILITY_ASK,
    CAPABILITY_BRAINSTORM,
    CAPABILITY_REVIEW,
    CAPABILITY_DEBUG,
    CAPABILITY_ARCHITECT,
)


def effective_capabilities(overrides: Mapping[ToolName, AccessLevel]) -> tuple[ToolCapability, ...]:
    """CAPABILITIES with each tool's access level replaced where `overrides` names it (#132)."""
    levels: dict[str, AccessLevel] = {str(k): v for k, v in overrides.items()}
    return tuple(replace(c, access=levels.get(c.name, c.access)) for c in CAPABILITIES)


__all__ = [
    "register_ask",
    "register_brainstorm",
    "register_review",
    "register_debug",
    "register_architect",
    "register_list_models",
    "register_list_sessions",
    "register_help",
    "CAPABILITIES",
    "effective_capabilities",
    "LIST_MODELS_TOOL_NAME",
    "LIST_SESSIONS_TOOL_NAME",
    "HELP_TOOL_NAME",
]
