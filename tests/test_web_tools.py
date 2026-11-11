"""Tests for sidekick/web_tools.py — the web capability and its exclusion rule (#76).

The load-bearing property is D4: web access and write_file never coexist in one call.
A web page is attacker-controlled text; once it is in a call that can write to the repo,
"write this to setup.sh" becomes a plausible instruction. These tests pin that invariant
at the policy function, at the request the API actually receives, and in the advertised
metadata, so it cannot be lost in any one of the three.
"""

import itertools
from pathlib import Path
from unittest.mock import patch

import pytest
from google.genai import types

from sidekick.config import Config
from sidekick.web_tools import resolve_capabilities, web_tool_set


class TestResolveCapabilities:
    """The single function that owns D4."""

    @pytest.mark.parametrize(
        "write,requested,default",
        list(itertools.product([True, False], [True, False, None], [True, False])),
    )
    def test_web_and_write_are_never_both_on(
        self, write: bool, requested: bool, default: bool
    ) -> None:
        """Exhaustive over the whole input space — this is the security invariant."""
        caps = resolve_capabilities(write=write, requested=requested, default=default)
        assert not (caps.web and caps.write)

    def test_none_follows_the_config_default(self) -> None:
        assert resolve_capabilities(write=False, requested=None, default=True).web is True
        assert resolve_capabilities(write=False, requested=None, default=False).web is False

    def test_explicit_true_overrides_a_false_default(self) -> None:
        assert resolve_capabilities(write=False, requested=True, default=False).web is True

    def test_explicit_false_overrides_a_true_default(self) -> None:
        assert resolve_capabilities(write=False, requested=False, default=True).web is False

    def test_web_suppresses_write_not_the_other_way_round(self) -> None:
        """A tool asking for write does not silently lose web; web wins and write drops,
        because the caller opted into web explicitly for this call."""
        caps = resolve_capabilities(write=True, requested=True, default=False)
        assert caps.web is True and caps.write is False

    def test_write_survives_when_web_is_off(self) -> None:
        caps = resolve_capabilities(write=True, requested=False, default=False)
        assert caps.web is False and caps.write is True

    def test_read_only_tool_is_unaffected_by_the_rule(self) -> None:
        caps = resolve_capabilities(write=False, requested=True, default=False)
        assert caps.web is True and caps.write is False

    def test_default_on_still_drops_write_for_a_write_capable_tool(self) -> None:
        """With the config default on, a write tool that says nothing gets web, not write."""
        caps = resolve_capabilities(write=True, requested=None, default=True)
        assert caps.web is True and caps.write is False


class TestWebToolSet:
    """What gets attached to the request."""

    def test_contains_both_builtin_tools(self) -> None:
        tools = web_tool_set()
        assert any(t.google_search is not None for t in tools)
        assert any(t.url_context is not None for t in tools)

    def test_declares_no_function_declarations(self) -> None:
        """These are server-side; mixing our declarations in here would double-declare them."""
        assert all(not t.function_declarations for t in web_tool_set())

    def test_is_empty_of_other_builtins(self) -> None:
        """Out of scope per the spec — one trust decision at a time."""
        for tool in web_tool_set():
            assert tool.code_execution is None
            assert tool.mcp_servers is None
            assert tool.exa_ai_search is None


class TestConfig:
    def test_web_tools_defaults_to_disabled(self) -> None:
        """Grounding bills per request, so it must never be on by accident."""
        assert Config(auth={"method": "api_key"}).web_tools.enabled is False

    def test_web_tools_can_be_enabled(self) -> None:
        cfg = Config(auth={"method": "api_key"}, web_tools={"enabled": True})  # type: ignore[arg-type]
        assert cfg.web_tools.enabled is True


