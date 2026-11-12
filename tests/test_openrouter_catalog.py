from decimal import Decimal

import pytest

from sidekick import openrouter_catalog as oc
from tests._openrouter_fixtures import (
    AUTO,
    BATCH,
    GEMINI_FLASH,
    GEMINI_LITE,
    GEMMA,
    GROK,
    IMAGE,
    INKLING,
    KIMI,
    NEMOTRON,
    OR_FREE,
    entry,
)

P = oc.parse_model
PINNED = {
    "flash": "gemini-3.5-flash",
    "flash-lite": "gemini-3.1-flash-lite",
    "pro": "gemini-3.1-pro-preview",
}


def test_prices_are_decimal_per_token():
    assert P(GROK).prompt_price == Decimal("0.000002")
    assert P(GROK).completion_price == Decimal("0.000006")


def test_negative_price_is_unknown():
    assert P(AUTO).prompt_price is None
    assert oc.format_price(None) == "varies"


def test_free_is_the_suffix_not_the_price():
    assert P(NEMOTRON).free is True
    assert P(OR_FREE).free is False
    assert oc.is_free("a/b:free")


@pytest.mark.parametrize(
    "raw,shown",
    [
        ("0.000002", "$2.00"),
        ("0.000000098", "$0.10"),
        ("0.000000294", "$0.29"),
        ("0.000000825", "$0.83"),
        ("0.000004125", "$4.13"),
        ("0", "$0"),
        ("0.000000000001", "<$0.01"),
    ],
)
def test_format_price_per_million(raw, shown):
    assert oc.format_price(Decimal(raw)) == shown  # ROUND_HALF_UP


def test_price_pair():
    assert oc.price_pair(P(GROK)) == "$2.00 in / $6.00 out per 1M tokens"
    assert oc.price_pair(P(NEMOTRON)) == "free"
    assert oc.price_pair(P(AUTO)) == "price varies, see https://openrouter.ai/openrouter/auto"


def test_text_chat_filter():
    assert P(GROK).text_chat
    assert P(OR_FREE).text_chat
    assert not P(AUTO).text_chat
    assert not P(BATCH).text_chat
    assert not P(IMAGE).text_chat


@pytest.mark.parametrize(
    "n,shown", [(500_000, "500k"), (1_048_576, "1M"), (1_000_000, "1M"), (None, "?")]
)
def test_format_context(n, shown):
    assert oc.format_context(n) == shown


def test_bad_entries_are_skipped_not_raised():
    got = oc.parse_catalog({"data": [GROK, {"name": "no id"}, {"id": "a/b", "pricing": "weird"}]})
    assert [m.id for m in got] == ["x-ai/grok-4.7", "a/b"]
    assert got[1].prompt_price is None


def test_payload_without_data_raises_value_error():
    with pytest.raises(ValueError):
        oc.parse_catalog({"models": []})


def test_free_candidates_preferences_first_then_context():
    extra_small = entry("acme/small:free", "0", "0", ctx=32_000)
    extra_big = entry("acme/big:free", "0", "0", ctx=128_001)
    cat = oc.parse_catalog(
        {"data": [extra_small, GEMMA, extra_big, NEMOTRON, INKLING, OR_FREE, GROK]}
    )
    assert oc.free_candidates(cat) == [
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "google/gemma-4-31b-it:free",
        "acme/big:free",
        "thinkingmachines/inkling-2-small:free",
        "acme/small:free",
    ]
    assert oc.free_candidates([]) == []


@pytest.mark.parametrize(
    "level,raw,want",
    [
        ("low", GROK, "low"),
        ("none", GROK, "low"),  # mandatory: never "none"
        ("medium", KIMI, "high"),  # nearest higher supported
        ("none", KIMI, "none"),
        ("high", entry("a/b", efforts=("low", "medium")), "medium"),  # none higher: the highest
        ("high", GEMMA, None),  # no reasoning parameter: omit
        ("none", None, None),
        ("medium", None, "medium"),  # model not in catalog
    ],
)
def test_map_effort(level, raw, want):
    assert oc.map_effort(level, P(raw) if raw else None) == want


def test_none_on_optional_reasoning_omits_field():
    optional = entry("a/b", efforts=("high", "medium"), mandatory=False)
    assert oc.map_effort("none", P(optional)) is None


@pytest.mark.parametrize(
    "level,efforts,want",
    [
        ("none", ("weird", "low"), "low"),
        ("none", ("weird",), None),
        ("none", (), None),
        ("medium", (), "medium"),
    ],
)
def test_map_effort_unknown_or_empty_efforts(level, efforts, want):
    mandatory = entry("a/b", efforts=efforts, mandatory=True)
    assert oc.map_effort(level, P(mandatory)) == want


def test_resolve_gemini_alias():
    cat = [P(GEMINI_FLASH), P(GEMINI_LITE), P(BATCH)]
    assert oc.resolve_gemini_alias("flash", cat, PINNED) == "google/gemini-3.8-flash"
    assert oc.resolve_gemini_alias("gemini-flash-latest", cat, PINNED) == "google/gemini-3.8-flash"
    assert oc.resolve_gemini_alias("flash-lite", cat, PINNED) == "google/gemini-3.5-flash-lite"
    assert oc.resolve_gemini_alias("pro", cat, PINNED) == "google/gemini-3.1-pro-preview"  # pinned
    assert oc.resolve_gemini_alias("gemini-3.5-flash", cat, PINNED) == "google/gemini-3.5-flash"


def test_first_use_notice_forced_deny_suffix_only_when_forced():
    from sidekick.openrouter_catalog import PrivacyStatus, first_use_notice

    args = ("x-ai/grok-4.7", None, PrivacyStatus.SOME_MAY_COLLECT)
    assert "privacy-only routing" not in first_use_notice(*args, block=False)
    assert "privacy-only routing" not in first_use_notice(*args, block=False, forced_deny=False)
    forced = first_use_notice(*args, block=False, forced_deny=True)
    assert "privacy-only routing" in forced and "may route this call" not in forced


def test_privacy_sentence_with_block_does_not_say_it_may_route_to_collectors():
    from sidekick.openrouter_catalog import PrivacyStatus, privacy_sentence

    on = privacy_sentence("x-ai/grok-4.7", PrivacyStatus.SOME_MAY_COLLECT, block=True)
    assert "may route" not in on and "block_data_collection" in on
    off = privacy_sentence("x-ai/grok-4.7", PrivacyStatus.SOME_MAY_COLLECT, block=False)
    assert "may route this call" in off
