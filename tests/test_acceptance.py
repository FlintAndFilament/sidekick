import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from mcp.server.fastmcp import Context

from sidekick.backends import Preflight
from sidekick.openrouter import OpenRouterError
from sidekick.openrouter_catalog import REPO_ACCESS_SENTENCE, unattended_paid_refused_message
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
    assert await c.preflight(GROK, elicit, want_tools=True) == Preflight(proceed=True)
    assert asked == [
        "Use `x-ai/grok-4.7` at $2.00 in / $6.00 out per 1M tokens? Some providers serving it "
        "may store or train on your prompts, and OpenRouter may route this call to one of them. "
        "With repository access on, the files it reads are sent to the provider serving it."
    ]
    assert json.loads((tmp_path / "config.json").read_text())["openrouter"]["accepted_models"] == [
        GROK
    ]
    assert await c.preflight(GROK, elicit, want_tools=True) == Preflight(proceed=True)
    assert len(asked) == 1 and len(fake.posts) == 1  # not asked or probed again


async def test_models_accepted_in_config_skip_everything(tmp_path):
    fake = FakeOpenRouter()
    c = make_client(fake, tmp_path, accepted_models=[GROK])
    assert await c.preflight(GROK, None) == Preflight(proceed=True, tools=False)
    assert fake.posts == []


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


async def test_unattended_free_model_proceeds_with_a_one_time_notice(tmp_path):
    c = make_client(FakeOpenRouter(replies=[(200, reply())]), tmp_path)
    first, second = await c.preflight(NEMO, None), await c.preflight(NEMO, None)
    assert first.proceed and first.notice.startswith(
        f"[sidekick notice] First use of the free model `{NEMO}`."
    )
    assert "Sent with privacy-only routing" in first.notice
    assert "may route this call" not in first.notice  # the notice must not contradict itself
    assert second == Preflight(proceed=True, tools=False)
    assert not (tmp_path / "config.json").exists()


async def test_unattended_paid_model_is_refused_and_nothing_is_sent(tmp_path):  # #141
    fake = FakeOpenRouter(replies=[(200, reply()), (200, reply())])
    c = make_client(fake, tmp_path)
    pre = await c.preflight(GROK, None, want_tools=True)
    assert pre == Preflight(proceed=False, message=unattended_paid_refused_message(GROK))
    assert pre.message == (
        "[sidekick] Not sent: `x-ai/grok-4.7` is a paid model and sidekick couldn't ask you to "
        "accept its price here. Run /sidekick:setup, or add it to openrouter.accepted_models in "
        "~/.config/sidekick/config.json."
    )
    assert fake.posts == []  # not even the billed privacy probe
    assert not (await c.preflight(GROK, None)).proceed  # still refused next time
    assert fake.posts == [] and not (tmp_path / "config.json").exists()


async def test_paid_model_refused_when_the_question_is_unavailable(tmp_path):
    fake = FakeOpenRouter(replies=[(200, reply()), (200, reply())])
    c = make_client(fake, tmp_path)
    elicit, asked = _asker("unavailable")
    pre = await c.preflight(GROK, elicit, want_tools=True)
    assert pre == Preflight(proceed=False, message=unattended_paid_refused_message(GROK))
    assert len(asked) == 1  # the question was tried, so the probe ran for its text
    assert len(fake.posts) == 1 and fake.posts[0]["max_tokens"] == 16  # no chat request
    assert not (tmp_path / "config.json").exists()


async def test_unattended_free_send_forces_deny_routing_without_file_tools(tmp_path):
    fake = FakeOpenRouter(replies=[(200, reply()), (200, reply())])  # owner 2026-11-11
    c = make_client(fake, tmp_path)
    pre = await c.preflight(NEMO, None, want_tools=True)
    assert pre.proceed and pre.tools is False
    assert REPO_ACCESS_SENTENCE not in pre.notice  # no file tools, so no repository claim
    await c.ask(c.get_or_create_session("ask:s", NEMO), "hi")
    assert len(fake.posts) == 2  # the privacy probe, then the chat request
    chat = fake.posts[1]
    assert chat["messages"][-1]["content"] == "hi"
    assert chat["provider"] == {"data_collection": "deny"}