class TestBuildConfigAttachesWebTools:
    """The request the SDK actually receives."""

    def _client(self) -> object:
        from sidekick.client import GeminiClient

        with patch("google.genai.Client"):
            return GeminiClient(Config(auth={"method": "api_key"}), api_key="k")

    def _decl(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name="read_file",
            description="d",
            parameters_json_schema={"type": "object", "properties": {}},
        )

    def test_web_off_is_unchanged_from_today(self) -> None:
        """Regression: nothing about an ordinary call may shift while the flag is off."""
        cfg = self._client().build_config(  # type: ignore[attr-defined]
            thinking="medium", declarations=[self._decl()], web=False
        )
        assert len(cfg.tools) == 1
        assert cfg.tools[0].function_declarations
        assert cfg.tool_config is None or not getattr(
            cfg.tool_config, "include_server_side_tool_invocations", False
        )

    def test_web_on_attaches_both_builtins_alongside_declarations(self) -> None:
        cfg = self._client().build_config(  # type: ignore[attr-defined]
            thinking="medium", declarations=[self._decl()], web=True
        )
        assert any(t.google_search is not None for t in cfg.tools)
        assert any(t.url_context is not None for t in cfg.tools)
        assert any(t.function_declarations for t in cfg.tools)

    def test_web_on_sets_the_flag_the_api_demands(self) -> None:
        """Without this the API returns 400 when built-ins meet function declarations."""
        cfg = self._client().build_config(  # type: ignore[attr-defined]
            thinking="medium", declarations=[self._decl()], web=True
        )
        assert cfg.tool_config.include_server_side_tool_invocations is True

    def test_web_on_without_declarations_still_works(self) -> None:
        """File tools can be off (kill switch) while web is on."""
        cfg = self._client().build_config(thinking="medium", web=True)  # type: ignore[attr-defined]
        assert any(t.google_search is not None for t in cfg.tools)
        assert cfg.tool_config.include_server_side_tool_invocations is True

    def test_afc_stays_disabled(self) -> None:
        """The bridge runs its own loop; the SDK's must stay off (#68)."""
        cfg = self._client().build_config(  # type: ignore[attr-defined]
            thinking="medium", declarations=[self._decl()], web=True
        )
        assert cfg.automatic_function_calling.disable is True


class TestExclusionReachesTheRequest:
    """End to end: a web-enabled call must not declare write_file to the API."""

    ARGS = {
        "brainstorm": {"topic": "t"},
        "review": {"content": "c"},
        "architect": {"description": "d"},
    }

    async def _declared(self, tmp_path: Path, tool: str, **extra: object) -> set[str]:
        from datetime import datetime
        from unittest.mock import AsyncMock

        from sidekick.client import GeminiClient
        from sidekick.server import build_server
        from sidekick.transcript import TranscriptWriter
        from sidekick.workspace import build_workspace
        from tests.test_tools import _text_response

        cfg = Config(auth={"method": "api_key"})
        with patch("google.genai.Client"):
            client = GeminiClient(cfg, api_key="k")
        gen = AsyncMock(return_value=_text_response("ok"))
        client._raw_client.aio.models.generate_content = gen
        mcp = build_server(
            client,
            TranscriptWriter(str(tmp_path / "t"), datetime.now()),
            build_workspace(cfg, tmp_path),
        )
        await mcp.call_tool(tool, {**self.ARGS[tool], **extra})
        request = gen.call_args.kwargs["config"]
        names: set[str] = set()
        for tool_obj in request.tools or []:
            for decl in tool_obj.function_declarations or []:
                names.add(decl.name)
        return names

    @pytest.mark.parametrize("tool", sorted(ARGS))
    async def test_write_file_is_withheld_when_web_is_on(self, tmp_path: Path, tool: str) -> None:
        assert "write_file" not in await self._declared(tmp_path, tool, web=True)

    @pytest.mark.parametrize("tool", sorted(ARGS))
    async def test_write_file_is_present_when_web_is_off(self, tmp_path: Path, tool: str) -> None:
        assert "write_file" in await self._declared(tmp_path, tool, web=False)

    @pytest.mark.parametrize("tool", sorted(ARGS))
    async def test_read_tools_survive_web(self, tmp_path: Path, tool: str) -> None:
        """Web access must not cost Gemini its ability to read the repo."""
        assert "read_file" in await self._declared(tmp_path, tool, web=True)


