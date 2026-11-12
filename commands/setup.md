---
description: Set up sidekick (a second opinion from the model you choose): check uv, pick OpenRouter or Gemini, verify it works
allowed-tools: Bash(uv --version), Bash(UV_PROJECT_ENVIRONMENT=* uv run --frozen --quiet --project * sidekick-setup *), AskUserQuestion, mcp__plugin_sidekick_mcp__setup_status, mcp__plugin_sidekick_mcp__list_models
---

Set up the sidekick plugin. Keep every message short: one step at a time, and say
what the user does next. Never ask for the key itself, never ask the user to paste
it into the chat, and never read, print or store its value. The key only ever goes
into Claude Code's own masked plugin prompt.

If any answer looks like a key (starts with `sk-or-`, `AIza` or `AQ`, or is long mixed-case text,
or is JSON with `private_key`), never put it in a command, never repeat it, and
don't use it. Tell the user it was a key and that it belongs in the plugin's
masked option (path O or A), and suggest rotating it, since it is now in this chat.

Run every setup command exactly like this, so it uses the server's own venv
instead of a second one:

    UV_PROJECT_ENVIRONMENT="${CLAUDE_PLUGIN_DATA}/venv" uv run --frozen --quiet --project "${CLAUDE_PLUGIN_ROOT}" sidekick-setup <subcommand> …

1. Run `uv --version`. If uv is missing, use AskUserQuestion once: "Install uv
   (Recommended)" or "Skip". On install, run Astral's official installer:
   - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   If the user skips, print the command for their OS and stop. After
   installing, re-run `uv --version`; if it still fails, tell the user to
   restart Claude Code and run `/sidekick:setup` again, then stop (the
   installer changed PATH, but this session's Bash tool and the MCP server
   still have the old one).
2. Run:

       UV_PROJECT_ENVIRONMENT="${CLAUDE_PLUGIN_DATA}/venv" uv run --frozen --quiet --project "${CLAUDE_PLUGIN_ROOT}" sidekick-setup status

   Read `values` (saved answers), `platform`, `auth_methods` and `checks`. If
   `checks.api_key_env_invalid` is true, tell the user their saved config has
   an API key stored where a variable NAME belongs — never show the stored
   value — and that this setup will replace it. If `config_error` is set, tell
   the user their config couldn't be read and will be backed up.
   If the `setup_status` tool is available, call it: it says why the server
   could not start, which tells you what to fix.
3. Ask which path, with AskUserQuestion. Put the saved one first: path O when
   `values.openrouter_default_model` is set or `checks.openrouter_key_env_set`
   is true, otherwise from `values.auth_method`. For a new user put path O
   first and mark it "(Recommended)":
   - **O. OpenRouter** (`openrouter`): one key, many models (Grok, Kimi,
     DeepSeek, Gemini and more), with live prices and a free model. Label it
     "OpenRouter (one key, many models)".
   - **A. API key in the keychain** (`api_key`): a Google AI Studio key, kept
     by Claude Code in the system keychain (macOS Keychain; a protected
     credentials file elsewhere). No shell setup. Label it "API key (keychain)".
   - **B. API key from an environment variable** (`api_key`): for people who
     already export the key themselves (CI, scripts, Linux, Windows). Label it
     "API key (env var)".
   - **C. Google Cloud Agent Platform** (formerly Vertex AI; `adc`, `env` or
     `keychain`): a GCP project with billing. Label it "Google Cloud Agent
     Platform".
