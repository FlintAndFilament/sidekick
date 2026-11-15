"""Every consent question logs one line: kind, model, outcome — never the question (#148)."""

import logging
from pathlib import Path

import pytest

from sidekick.router import Router
from tests._openrouter_fixtures import FakeOpenRouter, make_client, reply
from tests.test_backends import _gemini
from tests.test_list_models_openrouter import _call

GROK, NEMO = "x-ai/grok-4.7", "nvidia/nemotron-3-ultra-550b-a55b:free"


def _asker(*outcomes):
    asked: list[str] = []

    async def elicit(message: str):
        asked.append(message)
        return outcomes[min(len(asked), len(outcomes)) - 1]

    return elicit


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "sidekick.consent"]


@pytest.fixture(autouse=True)
def _info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="sidekick.consent")


@pytest.mark.parametrize("outcome", ["accept", "decline", "cancel", "unavailable"])
async def test_model_use_question_logs_its_outcome(tmp_path, caplog, outcome):
    c = make_client(FakeOpenRouter(replies=[(200, reply())]), tmp_path)
    await c.preflight(GROK, _asker(outcome), want_tools=False)
    assert _lines(caplog) == [f"consent model_use model={GROK} outcome={outcome}"]


async def test_paid_model_that_cannot_be_asked_logs_unavailable(tmp_path, caplog):
    c = make_client(FakeOpenRouter(), tmp_path)
    await c.preflight(GROK, None, want_tools=True)
    assert _lines(caplog) == [f"consent model_use model={GROK} outcome=unavailable"]


async def test_repo_access_question_logs_after_model_use(tmp_path, caplog):
    c = make_client(FakeOpenRouter(replies=[(200, reply())]), tmp_path)
    await c.preflight(GROK, _asker("accept", "decline"), want_tools=True)
    assert _lines(caplog) == [
        f"consent model_use model={GROK} outcome=accept",
        f"consent repo_access model={GROK} outcome=decline",
    ]


async def test_repo_access_without_an_asker_logs_unavailable(tmp_path, caplog):
    c = make_client(FakeOpenRouter(), tmp_path, accepted_models=[GROK])
    await c.preflight(GROK, None, want_tools=True)
    assert _lines(caplog) == [f"consent repo_access model={GROK} outcome=unavailable"]


async def test_no_question_asked_logs_nothing(tmp_path, caplog):
    c = make_client(FakeOpenRouter(), tmp_path, accepted_models=[GROK])
    await c.preflight(GROK, _asker("accept"), want_tools=False)
    assert _lines(caplog) == []


@pytest.mark.parametrize("outcome", ["accept", "decline", "unavailable"])
async def test_billed_check_logs_its_outcome(tmp_path, caplog, outcome):
    fake = FakeOpenRouter(replies=[(200, reply())])
    await _call(Router(make_client(fake, tmp_path)), _asker(outcome), check=GROK)
    assert _lines(caplog) == [f"consent billed_check model={GROK} outcome={outcome}"]


async def test_billed_check_without_an_asker_logs_unavailable(tmp_path, caplog):
    await _call(Router(make_client(FakeOpenRouter(), tmp_path)), None, check=GROK)
    assert _lines(caplog) == [f"consent billed_check model={GROK} outcome=unavailable"]


@pytest.mark.parametrize("outcome", ["accept", "decline", "unavailable"])
async def test_gemini_data_use_logs_its_outcome(tmp_path: Path, caplog, outcome):
    path = tmp_path / "config.json"
    path.write_text('{"auth": {"method": "api_key"}}')
    c = _gemini(consent=True, config_path=path)
    await c.preflight("gemini-3.5-flash", _asker(outcome))
    assert _lines(caplog) == [f"consent gemini_data_use model=gemini-3.5-flash outcome={outcome}"]


async def test_gemini_without_an_asker_logs_unavailable(tmp_path, caplog):
    c = _gemini(consent=True, config_path=tmp_path / "config.json")
    await c.preflight("gemini-3.5-flash", None)
    assert _lines(caplog) == ["consent gemini_data_use model=gemini-3.5-flash outcome=unavailable"]


async def test_log_line_never_carries_the_question_text(tmp_path, caplog):
    c = make_client(FakeOpenRouter(replies=[(200, reply())]), tmp_path)
    await c.preflight(GROK, _asker("accept", "accept"), want_tools=True)
    for line in _lines(caplog):
        assert "$" not in line and "?" not in line and "repository" not in line