class TestAdvertisedWebRow:
    """#74's rule applies to this capability too: the text must match the behaviour."""

    def _described(self, tmp_path: Path, tool: str, *, enabled: bool, ws_enabled: bool = True):
        import asyncio
        from datetime import datetime

        from sidekick.client import GeminiClient
        from sidekick.server import build_server
        from sidekick.transcript import TranscriptWriter
        from sidekick.workspace import build_workspace

        cfg = Config(
            auth={"method": "api_key"},  # type: ignore[arg-type]
            web_tools={"enabled": enabled},  # type: ignore[arg-type]
            file_tools={"enabled": ws_enabled},  # type: ignore[arg-type]
        )
        with patch("google.genai.Client"):
            client = GeminiClient(cfg, api_key="k")
        mcp = build_server(
            client,
            TranscriptWriter(str(tmp_path / "t"), datetime.now()),
            build_workspace(cfg, tmp_path),
        )
        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        return tools[tool].description or ""

    @pytest.mark.parametrize("tool", ["ask", "debug", "review", "architect"])
    def test_every_tool_advertises_the_web_row(self, tmp_path: Path, tool: str) -> None:
        assert "Web access:" in self._described(tmp_path, tool, enabled=False)

    def test_default_state_is_stated(self, tmp_path: Path) -> None:
        assert "OFF by default" in self._described(tmp_path, "ask", enabled=False)
        assert "ON by default" in self._described(tmp_path, "ask", enabled=True)

    def test_write_capable_tools_advertise_the_exclusion(self, tmp_path: Path) -> None:
        described = self._described(tmp_path, "review", enabled=False)
        assert "write_file is withheld" in described

    def test_read_only_tools_do_not_claim_a_withheld_write(self, tmp_path: Path) -> None:
        """ask never had write_file; saying it is withheld would mislead."""
        assert "withheld" not in self._described(tmp_path, "ask", enabled=False)

    def test_no_withheld_claim_when_file_tools_are_off(self, tmp_path: Path) -> None:
        """With the kill switch on, write_file does not exist to withhold."""
        described = self._described(tmp_path, "review", enabled=False, ws_enabled=False)
        assert "Web access:" in described
        assert "withheld" not in described

    def test_untrusted_content_is_flagged_to_the_caller(self, tmp_path: Path) -> None:
        assert "untrusted" in self._described(tmp_path, "ask", enabled=False)


