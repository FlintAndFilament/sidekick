# Roadmap

What has shipped, what is open, and what is parked. Rebuilt from git history and GitHub issues on
2026-11-11; everything under **Shipped** is on `main`.

The `26.7.x` labels are the names the July releases went out under. Work after 26.7.4 is grouped
by feature and issue number. From the plugin on, releases carry the plugin version
(`.claude-plugin/plugin.json`): 0.1.0 plugin, 0.2.0 and 0.3.0 rename, 0.4.0 `list_sessions`
(tagged `v0.4.0`), 0.5.0 auth set up inside Claude Code (tagged `v0.5.0`).

The project was called gemini-bridge until 2026-09-24 (#104). Upgrading an older install: **first** run `mv ~/.config/gemini-bridge ~/.config/sidekick`, then update from a terminal with `claude plugin marketplace update sidekick && claude plugin update sidekick@sidekick`, then run `/reload-plugins` in Claude Code. (Move the folder before updating: after the update, the new server has already created `~/.config/sidekick/`, and `mv` nests the old folder inside it. `/plugin update` inside a session does not update the plugin.)

---

## Shipped

### 26.7.1 — Core

**5 tools, ADC + env + Keychain auth, persistent sessions, transcript logging, structured logging,
full docs.**

- `ask`, `brainstorm`, `review`, `debug`, `architect`
- ADC (Application Default Credentials) — one-time setup, SDK auto-refreshes
- Env-file service account auth (`GOOGLE_APPLICATION_CREDENTIALS`, #14)
- Apple Keychain service account auth, macOS only (#16): the SA JSON is read into memory at
  startup via `security(1)` and hex-decoded when the CLI returns it that way (#25)
- Transcript logging to `YYYYMMDD-HHMM-sidekick-transcript.md`, project-local
  `./session-summaries` by default (#33)
- Structured logging to stderr and a daily log file, 4 files kept (#26, #27, #28)
- Interactive `setup.sh` wizard that pre-fills prompts from an existing config (#23, #24)
- Full `docs/` directory + README.md

**Why this scope:** Five tools cover the primary use cases (consult, challenge, critique, debug,
design). Persistent sessions give Gemini context across related calls. ADC requires no
credential management beyond one-time setup.

### 26.7.2 — Google AI Studio API key auth

- `api_key` auth method (#30): no GCP project or `gcloud` needed. `AuthResult` + `build_auth()`
  dispatch in `auth.py`; `project` became optional, required only for the Google Cloud Agent
  Platform (formerly Vertex AI) methods. The config stores the env var **name**, never the key.
- CI warnings and failures fixed (#32); transcript files renamed to `sidekick-transcript`
  (#35).
- 26.7.2.1 hotfix (#39): clearer `setup.sh` API key prompt (asks for the variable name and rejects
  a pasted key), and the key no longer leaks into the error log.

### 26.7.3 — Correctness and reliability (#42–#49)

- Tool personas actually reach Gemini: `system_instruction` is sent on every call (#42)
- `thinking="none"` clamped to the minimum budget on Gemini 2.x Pro models (#43)
- Retry with exponential backoff on 503/429 (#45)
- Empty or blocked responses report their `finish_reason` instead of returning nothing (#46)
- Session cache bounded: least recently used session dropped past 50 (#48)
- Thinking-parameter choice driven by a `ModelFamily` enum rather than name sniffing (#49), plus
  an encapsulation cleanup (#47)

### 26.7.4 — Per-call models and fallback

- Per-call `model=` on every generating tool; sessions are keyed per model (#51)
- Backend-aware warnings for model names that likely 404 on the active backend (#44)
- Overload errors carry an actionable hint (try again, or pass another model) (#52)
- Google's `-latest` aliases accepted, with a transparent, disclosed fallback model on a terminal
  503/429 (#53)

### Model discoverability and defaults (#55, #56, #58, #59, #60, #64)

- Each tool's `model` parameter description lists the models valid for the active backend, and
  `list_models` returns the live, chat-only catalog, falling back to a curated shortlist
  when the live list can't be read (#56). Single source of truth in `models.py`.
- `list_models` lists only models the bridge can run and accepts Gemini 3 previews (#60);
  it works on Agent Platform, which reports no `supported_actions` (#64).
- Tool parameter descriptions reach Claude (they were silently dropped before) (#55).
- `default_model` config field (#59).
- Moved off Gemini 2.5 ahead of its 2026-10-16 retirement: fallback and shortlists now use 3.x
  models (#58).

### Setup fix (#66)

`setup.sh` prints `claude mcp add sidekick -s user …` with the server name first. Without the
name, `claude mcp add` registered the server as `python3` and it failed to connect.

### Repository file tools (#68)

Gemini reads the repository itself with `list_dir`, `glob`, `grep` and `read_file`, sandboxed to
the directory Claude Code was launched in, with a deny-list for `.git`, `.env*`, keys and
credentials. `brainstorm`, `review` and `architect` can also `write_file`
(capped by `file_tools.max_write_bytes`). The bridge runs its own tool loop and saves
`architect` / `review` answers (and opted-in `brainstorm` answers) as
artifacts in `artifacts_dir`. `file_tools.enabled=false` turns it off; it also turns itself off
when launched from the home directory or filesystem root. See
[tools.md](tools.md#repository-access).

### Newest-model resolution (#69)

At startup the bridge reads the live catalog and resolves the newest Flash, Flash-Lite and Pro.
Calls without `model=` get the newest Flash; aliases `flash` / `flash-lite` / `pro` (and Google's
`-latest` names, translated so they also work on Agent Platform) track the newest release; the fallback is
the newest Flash-Lite. Pinned ids are used when the catalog can't be read. The thinking level is
mapped per concrete model, and the bridge learns and remembers which levels a model rejects. See
[configuration.md](configuration.md#choosing-a-model).

### CI security settings (#72, #73)

Workflows declare least-privilege permissions: Semgrep and Trivy get `security-events: write` so
SARIF upload works, Dependency Review gets `contents: read` (#72). The dependency graph was turned
back on after the org move so Dependency Review passes (#73).

### MCP capability metadata (#74)

Each tool's MCP description and annotations say what Gemini can read and write for that tool, the
write cap, and whether artifacts are saved, built from one capability row per tool so they can't
drift from the wiring.

### Web access (#76)

`web=true` (or `web_tools.enabled`) attaches Gemini's server-side `google_search` and
`url_context`. Off by default because grounding is billed per request. Web access and `write_file`
never share a call. Developer API (`api_key`) only: on Agent Platform, web is dropped and the reply carries
a notice. See [tools.md](tools.md#web-access).

#70 (let Gemini call external MCP tools such as Exa or Firecrawl) was closed as not planned,
superseded by #76.

### Usage help (#77, #78)

- The transcript records the file size for each `write_file` (it had logged the response size),
  the server instructions explain when to use web access and sessions, and every tool shares one
  accurate `session_name` description (#77).
- Server instructions cut to fit under Claude Code's ~2048-character cap; the full guide moved to
  a new `help` tool (#78). The server then registered 7 tools.

### Web sources (#80, #82)

Web answers end with a sources list taken from the grounding metadata (#80). The bridge resolves
Google's grounding redirect links to the real URLs, and Gemini is told not to type URLs itself, so
every listed URL comes from the metadata (#82). Resolving costs no Gemini tokens.

### Hardening (#85–#99), web source names (#90, #92)

A batch of review fixes from 2026-09-22 and -23, before the plugin.

- `setup.sh` re-runs merge into `config.json` instead of rewriting it, so hand-added
  `artifacts_dir`, `file_tools` and `web_tools` survive (#85). The write is atomic (#95), each
  backup gets its own timestamped name instead of overwriting the last one (#96), and tests cover
  injection round-trips and api_key project/location handling (#97). `config.json` is read once,
  not twice (#99). Its closing text names the `default_model` field (#89).
- Auth method `env` no longer falls back to the user's ADC when
  `GOOGLE_APPLICATION_CREDENTIALS` is unset; it fails with a clear error (#86).
- An unusable `transcript_dir` gives an actionable startup error instead of a traceback (#87),
  including `~nosuchuser` paths that failed during path resolution (#94).
- Library logs (google, google_genai, httpx, httpcore, urllib3) reach the log file; the old level suppression had no
  effect (#88). Credential payloads are dropped from the log deliberately (#98).
- A web source with no title is named after its resolved URL's host, not the redirect link, and
  untitled sources are still counted in the transcript summary (#90). When Gemini types a URL
  into a web answer, the sources footer opens with a caution that only the recorded list is
  authoritative; the answer itself is left as is (#92).

### Sidekick plugin (#102)

Repackaged as a Claude Code plugin, `sidekick`, installed with `/plugin marketplace add` +
`/plugin install` instead of a manual clone and `claude mcp add`. `setup.sh` is gone;
`/sidekick:setup` (a slash command) drives a new `sidekick-setup status|write` CLI that
never parses free text as shell, JSON or Python. The server launches with
`uv run --frozen --project ${CLAUDE_PLUGIN_ROOT} sidekick`, using a venv at
`${CLAUDE_PLUGIN_DATA}/venv` that `uv` builds once and reuses across restarts and version
bumps. Project-relative paths (`transcript_dir`, `artifacts_dir`, the file-tools sandbox root)
now anchor to `CLAUDE_PROJECT_DIR`, falling back to the working directory when Claude Code
doesn't set it. Tools now appear to Claude as `mcp__plugin_sidekick_mcp__*`.

#38 (ask for `-s user` / `project` / `local` scope in `setup.sh`) was closed as not planned,
superseded by #102 — `/plugin install` chooses the install scope itself, so there is no
`setup.sh` scope prompt left to build.

### Rename to sidekick (#104, #107) — 0.2.0, 0.3.0

The project, repository, package, CLIs, config folder (`~/.config/sidekick/`), log and transcript
names and notices all became `sidekick` (#104, 0.2.0). Then the MCP server became
`plugin:sidekick:mcp` and the tools lost their `gemini_` prefix: `ask`, `brainstorm`, `review`,
`debug`, `architect`, `list_models`, `help` (#107, 0.3.0). A clean break with no compatibility
shim; see the upgrade note at the top. "Gemini" stays where it names Google's model or API.

### Named sessions (#15) — 0.4.0

`session_name` routing was already live on every generating tool (separate per tool and per
model, LRU-capped at 50, named in each transcript header). 0.4.0 adds `list_sessions`: the live
conversations with tool, name, model and turn count, most recently used first. The planned
`new_session` reset tool was dropped, because a new name already starts fresh and every
`session_name` description says so. The server now registers 8 tools.

#17 (a `set_transcript_dir` tool) was closed as not planned: transcripts are already
project-local by default, and nothing has needed to switch directories mid-session.

### Auth set up inside Claude Code (#116) — 0.5.0

No shell-profile export or launch alias is needed. The Gemini API key is a masked plugin option
that Claude Code keeps in the system keychain, and the Agent Platform
service-account file is a picker option; both are set from `/plugin` → Configure options. A
missing or broken credential no longer ends in "Connection closed": the server starts in
setup mode with one `setup_status` tool that explains the fix. `/sidekick:setup` walks three
paths (keychain key, env-var key, Agent Platform) and verifies them with a live model list.
Also in `v0.5.0`:

- certifi bumped for Dependabot alert #1 (#117).
- The test suite builds its fake API keys at runtime instead of committing key-shaped
  literals, which had tripped secret-scanning alert #1 (#113).
- The service-account file guard echoes a value only when it is shaped like a file path. The
  old check looked for `AIza…` keys and missed newer `AQ…` keys pasted into the option (#119).

### Fixes (#122–#124) — 0.5.2

- A 503/429 fallback answer is now part of the named conversation. The retry on Flash-Lite sees
  the session's earlier turns, and its answer is committed to the requested model's session, so
  the next call without `model=` continues from it. `list_sessions` no longer shows a separate
  fallback-model row (#122).
- The `env` method loads a service-account key only. It no longer calls google-auth's deprecated
  `load_credentials_from_file`, which accepts any credential type, including an external-account
  config that fetches tokens from URLs the file names. Any other type is refused with an error
  that names the type it found (#124).
- `/sidekick:setup` stores a service account in the macOS Keychain itself. It asks for the key
  file's path, never its contents. The new `sidekick-setup keychain-store` subcommand checks the
  file is a service-account key, passes it to `security` on stdin (never argv, never the
  chat), reads it back to prove it works, and can delete the file afterwards (#123).

### OpenRouter backend (#133) — 0.6.0

- OpenRouter is the default backend when an OpenRouter key is set (masked plugin option, or a
  shell `OPENROUTER_API_KEY`). The Gemini API and Agent Platform stay as the second source and
  work exactly as before without the key. Model ids shaped `author/model` go to OpenRouter;
  Gemini ids and the `flash` / `flash-lite` / `pro` aliases go to Gemini.
- OpenRouter models are conversation only: they see the prompt Claude sends, not your files or
  the web (tool use is #132).
- Prices come live from OpenRouter's catalog. The first use of a model asks once, showing its
  price and whether its providers may store or train on prompts; accepted models are remembered.
- Privacy: allowed by default with a clear warning. `openrouter.block_data_collection: true`
  routes every request only to providers that don't collect data, and every block says it came
  from that setting. Without anyone to ask (unattended runs), a model is used only with
  privacy-only routing, or refused.
- With no model picked, sidekick uses a free model and switches to another free one when it is
  busy or limited to agent apps; paid models never switch. A one-time notice warns that Gemini
  free-tier API keys let Google use prompts.
- `list_models` shows prices, privacy markers, search (`query=`) and a privacy check (`check=`).
  `/sidekick:setup` leads with OpenRouter.

---

## Open

Tool use for OpenRouter models (#132).

New work is tracked in [GitHub issues](https://github.com/FlintAndFilament/sidekick/issues).

---

## Backlog

### Sliding window context management

Summarize and trim older exchanges when a session approaches the model's context limit.

**Why deferred:** Current Gemini models have a 1M-token context window, and a typical Claude Code
session's tool calls won't approach it. Sessions already end at server restart and the least
recently used one is dropped past 50. This matters only for very long sessions that are never
reset.

### Location-aware model validation

The server does not check at startup that the chosen model is served in the configured
Agent Platform `location`; a mismatch shows up as a 404 at call time. `"global"` avoids this. No issue filed yet.
