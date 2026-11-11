# Authentication

sidekick supports four auth methods, set by `auth.method` in
`~/.config/sidekick/config.json` (see [configuration.md](configuration.md)):

| Method | Backend | Needs a GCP project | Web access (`web=true`) |
|---|---|---|---|
| `adc` (default) | Vertex AI | yes | not available |
| `env` | Vertex AI | yes | not available |
| `keychain` (macOS) | Vertex AI | yes | not available |
| `api_key` | Gemini Developer API (Google AI Studio) | no | available |

The three Vertex methods only differ in where the credentials come from; they share the same
models, `project`/`location` settings and IAM role (`roles/aiplatform.user`). **Web access needs
`api_key`**: on Vertex the bridge drops `web=true`, answers without the web and says so in a
`[sidekick notice]` (the Gemini API flag that lets web tools share a request with the
bridge's file tools is Developer-API only).

"Vertex AI" here is **Google Cloud Agent Platform** (Google's new name for Vertex AI); the
config keeps the old method names.

Credentials are loaded once, at server startup. If they are missing or broken, the server still
connects but offers only a `setup_status` tool that explains the problem and the fix, so Claude
can tell you what to do instead of `/mcp` showing just "Connection closed". The problem is also
written to the log (see [logging.md](logging.md)); the messages are listed in
[Troubleshooting](#troubleshooting).

**Setting up auth.** Install the plugin (`/plugin install sidekick@sidekick`), then run
`/sidekick:setup`. It offers three paths and walks you through the one you pick:

| Path | What you do | Shell setup |
|---|---|---|
| **A. API key in the keychain** (recommended) | Paste a Google AI Studio key into the plugin's masked **Gemini API key** option | none |
| **B. API key from an env var** | Export the key yourself (CI, scripts, Linux, Windows) | yes |
| **C. Google Cloud Agent Platform** | gcloud login (ADC), a service-account file (picked in the plugin options), or a service account in macOS Keychain, plus a GCP project | none |

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

## The critical distinction: `gcloud auth login` vs ADC

These are **separate credential stores** and **separate re-auth timelines**:

| Command | Authenticates | Stored at | Expiry |
|---|---|---|---|
| `gcloud auth login` | gcloud CLI only | `~/.config/gcloud/credentials.db` | Short-lived; org session-timeout policy |
| `gcloud auth application-default login` | SDKs and APIs | `~/.config/gcloud/application_default_credentials.json` | Refresh token; typically months |

With `auth.method = "adc"` the MCP server uses **ADC only**. When your gcloud CLI session
expires and you re-auth with `gcloud auth login`, the MCP server is **unaffected**. You only need to re-run
`gcloud auth application-default login` if ADC itself expires (rare on a personal machine).

**Verify ADC is working:**
```bash
gcloud auth application-default print-access-token
```
If this returns a token, the MCP server will authenticate successfully.

---

## Method 1: ADC (Default, Recommended)

**One-time setup:**
```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

The first command opens a browser and stores a refresh token at
`~/.config/gcloud/application_default_credentials.json`. The SDK auto-refreshes access tokens
from this refresh token — no repeated re-auth.

The second command is required when using **user credentials** (as opposed to a service account).
User credentials span all your GCP projects, so the Vertex AI API needs to know which project's
quota to charge. Without it you will receive a `403 PERMISSION_DENIED: quota project not set`
error when calling the API. See the [Troubleshooting](#troubleshooting) table for the fix if you
already have ADC set up.

**Run `/sidekick:setup` and choose `adc`.** It checks
`gcloud auth application-default print-access-token` and stops if ADC is not configured.

**Config:**
```json
{"project": "YOUR_PROJECT_ID", "auth": {"method": "adc"}}
```

**Minimum GCP permissions:** `roles/aiplatform.user` on the project.

---

## Method 2: Env File SA

Use a service account key file on disk. Pick it in the plugin's options: `/plugin` →
**Installed** → sidekick → **Configure options** → **Service-account key file**. Claude Code
passes the path to the server as `SIDEKICK_SA_FILE`. No shell export is needed, and it works
however Claude Code is started.

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
   pick the file when it sends you to **Configure options**

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

## Method 3: Apple Keychain (macOS only)

Store the service account JSON in Apple Keychain. The server reads it once at startup into memory — the raw JSON is never written to disk after the initial store, and disappears when the process exits.

**Why Keychain over a disk file:** A SA JSON on disk is a persistent secret at rest. Keychain provides OS-managed storage with ACL enforcement. This is the preferred service account path for DLP-sensitive environments.

**One-time setup — store the SA JSON:**
```bash
security add-generic-password \
  -s "sidekick" \
  -a "vertex-sa" \
  -w "$(cat /path/to/sa-key.json)"
rm /path/to/sa-key.json   # remove disk copy immediately
```

**Run `/sidekick:setup` and choose `keychain`.** It prompts for the service and account names, then verifies the item exists and contains valid JSON before writing config.

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

**Minimum SA role:** `roles/aiplatform.user` (Vertex AI User) on the project. It covers the
`generateContent` calls and the model listing the bridge does at startup. It grants more than
prediction, so if you need tighter scope, build a custom role and test it with
`list_models` and one `ask` call.

The server reads the item with `security find-generic-password -s <service> -a <account> -w`.
macOS returns a multi-line value (like SA JSON) as a hex string; the server detects and decodes
that before parsing.

macOS only. The `security` CLI is not available on Linux, and `/sidekick:setup` doesn't offer `keychain` there.

---

## Method 4: API Key (Google AI Studio)

The simplest path: no GCP project, no `gcloud`, no service account. Get a key from
[aistudio.google.com/apikey](https://aistudio.google.com/apikey).

**Path A: keychain (recommended).** The plugin declares a masked **Gemini API key** option.
Claude Code asks for it when you install or enable the plugin in `/plugin`, and you can set or
change it any time from `/plugin` → **Installed** → sidekick → **Configure options**. On the
**Gemini API key** row, paste the key directly: no separate prompt opens, and Enter on an
empty row just closes the dialog. The key shows masked except its last few characters. Press
Enter to save. From a
terminal, `claude plugin configure sidekick@sidekick` shows whether it is set. It never prints
the value. Claude Code stores the value in the system keychain (macOS Keychain; a protected
credentials file on other platforms), never in `settings.json`, and passes it to sidekick's
server as `SIDEKICK_API_KEY`. There is nothing to export and no launch alias to maintain, and it
works however Claude Code is started: terminal, tmux, desktop app, IDE.

With the option filled in, no `config.json` is needed. A config with no `auth.method` is
treated as `api_key`.

**Path B: environment variable.** If you already export the key yourself, leave the option
blank and choose "API key (env var)" in setup. It asks for the variable's **name** (default
`GEMINI_API_KEY`), never the key. The variable must be set in the environment Claude Code is
started from. If both are present, the plugin option wins.

Setup re-prompts if a "name" looks like a key instead (starts with `AIza`, contains lowercase
letters, or is over 40 characters). The server makes the same check at startup.

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

`project` and `location` are omitted — they are not required by the Google AI Studio
(Developer API) endpoint. `default_model` is added only if you entered one.

**Positioning:**
- Use this method for personal use, quick setup, or when you don't have a GCP project
- ADC and Keychain methods remain first-class for team/enterprise use — they support larger
  quota tiers, org-managed access control, and service account rotation
- The API key is read at server startup and held in memory; it is never written to
  `config.json`
- This is the only method with web access (`web=true`, `web_tools.enabled`); see
  [tools.md](tools.md#web-access)

**Quota:** AI Studio rate limits depend on the model and on whether billing is enabled for the
key; check the current limits in AI Studio. Limits are generally lower than Vertex AI. When a
model is overloaded or out of quota (503/429), the bridge retries with backoff and then falls back
once to the newest Flash-Lite (see [configuration.md](configuration.md#choosing-a-model)).

---

## Troubleshooting

| Error message | Cause | Fix |
|---|---|---|
| `no ADC credentials found` | ADC not configured | `gcloud auth application-default login` |
| Token refresh / `invalid_grant` errors on a call | ADC refresh token expired or revoked | `gcloud auth application-default login` |
| `403 PERMISSION_DENIED: quota project not set` | ADC user credentials have no quota project | `gcloud auth application-default set-quota-project YOUR_PROJECT_ID` |
| `auth method is 'env' but no service-account key file is set` / `points at a file that does not exist` / `could not be loaded` | `env` method: no file picked and `GOOGLE_APPLICATION_CREDENTIALS` unset, a missing file, or a file google-auth cannot read. ADC is never used as a fallback | Pick the file on the **Service-account key file** row, then restart ([how](#change-settings--reconnect)); check the file's contents |
| `Keychain item not found` | Secret not stored | Re-run the `security add-generic-password` command |
| `not valid service account JSON` | Keychain value corrupted | Re-store the SA JSON key |
| `'security' CLI not found` | Not macOS | Keychain method is macOS-only |
| `no API key. The plugin's key option is empty and env var 'GEMINI_API_KEY' is not set` | Neither path A nor path B supplied a key | Paste the key on the **Gemini API key** row, then restart ([how](#change-settings--reconnect)) |
| `'api_key_env' in config.json contains what looks like an API key` | The key itself was put in `api_key_env` | Set `api_key_env` to the variable name, e.g. `"GEMINI_API_KEY"` |
| `'project' is required for Vertex AI auth methods` | `project` missing with `adc`/`env`/`keychain` | Add `project` to config.json, or switch to `api_key` |
| `Web access was requested but is unavailable` | `web=true` on a Vertex method | Use `api_key` for web access |
