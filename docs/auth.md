# Authentication

sidekick talks to Gemini through one of two Google backends: the **Gemini Developer API**
(Google AI Studio, an API key) or **Google Cloud Agent Platform** (formerly Vertex AI, a GCP
project). There are four auth methods, set by `auth.method` in
`~/.config/sidekick/config.json` (see [configuration.md](configuration.md)):

| Method | Backend | Needs a GCP project | Web access (`web=true`) |
|---|---|---|---|
| `api_key` | Gemini Developer API (Google AI Studio) | no | available |
| `adc` | Agent Platform | yes | not available |
| `env` | Agent Platform | yes | not available |
| `keychain` (macOS) | Agent Platform | yes | not available |

The method names are config values and stay as they are. **Which method applies when none is
saved:** with the plugin's **Gemini API key** option filled in and no `auth.method` in
`config.json` (or no file at all), it is `api_key`. With no key option, a `config.json` that
names no method means `adc`. With neither a key nor a file, the server starts in setup mode.
A method saved in `config.json` always wins, even over a filled-in key option.

[Diagram](diagrams/auth-resolution.mermaid): which credential each method uses, in order, and
every path into setup mode.

The three Agent Platform methods only differ in where the credentials come from; they share the
same models, `project`/`location` settings and IAM role (`roles/aiplatform.user`). **Web access
needs `api_key`**: on Agent Platform the bridge drops `web=true`, answers without the web and
says so in a `[sidekick notice]` (the Gemini API flag that lets web tools share a request with
the bridge's file tools is Developer-API only).

Credentials are loaded once, at server startup. If they are missing or **can't be loaded** (no
key, no file, an unreadable Keychain item, no ADC), the server starts in **setup mode**: it
still connects but offers only a `setup_status` tool that explains the problem and the fix, so
Claude can tell you what to do instead of `/mcp` showing just "Connection closed". The problem
is also written to the log (see [logging.md](logging.md)); the messages are listed in
[Troubleshooting](#troubleshooting).

Startup does **not** check that credentials work. A wrong or revoked key, an expired ADC token
or a missing IAM role loads fine, so all tools appear, and then calls fail. `list_models` shows
it as `[sidekick notice] Live model list unavailable (…)`; the last step of `/sidekick:setup`
runs that check for you.

**Setting up auth.** Install the plugin (`/plugin install sidekick@sidekick`), then run
`/sidekick:setup`. It offers three paths and walks you through the one you pick:

| Path | What you do | Shell setup |
|---|---|---|
| **A. API key in the keychain** (recommended) | Paste a Google AI Studio key into the plugin's masked **Gemini API key** option | none |
| **B. API key from an env var** | Export the key yourself (CI, scripts, Linux, Windows) | yes |
| **C. Google Cloud Agent Platform** | gcloud login (ADC), a service-account file (picked in the plugin options), or a service account in macOS Keychain, plus a GCP project | none |

[Diagram](diagrams/setup-flow.mermaid): the three `/sidekick:setup` paths, through to the live
`list_models` check.

To change or reset anything later, see [Change settings & reconnect](#change-settings--reconnect).

---

## Change settings & reconnect

This is the one place these steps are described; the rest of the docs link here. The server reads
its settings once, at startup, so every change ends with a restart.

1. **A plugin option** (the **Gemini API key** or the **Service-account key file**): run
   `/plugin configure sidekick@sidekick`. On the row you want, paste the value directly, then
   press Enter to save. No separate prompt opens, and Enter on an empty row just closes the
   dialog. The key shows masked except its last few characters. (`/plugin` → **Installed** →
   sidekick → **Configure options** opens the same dialog.)
2. **`config.json`** (auth method, project, defaults): run `/sidekick:setup` again, or edit
   `~/.config/sidekick/config.json` by hand (see [configuration.md](configuration.md)).
3. **Restart the server:** run `/reload-plugins`. It restarts sidekick with the new values. The
   alternative is `/mcp` → `plugin:sidekick:mcp` → **Reconnect**, or restarting Claude Code. A
   restart starts a new transcript file and drops the in-memory sessions (see
   [transcripts.md](transcripts.md#session-boundaries)).

From a terminal, `claude plugin configure sidekick@sidekick` shows which options are set. It
never prints a value. For scripts and CI, `claude plugin configure sidekick@sidekick
--values-stdin` reads the options as a JSON object of single-line strings on stdin, so the key
stays off the command line; options you leave out keep their values.

**Updating sidekick.** `/plugin update sidekick` inside a session does **not** update the
plugin. From a terminal, run:

```bash
claude plugin marketplace update sidekick && claude plugin update sidekick@sidekick
```

then run `/reload-plugins` in Claude Code.

---

## Method 1: API key (Google AI Studio)

The simplest path: no GCP project, no `gcloud`, no service account. Get a key from
[aistudio.google.com/apikey](https://aistudio.google.com/apikey).

**Path A: keychain (recommended).** The plugin declares a masked **Gemini API key** option.
Claude Code asks for it when you install or enable the plugin, and you can set or change it any
time: see [Change settings & reconnect](#change-settings--reconnect). Claude Code stores the
value in the system keychain (macOS Keychain; a protected credentials file on other platforms),
never in `settings.json`, and passes it to sidekick's server as `SIDEKICK_API_KEY`. There is
nothing to export and no launch alias to maintain, and it works however Claude Code is started:
terminal, tmux, desktop app, IDE.

With the option filled in, no `config.json` is needed. A config with no `auth.method` is
treated as `api_key`.

**Path B: environment variable.** If you already export the key yourself, leave the option
blank and choose "API key (env var)" in setup. It asks for the variable's **name** (default
`GEMINI_API_KEY`), never the key. The variable must be set in the environment Claude Code is
started from. If both are present, the plugin option wins. If the variable isn't set in
setup's own shell, setup only warns; if it isn't set where Claude Code starts either, the
server starts in setup mode.

Setup accepts only an UPPER_SNAKE name (`GEMINI_API_KEY`, `MY_KEY_2`) and asks again for
anything else. The server's own check is looser: it starts in setup mode only when the value
looks like a key (starts with `AIza`, contains lowercase letters, or is over 40 characters).
Newer keys can also start with `AQ`; either way, a key belongs in the plugin option, never in
`config.json`.

**Config written by `/sidekick:setup`:**
```json
{
  "default_thinking": "medium",
  "transcript_dir": "./session-summaries",
  "auth": {
    "method": "api_key",
    "api_key_env": "GEMINI_API_KEY"
  }
}
```

`project` and `location` aren't written, since the Developer API doesn't use them. If you
switch from an Agent Platform method, the old values stay in the file, unused.
`default_model` is added only if you entered one.

**Positioning:**
- Use this method for personal use, quick setup, or when you don't have a GCP project
- The Agent Platform methods remain first-class for team/enterprise use — they support larger
  quota tiers, org-managed access control, and service account rotation
- The API key is read at server startup and held in memory; it is never written to
  `config.json`
- This is the only method with web access (`web=true`, `web_tools.enabled`); see
  [tools.md](tools.md#web-access)

**Quota:** AI Studio rate limits depend on the model and on whether billing is enabled for the
key; check the current limits in AI Studio. Limits are generally lower than on Agent Platform.
When a model is overloaded or out of quota (503/429), the bridge retries with backoff and then
falls back once to the newest Flash-Lite (see [configuration.md](configuration.md#choosing-a-model)).

---

## Method 2: ADC (recommended for Agent Platform)

**One-time setup:**
```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

The first command opens a browser and stores a refresh token at
`~/.config/gcloud/application_default_credentials.json`. The SDK auto-refreshes access tokens
from this refresh token — no repeated re-auth.

The second command is required when using **user credentials** (as opposed to a service account).
User credentials span all your GCP projects, so Agent Platform needs to know which project's
quota to charge. Without it you will receive a `403 PERMISSION_DENIED: quota project not set`
error when calling the API. See the [Troubleshooting](#troubleshooting) table for the fix if you
already have ADC set up.

**Run `/sidekick:setup` and choose Google Cloud Agent Platform → "gcloud login".** It
checks `gcloud auth application-default print-access-token` and stops if ADC is not configured.

**Config:**
```json
{"project": "YOUR_PROJECT_ID", "auth": {"method": "adc"}}
```

**Minimum GCP permissions:** `roles/aiplatform.user` on the project.

With `auth.method = "adc"` the server calls `google.auth.default()`. That uses the file named
by `GOOGLE_APPLICATION_CREDENTIALS` first, if that variable is exported where Claude Code
starts, and only then your gcloud ADC login. So an exported service-account file silently wins
over the login; unset the variable if you mean to run as yourself.

### `gcloud auth login` vs ADC

These are **separate credential stores** and **separate re-auth timelines**:

| Command | Authenticates | Stored at | Expiry |
|---|---|---|---|
| `gcloud auth login` | gcloud CLI only | `~/.config/gcloud/credentials.db` | Short-lived; org session-timeout policy |
| `gcloud auth application-default login` | SDKs and APIs | `~/.config/gcloud/application_default_credentials.json` | Refresh token; typically months |

The server never uses the `gcloud auth login` session. When your gcloud CLI session expires and
you re-auth with `gcloud auth login`, the server is **unaffected**. You only need to re-run
`gcloud auth application-default login` if ADC itself expires (rare on a personal machine).

**Verify ADC is working:**
```bash
gcloud auth application-default print-access-token
```
If this returns a token, the server will load credentials. It does not prove the IAM role or
the quota project; `list_models` with `refresh=true` does.

---

## Method 3: Service-account key file (`env`)

Use a service account key file on disk. Pick it on the plugin's **Service-account key file**
row (see [Change settings & reconnect](#change-settings--reconnect)). Claude Code passes the path
to the server as `SIDEKICK_SA_FILE`. No shell export is needed, and it works however Claude Code
is started.

If you already export `GOOGLE_APPLICATION_CREDENTIALS` in the environment Claude Code starts
from, you can leave the picker blank and that variable is used. If both are set, the picker
wins.

The server loads the chosen file explicitly. It never falls back to your ADC user credentials:
no file, or a missing file, starts the server in setup mode with a message naming the problem
(#86), so this method can't run the server as you instead of the service account.

**Setup steps:**
1. Create or download a service account key in GCP IAM → Service Accounts → Keys
2. Grant the SA `roles/aiplatform.user` on your project
3. Store the JSON file in a secure location (e.g. `~/.config/sidekick/sa-key.json`, mode 600)
4. Run `/sidekick:setup`, choose Google Cloud Agent Platform → "Service-account key file", and
   pick the file on the plugin's **Service-account key file** row when it asks

**Auth block written by `/sidekick:setup`** (alongside `project` and `location`):
```json
{"auth": {"method": "env"}}
```

If you use the env var instead of the picker, `/sidekick:setup` checks it in its own shell. If
it is set but the file does not exist, setup stops with an error. With the picker, setup skips
that check, since only the server can see the picked path.

If the picker holds something that isn't a path (a pasted API key or key JSON), the server
refuses it without showing or logging it.

**Caution:** The key file persists on disk. Use only on full-disk-encrypted machines with restricted file permissions (`chmod 600`).

---

## Method 4: Service account in Apple Keychain (macOS only)

Store the service account JSON in Apple Keychain. The server reads it once at startup into memory — the raw JSON is never written to disk after the initial store, and disappears when the process exits.

**Why Keychain over a disk file:** A SA JSON on disk is a persistent secret at rest. Keychain provides OS-managed storage with ACL enforcement. This is the preferred service account path for DLP-sensitive environments.

**One-time setup — store the SA JSON.** `/sidekick:setup` does not store it for you (#123), so
run this in a terminal first, never in the chat:
```bash
security add-generic-password -U \
  -s "sidekick" \
  -a "vertex-sa" \
  -w "$(cat /path/to/sa-key.json)"
rm /path/to/sa-key.json   # remove disk copy immediately
```

`-U` replaces an existing item, so the same command re-stores a rotated key. The JSON is on
the `security` command line, visible to `ps`, for the moment the command runs. The bare `-w`
form, which prompts instead, would keep it off the command line, but the prompt reads a single
short line, so a multi-line service-account JSON can't be pasted into it.

**Then run `/sidekick:setup` and choose Google Cloud Agent Platform → "Service account in
macOS Keychain".** It asks for the service and account names, then checks that the item exists
and holds valid JSON before writing config. If the item is missing, it shows the same store
command.

**Auth block written by `/sidekick:setup`** (alongside `project` and `location`):
```json
{
  "auth": {
    "method": "keychain",
    "keychain_service": "sidekick",
    "keychain_account": "vertex-sa"
  }
}
```

**Minimum SA role:** `roles/aiplatform.user` on the project.
It covers the `generateContent` calls and the model listing the bridge does at startup. It
grants more than prediction, so if you need tighter scope, build a custom role and test it with
`list_models` and one `ask` call.

The server reads the item with `security find-generic-password -s <service> -a <account> -w`.
macOS returns a multi-line value (like SA JSON) as a hex string; the server detects and decodes
that before parsing.

macOS only. The `security` CLI is not available on Linux, and `/sidekick:setup` doesn't offer `keychain` there.

---

## How the plugin options reach the server

For contributors. The two plugin options arrive in the server's environment through
`.claude-plugin/plugin.json`:

| Option | Env var | Used by |
|---|---|---|
| `gemini_api_key` (**Gemini API key**) | `SIDEKICK_API_KEY` | `api_key`; wins over `api_key_env` |
| `service_account_file` (**Service-account key file**) | `SIDEKICK_SA_FILE` | `env`; wins over `GOOGLE_APPLICATION_CREDENTIALS` |

They are deliberately not named `GEMINI_API_KEY` or `GOOGLE_APPLICATION_CREDENTIALS`: a plugin
env entry would shadow a variable the user exports. An unset option can arrive as an empty
string or, on some Claude Code builds, as the literal `${user_config.…}` placeholder; the
server treats both as unset.

---

## Troubleshooting

| Error message | Cause | Fix |
|---|---|---|
| Only `setup_status` is offered | Setup mode: config or credentials couldn't load at startup | Call `setup_status` (or read the log); it names the problem. Fix it, then restart ([how](#change-settings--reconnect)) |
| `Config file not found … Run /sidekick:setup` | No `config.json` and no plugin key | Run `/sidekick:setup`, or paste a key on the **Gemini API key** row |
| `Config file is not valid JSON` / `Config file could not be read` / `Config validation failed` | A broken or unreadable `config.json` | Run `/sidekick:setup` (it backs up an invalid file) or fix the file; see [configuration.md](configuration.md) |
| `Unexpected startup error (<Type>)` | A bug or an unexpected environment problem | The log has the traceback (see [logging.md](logging.md)); please file an issue |
| `[sidekick notice] Live model list unavailable` from `list_models` | Credentials load but don't work: a wrong or revoked key, an expired ADC token, a missing IAM role or quota project | Fix the credentials, then restart; the reason is in the notice |
| `no ADC credentials found` | ADC not configured | `gcloud auth application-default login` |
| Token refresh / `invalid_grant` errors on a call | ADC refresh token expired or revoked | `gcloud auth application-default login` |
| `403 PERMISSION_DENIED: quota project not set` | ADC user credentials have no quota project | `gcloud auth application-default set-quota-project YOUR_PROJECT_ID` |
| `auth method is 'env' but no service-account key file is set` / `points at a file that does not exist` / `could not be loaded` | `env` method: no file picked and `GOOGLE_APPLICATION_CREDENTIALS` unset, a missing file, or a file google-auth cannot read. ADC is never used as a fallback | Pick the file on the **Service-account key file** row, then restart ([how](#change-settings--reconnect)); check the file's contents |
| `holds something that isn't a file path` | A key or key JSON was pasted where the file path belongs (it is never shown) | Clear the row and pick the JSON key file instead |
| `Keychain item not found` | Secret not stored, or different service/account names | Run the store command in [Method 4](#method-4-service-account-in-apple-keychain-macos-only), or fix the names with `/sidekick:setup` |
| `Keychain value could not be decoded` / `not valid service account JSON` | The Keychain item holds something other than SA JSON (valid JSON that isn't a service account lands here too) | Re-store the SA JSON with the same command (`-U` replaces the item) |
| `'security' CLI not found` | Not macOS | Keychain method is macOS-only |
| `no API key. The plugin's key option is empty and env var 'GEMINI_API_KEY' is not set` | Neither path A nor path B supplied a key | Paste the key on the **Gemini API key** row, then restart ([how](#change-settings--reconnect)) |
| `'api_key_env' in config.json contains what looks like an API key` | The key itself was put in `api_key_env` | Set `api_key_env` to the variable name, e.g. `"GEMINI_API_KEY"` |
| `'project' is required for Google Cloud Agent Platform auth methods` | `project` missing with `adc`/`env`/`keychain` | Add `project` to config.json, or switch to `api_key` |
| `transcript_dir: Transcript directory cannot be used` / `artifacts_dir …` | A configured directory can't be created, or lies outside the project | See [`transcript_dir`](configuration.md#transcript_dir) and [`artifacts_dir`](configuration.md#artifacts_dir) |
| `Web access was requested but is unavailable` | `web=true` on an Agent Platform method | Use `api_key` for web access |