async def test_first_use_accept_with_tools_records_both_lists_in_one_question(tmp_path):
    fake = FakeOpenRouter(replies=[(200, reply())])
    c = make_client(fake, tmp_path)
    elicit, asked = _asker("accept")
    assert await c.preflight(GROK, elicit, want_tools=True) == Preflight(proceed=True, tools=True)
    assert len(asked) == 1 and REPO_ACCESS_SENTENCE in asked[0]
    section = json.loads((tmp_path / "config.json").read_text())["openrouter"]
    assert section["accepted_models"] == [GROK] and section["repo_access_models"] == [GROK]
    assert await c.preflight(GROK, elicit, want_tools=True) == Preflight(proceed=True, tools=True)
    assert len(asked) == 1


async def test_first_use_without_tools_does_not_ask_about_the_repository(tmp_path):
    c = make_client(FakeOpenRouter(replies=[(200, reply())]), tmp_path)
    elicit, asked = _asker("accept")
    assert await c.preflight(GROK, elicit) == Preflight(proceed=True, tools=False)
    assert REPO_ACCESS_SENTENCE not in asked[0]
    section = json.loads((tmp_path / "config.json").read_text())["openrouter"]
    assert section == {"accepted_models": [GROK]}


async def test_accepted_model_gets_a_repository_question_and_accept_is_remembered(tmp_path):
    fake = FakeOpenRouter(replies=[(200, reply())])
    c = make_client(fake, tmp_path, accepted_models=[GROK])
    elicit, asked = _asker("accept")
    assert await c.preflight(GROK, elicit, want_tools=True) == Preflight(proceed=True, tools=True)
    assert asked == [
        "Let `x-ai/grok-4.7` at $2.00 in / $6.00 out per 1M tokens read files in this "
        "repository? Files it reads are sent to the provider serving it. Some providers serving "
        "it may store or train on your prompts, and OpenRouter may route this call to one of them."
    ]
    section = json.loads((tmp_path / "config.json").read_text())["openrouter"]
    assert section == {"repo_access_models": [GROK]}
    assert await c.preflight(GROK, elicit, want_tools=True) == Preflight(proceed=True, tools=True)
    assert len(asked) == 1
    fresh = make_client(
        FakeOpenRouter(), tmp_path, accepted_models=[GROK], repo_access_models=[GROK]
    )
    assert await fresh.preflight(GROK, None, want_tools=True) == Preflight(proceed=True, tools=True)


@pytest.mark.parametrize("outcome", ["decline", "cancel"])
async def test_repository_decline_runs_without_tools_and_is_not_asked_again(tmp_path, outcome):
    c = make_client(FakeOpenRouter(replies=[(200, reply())]), tmp_path, accepted_models=[GROK])
    elicit, asked = _asker(outcome)
    want = Preflight(proceed=True, tools=False)
    assert await c.preflight(GROK, elicit, want_tools=True) == want
    assert await c.preflight(GROK, elicit, want_tools=True) == want
    assert len(asked) == 1 and not (tmp_path / "config.json").exists()


async def test_repository_question_unavailable_runs_without_tools(tmp_path):
    c = make_client(FakeOpenRouter(replies=[(200, reply())]), tmp_path, accepted_models=[GROK])
    elicit, asked = _asker("unavailable")
    want = Preflight(proceed=True, tools=False)
    assert await c.preflight(GROK, elicit, want_tools=True) == want
    assert await c.preflight(GROK, None, want_tools=True) == want
    assert not (tmp_path / "config.json").exists()


async def test_repository_access_alone_does_not_count_as_acceptance(tmp_path):
    fake = FakeOpenRouter(replies=[(200, reply())])
    c = make_client(fake, tmp_path, repo_access_models=[GROK])
    assert not (await c.preflight(GROK, None, want_tools=True)).proceed


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
        NEMO, elicit, want_tools=True
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
        await make_client(fake, tmp_path).preflight(NEMO, None)  # free: the probe still runs


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
