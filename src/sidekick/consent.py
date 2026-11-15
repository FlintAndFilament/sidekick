"""One log line per consent question (#148).

Responsibilities:
  - ask(): put a question to the user through an Elicit (or answer "unavailable" when there is
    none) and log the outcome
  - record(): log a question that could not be put at all, so a refusal is still traced

Invariants:
  - Logs the kind, the model id and the outcome only: never the question text, prompt text or
    key material
  - Exactly one line per question that was asked, or would have been asked
"""

import logging
from typing import Literal, Optional

from sidekick.backends import Elicit, ElicitOutcome

ConsentKind = Literal["model_use", "repo_access", "billed_check", "gemini_data_use"]

_log = logging.getLogger("sidekick.consent")


def record(kind: ConsentKind, model: str, outcome: ElicitOutcome) -> None:
    _log.info("consent %s model=%s outcome=%s", kind, model, outcome)


async def ask(
    elicit: Optional[Elicit], kind: ConsentKind, model: str, question: str
) -> ElicitOutcome:
    """The user's answer to `question`, or "unavailable" when sidekick can't ask; logged."""
    outcome: ElicitOutcome = await elicit(question) if elicit else "unavailable"
    record(kind, model, outcome)
    return outcome