4. Follow the chosen path:

   **O. OpenRouter.** Numbered sub-steps:
   1. Tell the user: "Run `/plugin configure sidekick@sidekick`. On the
      **OpenRouter API key** row, paste your key directly (get one at
      https://openrouter.ai/settings/keys), then press Enter. It is stored in
      your keychain. Then run `/reload-plugins`." Use AskUserQuestion: "Done" or
      "I need a key first".
   2. Call `list_models` (no arguments). Show its rows for the five picks and
      the free default in a short list, with prices exactly as shown. Say:
      "Prices are per 1M tokens, input / output, live from OpenRouter.
      OpenRouter adds no markup; its fee is on buying credits." Use
      AskUserQuestion with 4 options: "Free model (skip)",
      "deepseek/deepseek-v4.1-flash-1017: best value",
      "moonshotai/kimi-k3.1-pro: best quality", "x-ai/grok-4.7". The automatic
      "Other" takes any id (for example `qwen/qwen3.8-max-0902` or
      `google/gemini-3.8-flash`). On "Free model (skip)", use the id marked
      `(free default)`. If the "Other" answer looks like a key (`sk-or-`,
      `AIza`, `AQ`, or `private_key` JSON), apply the key rule: do not run any
      command with it, and tell the user to paste keys only in the plugin
      option.
   3. Call `list_models(check='<id>')` and show its privacy sentence verbatim.
      For a free model, also show its limits line (it says free models allow
      20 requests a minute and 50 a day) and say: "Free models are often served
      by providers that store or train on prompts." Use AskUserQuestion: "Use
      it" or "Pick another" (on "Pick another", go back to sub-step 2).
   4. Use AskUserQuestion: "Warn me about providers that may store or train on
      prompts (Recommended)" or "Block them (block_data_collection)". If they
      block and the check said every provider may store or train on prompts,
      say: "With blocking on, this model can't be used, because every provider
      serving it may store or train on prompts." Then go back to sub-step 2.
   5. Say once: "OpenRouter screens all requests for abuse and keeps flagged
      content (since 2026-10-27); this happens whatever sidekick's settings are."
   6. Run `… sidekick-setup openrouter --default-model '<id>' --accept '<id>'
      --block-data-collection 'on|off'`. For the free choice, pass
      `--default-model '-'` so the live free default is used, and `--accept` the
      checked free id. On `"ok": false`, show `error`. The command never sees
      the key.
   7. Tell the user to run `/reload-plugins`, then call
      `list_models(refresh=true)`. Setup is complete when the chosen id shows
      `(default)`. A `[sidekick error]` from sub-step 3 that mentions the API
      key means the key is wrong: go back to sub-step 1.
   To accept another model for unattended use later: first run
   `list_models(check='<id>')`, show the user the price and privacy sentence, and
   only on their OK run `sidekick-setup openrouter --accept '<id>'`.
   Path O skips steps 5 to 7 below.

   **A. Keychain.** Tell the user, in these words: "Run
   `/plugin configure sidekick@sidekick`. On the **Gemini API key** row, paste
   your key directly. Pressing Enter on an empty row does not open
   a prompt; it just closes. The key shows masked except its last few
   characters. Press Enter to save. It is stored in your keychain. Get a key
   at https://aistudio.google.com/apikey. If this key's Cloud project
   has no billing, Google may use prompts and responses to improve its products
   (block_data_collection does not apply to Gemini)." Then use
   AskUserQuestion: "Done" or "I need a key first". Write `--auth-method 'api_key' --key-source 'plugin'`.
   To change or replace the key later, the user repeats the same steps; see
   docs/auth.md, "Change settings & reconnect".

   **B. Env var.** Ask for the NAME of the variable (default
   `values.api_key_env`, normally `GEMINI_API_KEY`). Say plainly that the
   variable must be set in the environment Claude Code is started from, and
   that path A avoids that. Tell the user: if this key's Cloud project has no
   billing, Google may use prompts and responses to improve its products
   (block_data_collection does not apply to Gemini). Write `--auth-method 'api_key' --api-key-env '…'`.
   If the user has also filled the plugin's key option, that option wins.

   **C. Agent Platform.** Ask how to sign in, from `auth_methods` only:
   - `adc` — "gcloud login (Recommended)". If `checks.gcloud` is false, tell
     them to install the Google Cloud CLI first
     (https://cloud.google.com/sdk/docs/install). Then, with `! ` in front so
     they run here, run `gcloud auth application-default login` and
     `gcloud auth application-default set-quota-project <PROJECT_ID>` (ask
     for the project ID first). Without the quota project, calls fail with
     `403 … quota project not set`.
   - `env` — "Service-account key file". Tell the user: "Run
     `/plugin configure sidekick@sidekick`, and on the **Service-account key
     file** row pick or paste the path to the JSON key, then press Enter to
     save." Then use AskUserQuestion: "Done" or "I
     export GOOGLE_APPLICATION_CREDENTIALS myself"
     (`checks.google_application_credentials` shows whether it is set here).
     On "Done", add `--sa-file-source 'plugin'` to the write.
   - `keychain` — "Service account in macOS Keychain" (macOS only). Ask for
     the service and account names (defaults `values.keychain_service`,
     `values.keychain_account`). Then use AskUserQuestion: "Store a key file
     now" or "Already stored". To store, ask for the PATH to the JSON key file,
     only the path. Never read, cat or open the file yourself. If the answer is
     JSON or a key rather than a path, treat it as a pasted key (see the top).
     Then ask with AskUserQuestion: "Delete the file after storing
     (Recommended)" or "Keep the file". Run:

         UV_PROJECT_ENVIRONMENT="${CLAUDE_PLUGIN_DATA}/venv" uv run --frozen --quiet --project "${CLAUDE_PLUGIN_ROOT}" sidekick-setup keychain-store --file '…' --keychain-service '…' --keychain-account '…' [--delete-file]

     It reads the key itself, passes it to macOS `security` on stdin (never
     on a command line), and reads it back to check that it works. Its output never
     contains the key. On `"ok": false`, show `error` and ask again. macOS
     may show a Keychain access prompt; tell the user to allow it.
     If a later write fails with `Keychain item not found`, the names don't
     match the stored item: ask again.
   Then ask for the GCP project ID and the location (default
   `values.location`; `global` recommended). Write `--auth-method '…'
   --project '…' --location '…'` plus the keychain names when used. Tell them
   to leave the plugin's API key option blank (if it holds a key, it is
   ignored while an Agent Platform method is saved).

5. Ask for the default thinking level (`none`/`low`/`medium`/`high`, default
   `values.default_thinking`) and default model (blank keeps the current
   value — none saved = newest Flash; `-` clears it). Offer the saved values
   first.

   If any free-text answer contains a single quote (`'`), reject it and ask
   again — no legitimate project, location, model, env var name or keychain
   name needs one.
6. Run write with only the answers actually given, quoting every value in
   single quotes:

       UV_PROJECT_ENVIRONMENT="${CLAUDE_PLUGIN_DATA}/venv" uv run --frozen --quiet --project "${CLAUDE_PLUGIN_ROOT}" sidekick-setup write --auth-method '…' [--project '…'] [--location '…'] [--thinking '…'] [--default-model '…'] [--transcript-dir '…'] [--keychain-service '…'] [--keychain-account '…'] [--api-key-env '…'] [--key-source 'plugin'] [--sa-file-source 'plugin']

   On `"ok": false`, show `error` and offer to retry from step 3. Otherwise
   show any `warnings`, and show `backup`'s path if it is non-null.
7. Tell the user to run `/reload-plugins` (or `/mcp` → `plugin:sidekick:mcp`
   → **Reconnect**), then say "done". Steps: docs/auth.md, "Change settings &
   reconnect". Then verify: call
   the `list_models` tool with `refresh=true`.
   - If only `setup_status` exists, call it, show the problem in one line, and
     go back to the step it points to.
   - If the reply starts with `[sidekick notice] Live model list unavailable`,
     the credentials did not work (wrong or revoked key, missing IAM role).
     Show the reason in one line and go back to step 4.
   - Otherwise setup is complete; say so in one line.