class TestGroundingIsRecorded:
    """Server-side calls never reach ToolRegistry, so they need their own audit trail."""

    def _candidate(self, queries: list[str], uris: list[str]) -> types.Candidate:
        chunks = [
            types.GroundingChunk(web=types.GroundingChunkWeb(uri=u, title=u.split("//")[-1]))
            for u in uris
        ]
        return types.Candidate(
            content=types.Content(role="model", parts=[types.Part.from_text(text="a")]),
            grounding_metadata=types.GroundingMetadata(
                web_search_queries=queries, grounding_chunks=chunks
            ),
        )

    def test_queries_and_sources_are_recorded(self) -> None:
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        records: list[ToolCallRecord] = []
        record_grounding(self._candidate(["python version"], ["https://python.org"]), records)
        assert len(records) == 1
        assert records[0].name == "google_search"
        assert records[0].args == {"query": "python version"}
        assert "python.org" in records[0].summary

    def test_each_query_gets_its_own_record(self) -> None:
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        records: list[ToolCallRecord] = []
        record_grounding(self._candidate(["a", "b"], ["https://x.test"]), records)
        assert [r.args["query"] for r in records] == ["a", "b"]

    def _fetch_candidate(self, urls: list[tuple[str, str]]) -> types.Candidate:
        return types.Candidate(
            content=types.Content(role="model", parts=[types.Part.from_text(text="a")]),
            url_context_metadata=types.UrlContextMetadata(
                url_metadata=[
                    types.UrlMetadata(retrieved_url=u, url_retrieval_status=st) for u, st in urls
                ]
            ),
        )

    def test_url_fetch_is_recorded_from_its_own_metadata(self) -> None:
        """A pure url_context fetch produces no grounding chunks; it must still be logged,
        or the transcript never shows which URL entered the context."""
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        records: list[ToolCallRecord] = []
        record_grounding(
            self._fetch_candidate([("https://docs.test/page", "URL_RETRIEVAL_STATUS_SUCCESS")]),
            records,
        )
        assert len(records) == 1
        assert records[0].name == "url_context"
        assert records[0].args == {"url": "https://docs.test/page"}
        assert records[0].ok is True

    def test_failed_retrieval_is_recorded_as_a_failure(self) -> None:
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        records: list[ToolCallRecord] = []
        record_grounding(
            self._fetch_candidate([("https://blocked.test", "URL_RETRIEVAL_STATUS_ERROR")]),
            records,
        )
        assert records[0].ok is False
        assert "not retrieved" in records[0].summary

    def test_search_and_fetch_are_recorded_separately(self) -> None:
        """A call that both searches and fetches must not attribute the fetch to the search."""
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        candidate = self._candidate(["q"], ["https://x.test"])
        candidate.url_context_metadata = types.UrlContextMetadata(
            url_metadata=[
                types.UrlMetadata(
                    retrieved_url="https://named.test/page",
                    url_retrieval_status="URL_RETRIEVAL_STATUS_SUCCESS",
                )
            ]
        )
        records: list[ToolCallRecord] = []
        record_grounding(candidate, records)
        by_name = {r.name for r in records}
        assert by_name == {"google_search", "url_context"}
        fetch = next(r for r in records if r.name == "url_context")
        assert fetch.args["url"] == "https://named.test/page"

    def test_no_grounding_means_no_records(self) -> None:
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        records: list[ToolCallRecord] = []
        record_grounding(types.Candidate(content=types.Content(role="model", parts=[])), records)
        assert records == []

    def test_readable_source_name_preferred_over_redirect_uri(self) -> None:
        """Live grounding returns an opaque vertexaisearch redirect as uri; title holds the
        site, which is the part an auditor can actually use."""
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        chunk = types.GroundingChunk(
            web=types.GroundingChunkWeb(
                uri="https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIabc",
                title="python.org",
            )
        )
        candidate = types.Candidate(
            content=types.Content(role="model", parts=[types.Part.from_text(text="a")]),
            grounding_metadata=types.GroundingMetadata(
                web_search_queries=["q"], grounding_chunks=[chunk]
            ),
        )
        records: list[ToolCallRecord] = []
        record_grounding(candidate, records)
        assert "python.org" in records[0].summary
        assert "vertexaisearch" not in records[0].summary

    def test_titleless_source_is_recorded_with_no_name(self) -> None:
        """Without a title the name must stay empty so the footer can name the source after
        its resolved URL; the redirect link is not a name (#90)."""
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIabc"
        candidate = types.Candidate(
            content=types.Content(role="model", parts=[types.Part.from_text(text="a")]),
            grounding_metadata=types.GroundingMetadata(
                web_search_queries=["q"],
                grounding_chunks=[types.GroundingChunk(web=types.GroundingChunkWeb(uri=redirect))],
            ),
        )
        records: list[ToolCallRecord] = []
        record_grounding(candidate, records)
        assert records[0].sources == (("", redirect),)
        assert "vertexaisearch" not in records[0].summary
        # The count is of sources, not of names: an untitled source is still a source.
        assert records[0].summary.startswith("1 source(s)")

    def test_untitled_sources_are_counted_beside_named_ones(self) -> None:
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        chunks = [
            types.GroundingChunk(web=types.GroundingChunkWeb(uri="https://x.test", title="x.test")),
            types.GroundingChunk(web=types.GroundingChunkWeb(uri="https://y.test")),
            types.GroundingChunk(web=types.GroundingChunkWeb(uri="https://z.test")),
        ]
        candidate = types.Candidate(
            content=types.Content(role="model", parts=[types.Part.from_text(text="a")]),
            grounding_metadata=types.GroundingMetadata(
                web_search_queries=["q"], grounding_chunks=chunks
            ),
        )
        records: list[ToolCallRecord] = []
        record_grounding(candidate, records)
        assert records[0].summary == "3 source(s): x.test"

    def test_duplicate_sources_are_collapsed(self) -> None:
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        records: list[ToolCallRecord] = []
        record_grounding(self._candidate(["q"], ["https://x.test", "https://x.test"]), records)
        assert records[0].summary.startswith("1 source(s)")

    def test_records_render_into_the_transcript_format(self) -> None:
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        records: list[ToolCallRecord] = []
        record_grounding(self._candidate(["q"], ["https://x.test"]), records)
        assert records[0].render().startswith("→ google_search(query=")


