import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from mcp.server.fastmcp import Context

from sidekick.backends import Preflight
from sidekick.openrouter import OpenRouterError
from sidekick.tools.base import _AcceptForm, elicitor
from tests._openrouter_fixtures import DATA_POLICY_404, FakeOpenRouter, make_client, reply

GROK, NEMO = "x-ai/grok-4.7", "nvidia/nemotron-3-ultra-550b-a55b:free"


def _asker(outcome):
    asked: list[str] = []

    async def elicit(message: str):
        asked.append(message)
        return outcome

    return elicit, asked


async def test_unaccepted_paid_model_asks_with_price_and_privacy(tmp_path):
    fake = FakeOpenRouter(replies=[(200, reply())])
    c = make_client(fake, tmp_path)
    elicit, asked = _asker("accept")
    assert await c.preflight(GROK, elicit) == Preflight(proceed=True)
    assert asked == [
        "Use `x-ai/grok-4.7` at $2.00 in / $6.00 out per 1M tokens? Some providers serving it "
        "may store or train on your prompts, and OpenRouter may route this call to one of them. "
        "With repository access on, the files it reads are sent to the provider serving it."
    ]
    assert json.loads((tmp_path / "config.json").read_text())["openrouter"]["accepted_models"] == [
        GROK
    ]
    assert await c.preflight(GROK, elicit) == Preflight(proceed=True)
    assert len(asked) == 1 and len(fake.posts) == 1  # not asked or probed again


async def test_models_accepted_in_config_skip_everything(tmp_path):
    fake = FakeOpenRouter()
    c = make_client(fake, tmp_path, accepted_models=[GROK])
    assert await c.preflight(GROK, None) == Preflight(proceed=True) and fake.posts == []


@pytest.mark.parametrize(
    "outcome,phrase",
    [("decline", "Not sent: you declined `x-ai/grok-4.7`"), ("cancel", "dismissed")],
)
async def test_decline_and_cancel_send_nothing(tmp_path, outcome, phrase):
    fake = FakeOpenRouter(replies=[(200, reply())])
    pre = await make_client(fake, tmp_path).preflight(GROK, _asker(outcome)[0])
    assert pre.proceed is False and phrase in pre.message and len(fake.posts) == 1  # only the probe
    assert not (tmp_path / "config.json").exists()
    if outcome == "cancel":
        assert "/sidekick:setup" in pre.message


async def test_unavailable_proceeds_with_a_one_time_notice(tmp_path):
    c = make_client(FakeOpenRouter(replies=[(200, reply())]), tmp_path)
    first, second = await c.preflight(GROK, None), await c.preflight(GROK, None)
    assert first.proceed and first.notice.startswith(
        "[sidekick notice] First use of `x-ai/grok-4.7` at $2.00 in / $6.00 out per 1M tokens."
    )
    assert "Sent with privacy-only routing" in first.notice
    assert "may route this call" not in first.notice  # the notice must not contradict itself
    assert second == Preflight(proceed=True) and not (tmp_path / "config.json").exists()


async def test_unattended_send_forces_deny_routing(tmp_path):  # owner 2026-11-11
    fake = FakeOpenRouter(replies=[(200, reply()), (200, reply())])
    c = make_client(fake, tmp_path)
    assert (await c.preflight(GROK, None)).proceed
    await c.ask(c.get_or_create_session("ask:s", GROK), "hi")
    assert len(fake.posts) == 2  # the privacy probe, then the chat request
    chat = fake.posts[1]
    assert chat["messages"][-1]["content"] == "hi"
    assert chat["provider"] == {"data_collection": "deny"}


@pytest.mark.parametrize(
    "probe", [(404, DATA_POLICY_404), (500, {"error": {"code": 500, "message": "x"}})]
)
async def test_unattended_refuses_when_providers_may_collect_or_unknown(tmp_path, probe):
    fake = FakeOpenRouter(replies=[probe])  # owner 2026-11-11
    pre = await make_client(fake, tmp_path).preflight(NEMO, None)
    assert pre.proceed is False and "couldn't ask you here" in pre.message and len(fake.posts) == 1


async def test_free_model_question_has_privacy_and_limits_but_no_price(tmp_path):
    elicit, asked = _asker("accept")
    await make_client(FakeOpenRouter(replies=[(404, DATA_POLICY_404)]), tmp_path).preflight(
        NEMO, elicit
    )
    assert asked == [
        f"Use the free model `{NEMO}`? Every provider serving it may store or train on your "
        "prompts, including any code sent with them. Free models allow 20 requests a minute and "
        "50 a day (1,000 a day once you have bought at least 10 credits). "
        "With repository access on, the files it reads are sent to the provider serving it."
    ]


async def test_block_switch_refuses_before_asking(tmp_path):
    elicit, asked = _asker("accept")
    pre = await make_client(
        FakeOpenRouter(replies=[(404, DATA_POLICY_404)]), tmp_path, block_data_collection=True
    ).preflight(NEMO, elicit)
    assert pre.proceed is False and asked == []
    assert f"Blocked by your setting `block_data_collection`: every provider serving `{NEMO}`" in (
        pre.message
    )


async def test_unknown_model_does_not_claim_a_price(tmp_path):  # Review Focus 5
    elicit, asked = _asker("decline")
    bad = (400, {"error": {"code": 400, "message": "acme/typo-model is not a valid model ID"}})
    await make_client(FakeOpenRouter(replies=[bad]), tmp_path).preflight("acme/typo-model", elicit)
    assert "price not shown in OpenRouter's catalog" in asked[0] and "$" not in asked[0]
    assert "https://openrouter.ai/acme/typo-model" in asked[0]


async def test_persist_failure_keeps_acceptance_in_memory(tmp_path):
    (tmp_path / "config.json").write_text("{bad")
    c = make_client(FakeOpenRouter(replies=[(200, reply())]), tmp_path)
    elicit, asked = _asker("accept")
    assert (await c.preflight(GROK, elicit)).proceed and (await c.preflight(GROK, elicit)).proceed
    assert len(asked) == 1 and (tmp_path / "config.json").read_text() == "{bad"


async def test_preflight_auth_error_propagates(tmp_path):
    fake = FakeOpenRouter(replies=[(401, {"error": {"code": 401, "message": "x"}})])
    with pytest.raises(OpenRouterError):
        await make_client(fake, tmp_path).preflight(GROK, None)


def _ctx(result=None, *, capable=True, raises=None):
    elicit = AsyncMock(return_value=result, side_effect=raises)
    session = SimpleNamespace(check_client_capability=lambda caps: capable)
    return SimpleNamespace(request_context=SimpleNamespace(session=session), elicit=elicit)


async def test_elicitor_outside_a_request_is_unavailable():
    assert await elicitor(Context())("q") == "unavailable"


async def test_elicitor_without_client_capability_is_unavailable():
    ctx = _ctx(capable=False)
    assert await elicitor(ctx)("q") == "unavailable" and ctx.elicit.await_count == 0  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "result,raises,want",
    [
        (AcceptedElicitation(data=_AcceptForm(accept=True)), None, "accept"),
        (AcceptedElicitation(data=_AcceptForm(accept=False)), None, "decline"),
        (DeclinedElicitation(), None, "decline"),
        (CancelledElicitation(), None, "cancel"),
        (None, RuntimeError("client went away"), "unavailable"),
    ],
)
async def test_elicitor_maps_actions(result, raises, want):
    assert await elicitor(_ctx(result, raises=raises))("q") == want  # type: ignore[arg-type]
