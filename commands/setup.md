---
description: Set up sidekick (Gemini second opinion): check uv, pick an auth path, verify it works
allowed-tools: Bash(uv --version), Bash(UV_PROJECT_ENVIRONMENT=* uv run --frozen --quiet --project * sidekick-setup *), AskUserQuestion, mcp__plugin_sidekick_mcp__setup_status, mcp__plugin_sidekick_mcp__list_models
---

Set up the sidekick plugin. Keep every message short: one step at a time, and say
what the user does next. Never ask for the key itself, never ask the user to paste
it into the chat, and never read, print or store its value. The key only ever goes
into Claude Code's own masked plugin prompt.

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
3. Ask which auth path, with AskUserQuestion. Put the saved one first (from
   `values.auth_method`); for a new user put path A first and mark it
   "(Recommended)":
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

   **A. Keychain.** Tell the user, in these words: "Type `/plugin`, open the
   Installed tab, select sidekick, choose **Configure options**. On the **Gemini API key**
   row, paste your key directly. Pressing Enter on an empty row does not open
   a prompt; it just closes. The key shows masked except its last few
   characters. Press Enter to save. It is stored in your keychain. Get a key
   at https://aistudio.google.com/apikey." Then use
   AskUserQuestion: "Done" or "I need a key first". Write `--auth-method 'api_key'`.
   To change or replace the key later, the user repeats the same `/plugin` steps.

   **B. Env var.** Ask for the NAME of the variable (default
   `values.api_key_env`, normally `GEMINI_API_KEY`). Say plainly that the
   variable must be set in the environment Claude Code is started from, and
   that path A avoids that. Write `--auth-method 'api_key' --api-key-env '…'`.
   If the user has also filled the plugin's key option, that option wins.

   **C. Agent Platform.** Ask how to sign in, from `auth_methods` only:
   - `adc` — "gcloud login (Recommended)". If `checks.gcloud` is false, tell
     them to install the Google Cloud CLI first
     (https://cloud.google.com/sdk/docs/install), then run
     `gcloud auth application-default login` with `! ` in front so it runs
     here.
   - `env` — "Service-account key file". Tell the user: "Type `/plugin`, open
     the Installed tab, select sidekick, choose **Configure options**, and on
     the **Service-account key file** row pick or paste the path to the JSON
     key, then press Enter to save." Then use AskUserQuestion: "Done" or "I
     export GOOGLE_APPLICATION_CREDENTIALS myself"
     (`checks.google_application_credentials` shows whether it is set here).
   - `keychain` — "Service account in macOS Keychain" (macOS only): ask for the
     service and account names (defaults `values.keychain_service`,
     `values.keychain_account`).
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

       UV_PROJECT_ENVIRONMENT="${CLAUDE_PLUGIN_DATA}/venv" uv run --frozen --quiet --project "${CLAUDE_PLUGIN_ROOT}" sidekick-setup write --auth-method '…' [--project '…'] [--location '…'] [--thinking '…'] [--default-model '…'] [--transcript-dir '…'] [--keychain-service '…'] [--keychain-account '…'] [--api-key-env '…']

   On `"ok": false`, show `error` and offer to retry from step 3. Otherwise
   show any `warnings`, and show `backup`'s path if it is non-null.
7. Tell the user to run `/mcp`, select `plugin:sidekick:mcp` and choose
   **Reconnect** (or restart Claude Code), then say "done". Then verify: call
   the `list_models` tool. If it returns models, say setup is complete. If only
   `setup_status` exists, call it, show the problem in one line, and go back to
   the step it points to.