class TestUntrustedContentDoesNotPersist:
    """#76 D4 holds per call; session history could carry web content past it (review finding).

    The API returns server-side tool_call/tool_response parts inside the same content object
    as the answer. Committing that object to history would replay retrieved page text into
    the next call in the session — which may be write-capable, since that call's own `web`
    is false. Two calls, each individually honouring D4, would together defeat it.
    """

    def _turn_with_web_traffic(self) -> types.Candidate:
        return types.Candidate(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(tool_call=types.ToolCall(args={"query": "q"})),
                    types.Part(
                        tool_response=types.ToolResponse(response={"page": "untrusted text"})
                    ),
                    types.Part.from_text(text="the answer"),
                ],
            )
        )

    def test_history_content_keeps_only_answer_parts(self) -> None:
        from sidekick.tool_loop import _answer

        result = _answer(self._turn_with_web_traffic())
        assert result.text == "the answer"
        for part in result.content.parts or []:
            assert part.tool_call is None
            assert part.tool_response is None

    def test_answer_text_is_unaffected_by_the_stripping(self) -> None:
        from sidekick.tool_loop import _answer

        assert _answer(self._turn_with_web_traffic()).text == "the answer"

    def test_ordinary_turns_are_unchanged(self) -> None:
        from sidekick.tool_loop import _answer

        candidate = types.Candidate(
            content=types.Content(role="model", parts=[types.Part.from_text(text="plain")])
        )
        result = _answer(candidate)
        assert len(result.content.parts or []) == 1
        assert result.text == "plain"


class TestVertexCannotCarryTheFlag:
    """include_server_side_tool_invocations is Developer-API only; the SDK raises on Vertex."""

    def _vertex_client(self):
        from unittest.mock import MagicMock

        from sidekick.client import GeminiClient

        with patch("google.genai.Client"):
            return GeminiClient(Config(project="p", auth={"method": "adc"}), MagicMock())

    def test_web_is_reported_unsupported(self) -> None:
        assert self._vertex_client().web_supported is False

    def test_developer_api_supports_web(self) -> None:
        from sidekick.client import GeminiClient

        with patch("google.genai.Client"):
            client = GeminiClient(Config(auth={"method": "api_key"}), api_key="k")
        assert client.web_supported is True

    def test_no_flag_and_no_web_tools_on_vertex(self) -> None:
        """Attaching them would make the SDK raise before the request leaves the process."""
        cfg = self._vertex_client().build_config(thinking="medium", web=True)
        assert cfg.tool_config is None or not getattr(
            cfg.tool_config, "include_server_side_tool_invocations", None
        )
        for tool in cfg.tools or []:
            assert tool.google_search is None and tool.url_context is None

    def test_the_sdk_really_rejects_the_flag_on_vertex(self) -> None:
        """Pins the upstream behaviour this gate exists for, so an SDK change surfaces here."""
        from google.genai import models as genai_models
        from google.genai import types as gt

        payload = gt.ToolConfig(include_server_side_tool_invocations=True).model_dump(
            exclude_none=True
        )
        with pytest.raises(ValueError, match="only supported in"):
            genai_models._ToolConfig_to_vertex(payload)


REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIabc"


