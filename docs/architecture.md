# Architecture

The end-to-end request flow for a Gemini call, as Claude Code sees it: [diagrams/request-flow.mermaid](diagrams/request-flow.mermaid). For OpenRouter calls, see [diagrams/backend-routing.mermaid](diagrams/backend-routing.mermaid) and [diagrams/first-use-acceptance.mermaid](diagrams/first-use-acceptance.mermaid).

## Components

**Diagram:** [diagrams/components.mermaid](diagrams/components.mermaid) — how the startup modules, the server and the eight tools connect.

The five generating tools route through `call_gemini()` in `tools/base.py`.
`list_models` and `list_sessions` call the client directly and never write a transcript;
`list_sessions` only reads the in-memory session cache.
`help` never calls the backend: `server.py` hands it a callable that renders text
from `guide.py`.

## Module Map

Package root: `src/sidekick/`.

| Module | Responsibility | Depends on (in-package) |
|---|---|---|
| `__init__.py` | Package identity only | none |
| `__main__.py` | Startup: `main()` configures logging (daily file + stderr; importing the module writes nothing, #144), then `_start()` builds config, auth and `GeminiClient` + `refresh_latest()` (when Gemini is configured), `project_root()`, workspace, `OpenRouterClient` + `Router` (when an OpenRouter key is set), transcript, server. On a config, auth (with no OpenRouter key), transcript or `artifacts_dir` error it serves the `setup_status`-only setup server instead (#116); `main()` adds a last-resort handler that does the same for any other startup exception, naming only its type. See [Startup & setup mode](#startup--setup-mode) | config, auth, backends, client, openrouter, router, transcript, workspace, sandbox, server |
| `config.py` | Pydantic models `Config`, `AuthConfig`, `FileToolsConfig`, `WebToolsConfig`, `OpenRouterConfig` and the `ModelFamily` enum; loads and validates `config.json`; raises `ConfigError`. `plugin_option()` reads the plugin options Claude Code passes as env (`SIDEKICK_API_KEY`, `SIDEKICK_SA_FILE`, `SIDEKICK_OPENROUTER_KEY`), treating empty or a literal `${user_config.…}` as unset. With a plugin key set, no config file is needed, and a file that names no `auth.method` defaults to `api_key` (#116). `gemini_configured()` is false only for an OpenRouter-only setup. The `remember_*` writers save consent answers (`openrouter.accepted_models`, `openrouter.repo_access_models`, `gemini_data_use_accepted`) atomically | sandbox (`DEFAULT_DENY`) |
| `auth.py` | Credentials for `adc`, `env`, `keychain` and `api_key`; `build_auth()` returns `AuthResult(credentials, api_key)`; raises `AuthError`. The plugin option wins over the shell env: `api_key` reads `SIDEKICK_API_KEY`, then the var named by `api_key_env` (default `GEMINI_API_KEY`); `env` reads `SIDEKICK_SA_FILE`, then `GOOGLE_APPLICATION_CREDENTIALS`. An SA-file value is echoed in errors and logs only when it is path-shaped (#116, #119) | config |
| `errors.py` | `ClientError`, in its own module so `client.py` and `tool_loop.py` can both raise it | none |
| `models.py` | Model taxonomy: per-backend shortlist, backend detection, schema hint, chat-capable filter, newest-per-family resolution, alias mapping. Pure, no I/O | none |
| `backends.py` | Pure types shared by every backend: `Session`, the `Backend` and `ModelSource` protocols, `Surface`, `Preflight` (proceed, message, notice, whether file tools may go), `Elicit` (asks the user; `accept`, `decline`, `cancel` or `unavailable`). No SDK imports | config, errors, tool_loop (types only) |
| `router.py` | `Router`: the `ModelSource` the tools see when an OpenRouter key is set. Routes each call to OpenRouter or Gemini, merges sessions, reports the `Surface` (see [backend-routing](diagrams/backend-routing.mermaid)) | backends, client, config, models, openrouter |
| `openrouter.py` | `OpenRouterClient`: chat over OpenRouter's HTTP API, retries, the first-use price and privacy preflight ([first-use-acceptance](diagrams/first-use-acceptance.mermaid)), free-model fallback (tool-capable models first), `tools_supported(model)`, and, for tool-capable models, file tools with `provider.require_parameters` after the user accepts repository access for that model (`openrouter.repo_access_models`). Fails closed when sidekick can't ask: a paid model is not sent, not even the billed probe (#141) | config, backends, client, errors, openrouter_catalog, openai_tool_loop, tool_loop (types only) |
| `openai_tool_loop.py` | `run_openai_tool_loop()`: the OpenAI-format function-calling loop for OpenRouter (`tool_calls` → `tool` messages), over the shared `ToolRegistry`; dispatch, records, round cap and budget prompt come from `tool_loop.py`. HTTP-free: the caller supplies `post` | tool_loop |
| `openrouter_catalog.py` | OpenRouter's public model catalog: prices, privacy status, free candidates, search, `FREE_LIMITS`, and the user-facing messages and consent questions. The key is never sent here | config, models |
| `client.py` | `GeminiClient`: builds the google-genai client, LRU session cache (50) and `sessions()` for `list_sessions`, model resolution, thinking translation that learns per-model rejections, 503/429 retry with backoff, `build_config()`, `ask()`. With API-key auth, `preflight()` asks before the first call that Google may use prompts if the key's project has no billing, remembered as `gemini_data_use_accepted`; it fails closed when sidekick can't ask (#142) | backends, config, errors, models, tool_loop, web_tools |
| `tool_loop.py` | `ToolRegistry` (declarations for Gemini, or as OpenAI-format `openai_tools()`), `ToolCallRecord`, `run_tool_loop()` for Gemini (up to 20 rounds, then a forced answer), `record_grounding()` for server-side web activity | errors |
| `sandbox.py` | Path confinement: resolves every model-supplied path inside the root, applies the deny-list at any depth, walks without leaving the root | none |
| `file_tools.py` | `list_dir`, `glob`, `grep`, `read_file`, `write_file` on top of `Sandbox`, with result and size caps; publishes them to a `ToolRegistry` (read-only or read+write) | sandbox, tool_loop |
| `artifacts.py` | `ArtifactStore`: saves a final answer as `<YYYYMMDD-HHMM>-<tool>-<slug>.md`, never overwriting | none |
| `workspace.py` | `project_root()`: `CLAUDE_PROJECT_DIR` when it is a real directory, else the cwd (#102). `Workspace`: Sandbox rooted at that root, `FileTools`, per-call registry, `ArtifactStore`. Disables file tools when the root is home, `/`, or an ancestor of home | config, sandbox, file_tools, tool_loop, artifacts |
| `web_tools.py` | `resolve_capabilities()`: decides web and write for a call, never both (#76). `web_tool_set()`: the `google_search` + `url_context` built-ins | none |
| `sources.py` | Resolves Google grounding redirect links to real URLs with one HEAD request each (no redirect follow, 3 s timeout, in parallel); renders the sources footer (#80, #82) | tool_loop (`ToolCallRecord`) |
| `transcript.py` | `TranscriptWriter`: appends each exchange as Markdown; write errors are logged, never raised | none |
| `guide.py` | Text only: the short server instructions (kept under `INSTRUCTIONS_BUDGET` = 2000 characters) and the `help` topics, all derived from the live capability rows, deny-list, caps, web support and the backend `Surface` (#74, #78) | backends, tools, tools/base, file_tools, openrouter_catalog, sandbox, web_tools, workspace, transcript |
| `server.py` | `build_server()`: `FastMCP` with the guide's instructions and all 8 tools. `build_setup_server(problem)`: setup mode, whose instructions are the problem text and whose only tool is `setup_status` (#116) | backends, guide, tools, transcript, workspace |
| `setup_cli.py` | The `sidekick-setup` CLI behind `/sidekick:setup`: `status` reports saved values, `write` takes the answers as argv, changes only the wizard's keys, writes atomically and backs up an unreadable config under a timestamp (#85, #95, #96, #102). `openrouter` saves the OpenRouter default model, accepted models and `block_data_collection`; `keychain-store` puts a service-account key in the macOS keychain | auth, config |

Tools package: `src/sidekick/tools/`.

| Module | Responsibility | Depends on (in-package) |
|---|---|---|
| `tools/__init__.py` | Re-exports every `register_*` and collects the five `CAPABILITY` rows into `CAPABILITIES` | every tool module, tools/base |
| `tools/base.py` | `ToolCapability`, `capability_hint()`, `tool_annotations()`, `session_param_hint()`, `model_param_hint()`, `elicitor()` (an `Elicit` over MCP elicitation, or None when the client can't be asked), and `call_gemini()` | backends, client, config, file_tools, sandbox, sources, tool_loop, transcript, web_tools, workspace |
| `tools/ask.py` | `ask`: general question. Read-only, no artifacts | tools/base, backends, config, transcript, workspace |
| `tools/brainstorm.py` | `brainstorm`: divergent ideas. Read+write, artifacts opt-in | same as ask |
| `tools/review.py` | `review`: critical review. Read+write, artifacts on by default | same as ask |
| `tools/debug.py` | `debug`: root-cause hypotheses. Read-only, no artifacts | same as ask |
| `tools/architect.py` | `architect`: design and tradeoffs. Read+write, artifacts on by default | same as ask |
| `tools/list_models.py` | `list_models`: the OpenRouter section (featured picks, `query=`, `check=`) and the live, chat-capable Gemini catalog, which falls back to the static shortlist. `check=` on a paid model not yet accepted asks before its billed privacy probe (#142) | backends, client, models, openrouter, openrouter_catalog, tools/base, transcript (signature only) |
| `tools/list_sessions.py` | `list_sessions`: renders the live sessions (tool, `session_name`, model, turns), most recently used first (#15) | backends, client, tools/base |
| `tools/help.py` | `help`: returns one help topic or all of them from an injected render callable, so it never imports `guide.py` (#78) | none |

There is no `Router` without an OpenRouter key: a Gemini-only server keeps `GeminiClient` as its
`ModelSource`, so its behaviour, instructions and tool text are what they were before the
OpenRouter backend existed. The `Router` is added only when a key is present, and `build_server()`
reads its `surface` to word the instructions and help.

## Startup & setup mode

**Diagram:** [diagrams/startup.mermaid](diagrams/startup.mermaid) — the startup sequence and every path into setup mode.

`__main__.main()` configures logging first, then calls `_start()`. Importing the module
writes nothing (#144). `_start()` runs in order: `load_config()`; when `gemini_configured()`,
`build_auth()`, `GeminiClient(...)` and `refresh_latest()` (one `models.list` call; pinned ids
if it fails); `project_root()`; `build_workspace(config, root)`; with an OpenRouter key,
`OpenRouterClient(...)`, `refresh_catalog()` and `Router(...)`; `TranscriptWriter(...)`;
`build_server(...)`. Plugin options arrive as env (`SIDEKICK_API_KEY`, `SIDEKICK_SA_FILE`,
`SIDEKICK_OPENROUTER_KEY`, set in `plugin.json` `mcpServers.mcp.env`) and are read by
`config.plugin_option()`.

With an OpenRouter key and no saved `auth.method` or `project`, Gemini is not set up at all and
the server runs on OpenRouter only. With an OpenRouter key, an `AuthError` does not start setup
mode either: the server logs a warning and runs on OpenRouter only. See
[diagrams/auth-resolution.mermaid](diagrams/auth-resolution.mermaid).

- **Triggers:** a `ConfigError`, an `AuthError` with no OpenRouter key, a `TranscriptError` or
  a `SandboxError` (the `artifacts_dir` check) in `_start()`, or any other exception, caught by
  `main()`'s last-resort handler. That handler reports only the exception type name; the traceback goes
  to the log.
- **What it serves:** `build_setup_server(problem)`. Its instructions are the problem text and
  its only tool is `setup_status`, so `/mcp` shows "connected" and Claude can explain the fix
  instead of showing only "Connection closed".
- **Not covered:** credentials that load but are wrong (a revoked key, an expired token) pass
  startup and fail on the first call.
- **Outside the safety net:** `server.run()` runs after the `try`, so a failure while serving
  never starts a second server on the same stdio.
- **Recovery:** fix the problem (`/sidekick:setup`, or the plugin options), then reconnect
  with `/mcp` or restart Claude Code.

## Data Flow (per generating tool call)

1. Claude Code sends an MCP tool call (e.g. `review`) with optional `thinking`,
   `session_name`, `model`, `web`, and, on tools that save artifacts, `write_artifact`.
2. The tool function in `tools/review.py` builds the prompt and calls `call_gemini()`.
3. `client.backend_for(model)` picks the backend: Gemini, or, with an OpenRouter key, the
   `Router`'s choice ([backend-routing](diagrams/backend-routing.mermaid)). `call_gemini()`
   then runs `resolve_capabilities()`: `web` is the call's argument, or the backend's web
   default when omitted; if web is on, `write_file` is withheld for this call.
   On Google Cloud Agent Platform (formerly Vertex AI) and on OpenRouter, web is dropped (the
   server-side web tools need a Gemini Developer API request flag), write is restored, and a notice is added to
   the reply.
4. `backend.get_or_create_session()` returns the session keyed `tool:session_name:model`.
   `backend.resolve_model()` picks the model: per-call `model`, else config `default_model`,
   else the newest Flash (on OpenRouter: `openrouter.default_model`, else the live free
   default). Aliases (`flash`, `flash-lite`, `pro`, `*-latest`) map to the newest release found
   at startup.
5. `backend.preflight()` may ask the user, through MCP elicitation, before anything is sent.
   Gemini with an API key asks once that Google may use prompts if the key's project has no
   billing (#142). OpenRouter asks about a model's price and privacy on first use, then,
   separately, whether it may read the repository (#141,
   [first-use-acceptance](diagrams/first-use-acceptance.mermaid)). A decline, or a client that
   can't be asked, returns a `[sidekick] Not sent: …` message: nothing is sent and nothing is
   written to the transcript. A free OpenRouter model can still go out unasked, with
   no-collection routing and no file tools.
6. It takes a file-tool registry from the workspace (read-only or read+write) only if the file
   tools are on, the model can use tools, and preflight allowed them, and appends the matching
   preambles to the system prompt.
7. On Gemini, `client.ask()` calls `run_tool_loop()`. Each round, `build_config()` sets the thinking
   parameter for the model's generation, the file-tool declarations, and, with web on, the
   `google_search` + `url_context` built-ins. `generate()` retries 503/429 with backoff.
   A thinking-level rejection is learned and the request rebuilt.
8. In the loop, `record_grounding()` records server-side searches, URL fetches and their
   sources. Gemini's file-tool calls are dispatched locally through the sandbox and recorded.
   The loop ends when Gemini answers in text. On OpenRouter, `OpenRouterClient.ask()` runs
   `run_openai_tool_loop()` instead, for models whose catalog entry lists tool calling and that
   the user let read the repository: the same registry dispatch, records and round cap, in the
   OpenAI chat-completions format (#132).
9. On success only, the prompt and final answer are committed to the session history.
10. If the call failed with a terminal 503/429 and the model was not already the fallback,
    `call_gemini()` retries once on the newest Flash-Lite (on OpenRouter: the next free model,
    also when a free model is gated; a paid model never switches) and prefixes a disclosure
    notice. The fallback model goes through its own preflight first.
    It does not retry if `write_file` already succeeded in this call. The retry sees the named
    session's earlier turns, and its answer is committed to that same session, so the next call
    without `model=` continues from it (#122).
11. `resolve_redirects()` turns grounding redirect links into real URLs (HEAD only, no Gemini
    tokens), and `sources_footer()` appends up to 30 sources to the reply. The transcript
    gets the full list. When the answer itself contains URLs, the footer opens with a caution
    that those links were typed by the model, not taken from the grounding metadata, and may
    be fabricated (#92). The answer is never rewritten.
12. `transcript.append()` writes the exchange, including every recorded tool call.
13. If the tool asked for an artifact, `ArtifactStore.save()` writes it and the reply gets
    the artifact path. A failed save adds a notice; the answer is still returned.
14. The reply goes back to Claude Code as the tool result. Errors come back as
    `[sidekick error] ...` strings, never as exceptions.

**Diagram:** [diagrams/generating-call.mermaid](diagrams/generating-call.mermaid) — one generating call through the modules, step by step.

**Diagram:** [diagrams/tool-loop.mermaid](diagrams/tool-loop.mermaid) — steps 7–10 in detail: the function-calling loop, its caps, and how it ends.

## Session Lifecycle

- **Created:** on the first call for a key, with no API call. The key is
  `tool_name:session_name:concrete-model`, so changing the model starts a new session. On
  OpenRouter, a call without `model=` uses one default session that follows the current
  default model.
- **History:** owned by the bridge (`Session.history`), not by an SDK chat object. Only
  `[prompt, final answer]` is committed, and only when the call succeeds. Tool calls made
  during a call are not replayed into later turns.
- **Capped:** at most 50 sessions per backend; the least recently used one is evicted.
- **Persists:** for the lifetime of the MCP server process (one Claude Code session).
- **Destroyed:** when Claude Code restarts (new server process, new sessions).
- **Visible:** `list_sessions` lists them, most recently used first, with turn counts.

Each tool has its own sessions (`ask:default`, `review:default`, ...), so a
tool's system prompt persona stays fixed within its sessions.

## Transcript Lifecycle

- **File created:** on the first append, named with the server startup timestamp
- **File path:** `{transcript_dir}/YYYYMMDD-HHMM-sidekick-transcript.md`
  (`transcript_dir` defaults to `./session-summaries`)
- **Appended:** after every successful generating call, and after a failed one that had
  already made tool calls. A call refused at preflight writes nothing
- **Contents:** tool, thinking level, session, prompt, each tool call (file tools,
  searches, URL fetches), the response, and the full sources list
- **Never truncated:** append-only; write errors are logged, never break tool calls
- **Per session:** one file per server process lifetime

## SOLID Notes

| Principle | How it's enforced |
|---|---|
| SRP | Each module owns one concern (see the module map) |
| OCP | New tool: add a module, export it from `tools/__init__.py`, one register call in `server.py`. New auth: add a loader + one entry in `_LOADERS`. New tool source: add (declaration, handler) pairs to a `ToolRegistry`; Gemini and OpenRouter (`openai_tools()`) both read it |
| LSP | All auth paths return an `AuthResult`; all tools return `ToolResult` |
| ISP | Tools receive a `ModelSource`, `TranscriptWriter` and `Workspace`, never config or credentials |
| DIP | `client.py` depends on the `Credentials` abstraction; `tools/help.py` takes a render callable instead of importing `guide.py` |