def _grounded(
    queries: list[str], chunks: list[tuple[str, str]], text: str = "the answer"
) -> types.Candidate:
    """A final answer turn that searched: (title, uri) per grounding chunk."""
    return types.Candidate(
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
        finish_reason=types.FinishReason.STOP,
        grounding_metadata=types.GroundingMetadata(
            web_search_queries=queries,
            grounding_chunks=[
                types.GroundingChunk(web=types.GroundingChunkWeb(uri=u, title=t)) for t, u in chunks
            ],
        ),
    )


class TestSourcesReachTheCaller:
    """#80: the caller gets title + link for every source a web answer drew on."""

    def test_search_record_carries_title_and_uri(self) -> None:
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        records: list[ToolCallRecord] = []
        record_grounding(_grounded(["q"], [("python.org", REDIRECT)]), records)
        assert records[0].sources == (("python.org", REDIRECT),)

    def test_sources_attach_once_not_per_query(self) -> None:
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        records: list[ToolCallRecord] = []
        record_grounding(_grounded(["a", "b"], [("x.test", "https://x.test")]), records)
        assert sum(len(r.sources) for r in records) == 1

    def test_sources_without_a_reported_query_are_kept(self) -> None:
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        records: list[ToolCallRecord] = []
        record_grounding(_grounded([], [("x.test", "https://x.test")]), records)
        assert [s for r in records for s in r.sources] == [("x.test", "https://x.test")]

    def test_retrieved_url_is_a_source_and_a_failed_one_is_not(self) -> None:
        from sidekick.tool_loop import ToolCallRecord, record_grounding

        candidate = types.Candidate(
            content=types.Content(role="model", parts=[types.Part.from_text(text="a")]),
            url_context_metadata=types.UrlContextMetadata(
                url_metadata=[
                    types.UrlMetadata(
                        retrieved_url="https://ok.test",
                        url_retrieval_status="URL_RETRIEVAL_STATUS_SUCCESS",
                    ),
                    types.UrlMetadata(
                        retrieved_url="https://bad.test",
                        url_retrieval_status="URL_RETRIEVAL_STATUS_ERROR",
                    ),
                ]
            ),
        )
        records: list[ToolCallRecord] = []
        record_grounding(candidate, records)
        sources = [s for r in records for s in r.sources]
        assert sources == [("https://ok.test", "https://ok.test")]

    def test_footer_is_empty_without_sources(self) -> None:
        from sidekick.sources import sources_footer
        from sidekick.tool_loop import ToolCallRecord

        assert sources_footer([ToolCallRecord("read_file", {}, True, "1 B")]) == ""

    def test_footer_lists_title_and_link_once_each(self) -> None:
        from sidekick.sources import sources_footer
        from sidekick.tool_loop import ToolCallRecord

        rec = ToolCallRecord("google_search", {"query": "q"}, True, "", (("python.org", REDIRECT),))
        text = sources_footer([rec, rec])
        assert text.count(REDIRECT) == 1
        assert "python.org" in text
        assert "unresolved Google redirect" in text  # unresolved: the caller must know

    def test_footer_is_capped(self) -> None:
        from sidekick.sources import MAX_SOURCES, sources_footer
        from sidekick.tool_loop import ToolCallRecord

        many = tuple((f"s{i}", f"https://s{i}.test") for i in range(MAX_SOURCES + 3))
        text = sources_footer([ToolCallRecord("google_search", {}, True, "", many)])
        assert f"https://s{MAX_SOURCES - 1}.test" in text
        assert f"https://s{MAX_SOURCES}.test" not in text
        assert "3 more" in text

    async def test_web_answer_ends_with_sources_and_transcript_logs_them(
        self, tmp_path: Path
    ) -> None:
        from datetime import datetime
        from unittest.mock import AsyncMock

        from sidekick.client import GeminiClient
        from sidekick.server import build_server
        from sidekick.transcript import TranscriptWriter
        from sidekick.workspace import build_workspace

        cfg = Config(auth={"method": "api_key"})
        with patch("google.genai.Client"):
            client = GeminiClient(cfg, api_key="k")
        client._raw_client.aio.models.generate_content = AsyncMock(
            return_value=types.GenerateContentResponse(
                candidates=[_grounded(["q"], [("python.org", REDIRECT)])]
            )
        )
        transcript = TranscriptWriter(str(tmp_path / "t"), datetime.now())
        mcp = build_server(client, transcript, build_workspace(cfg, tmp_path))
        real = "https://www.python.org/downloads/"
        resolver = AsyncMock(return_value={REDIRECT: real})
        with patch("sidekick.tools.base.resolve_redirects", resolver):
            result = await mcp.call_tool("ask", {"prompt": "p", "web": True})
        blocks = result[0] if isinstance(result, tuple) else result
        text = "".join(getattr(b, "text", "") for b in blocks)  # type: ignore[union-attr]
        assert text.startswith("the answer")
        assert real in text and "python.org" in text
        assert REDIRECT not in text
        assert real in transcript.path.read_text()

    async def test_links_typed_in_the_answer_are_flagged(self, tmp_path: Path) -> None:
        """Gemini is told not to write URLs, but an explicit user request overrides that, and
        the ones it types can be fabricated. The reply says which list is authoritative (#92)."""
        from datetime import datetime
        from unittest.mock import AsyncMock

        from sidekick.client import GeminiClient
        from sidekick.server import build_server
        from sidekick.sources import TYPED_URL_CAUTION
        from sidekick.transcript import TranscriptWriter
        from sidekick.workspace import build_workspace

        cfg = Config(auth={"method": "api_key"})
        with patch("google.genai.Client"):
            client = GeminiClient(cfg, api_key="k")
        client._raw_client.aio.models.generate_content = AsyncMock(
            return_value=types.GenerateContentResponse(
                candidates=[
                    _grounded(
                        ["q"],
                        [("python.org", REDIRECT)],
                        text="See https://www.python.org/invented/path for details.",
                    )
                ]
            )
        )
        transcript = TranscriptWriter(str(tmp_path / "t"), datetime.now())
        mcp = build_server(client, transcript, build_workspace(cfg, tmp_path))
        resolver = AsyncMock(return_value={REDIRECT: "https://www.python.org/downloads/"})
        with patch("sidekick.tools.base.resolve_redirects", resolver):
            result = await mcp.call_tool("ask", {"prompt": "p", "web": True})
        blocks = result[0] if isinstance(result, tuple) else result
        text = "".join(getattr(b, "text", "") for b in blocks)  # type: ignore[union-attr]
        assert TYPED_URL_CAUTION in text
        # The answer itself is left exactly as Gemini wrote it.
        assert "https://www.python.org/invented/path" in text
        assert TYPED_URL_CAUTION in transcript.path.read_text()

    async def test_web_off_adds_no_footer_and_makes_no_request(self, tmp_path: Path) -> None:
        from datetime import datetime
        from unittest.mock import AsyncMock

        from sidekick.client import GeminiClient
        from sidekick.server import build_server
        from sidekick.transcript import TranscriptWriter
        from sidekick.workspace import build_workspace
        from tests.test_tools import _text_response

        cfg = Config(auth={"method": "api_key"})
        with patch("google.genai.Client"):
            client = GeminiClient(cfg, api_key="k")
        client._raw_client.aio.models.generate_content = AsyncMock(
            return_value=_text_response("plain")
        )
        mcp = build_server(
            client,
            TranscriptWriter(str(tmp_path / "t"), datetime.now()),
            build_workspace(cfg, tmp_path),
        )
        with patch("httpx.AsyncClient") as http:
            result = await mcp.call_tool("ask", {"prompt": "p", "web": False})
        blocks = result[0] if isinstance(result, tuple) else result
        assert "".join(getattr(b, "text", "") for b in blocks) == "plain"  # type: ignore[union-attr]
        http.assert_not_called()

    def test_gemini_is_told_not_to_type_urls(self) -> None:
        from sidekick.tools.base import _WEB_PREAMBLE

        assert "Do not write out URLs" in _WEB_PREAMBLE
        assert "Cite the sources" not in _WEB_PREAMBLE
