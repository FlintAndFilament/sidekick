# Logging

sidekick's MCP server writes structured logs to a daily file. Claude Code swallows MCP server
stderr — the file is the only way to see what the server is doing.

## Log file location

```
~/.config/sidekick/logs/YYYYMMDD-sidekick.log
```

- One file per calendar day, named by the day the server started
- Multiple Claude Code sessions on the same day append to the same file
- Startup entries appear each time the server process starts (when Claude Code launches it)
- Logging is set up at the start of `main()`, never at import: importing `sidekick.__main__`
  (tests, tooling) creates, prunes and writes nothing (#144)
- At startup, all but the four newest log files are deleted, then today's file is opened — so
  there are four or five files at any time
- The same lines also go to stderr, which is useful only when running the server by hand
- The bridge's own loggers (`sidekick.*`) are written at `SIDEKICK_LOG_LEVEL`. The libraries
  (`google`, `google_genai`, `httpx`, `httpcore`, `urllib3`) are capped at WARNING and above
  unless the level is `DEBUG` (#88)
- Records carrying a google-auth HTTP payload (`httpRequest`/`httpResponse`, which hold raw
  request headers and bodies) are dropped before reaching either handler, so no bearer token or
  service-account assertion can land in the log file (#98)
- `DEBUG` raises library verbosity considerably — useful for troubleshooting, not for leaving on

## Tail live

```bash
tail -f ~/.config/sidekick/logs/$(ls -t ~/.config/sidekick/logs/*.log | head -1 | xargs basename)
```

## Log levels

Set via `SIDEKICK_LOG_LEVEL` environment variable before starting Claude Code
(`DEBUG`, `INFO`, `WARNING`, `ERROR`; case-insensitive). Default: `INFO`; an unrecognized value
also means `INFO`. Use only real level names: a value that names some other attribute of
Python's `logging` module (e.g. `BASIC_FORMAT`) raises before the server starts, so not even
setup mode comes up.

| Level | What you see | When to use |
|---|---|---|
| `INFO` | Startup: resolved latest Gemini models, file-tools state with sandbox root and artifacts directory, primary backend, default model and Gemini state (`on`, `off`, `unavailable`), transcript path. Also a file tool that raised, and a grounding redirect that did not resolve | Default — daily use |
| `WARNING` | Retries on 503/429 and falling back to the fallback model; thinking-level adjustments; web access requested on Google Cloud Agent Platform (formerly Vertex AI); a model name that doesn't suit the backend (a preview id on `api_key`, a `-latest` alias on Agent Platform); `inference failed …` before a fallback or retry decision; sandbox rejections; `grep` timeouts; tool-loop round cap; malformed function calls; empty Gemini responses; latest-model resolution failures; transcript write failures; `Gemini backend unavailable; serving OpenRouter only`; `OpenRouter catalog unavailable`; OpenRouter HTTP and transport retries; `elicitation unavailable` (sidekick could not ask the user); `accepted for <model> this session only`, `repository access granted for <model> this session only` and `Gemini data-use terms accepted for this session only` (an answer that could not be saved to `config.json`) | Included at INFO |
| `ERROR` | Startup failures (config, auth with method and source, `transcript_dir`, `artifacts_dir`, or an unexpected error with its traceback), after which the server stays up in setup mode; inference failures (with tool + session); preflight failures; fallback also failed; overload after a write (not retried); artifact not saved; `models.list` failures | Included at INFO |
| `DEBUG` | Per call: tool, session, model, backend, thinking level; request model, prompt length, number of file tools, web on/off; response length; credential loading; session-cache evictions. Also the libraries' own debug output, including one httpx line per HTTP request | Troubleshooting |

## Example log output

**Normal startup (INFO), Gemini with an API key, no OpenRouter key:**
```
[sidekick] 23:28:31 INFO     sidekick.__main__: latest models: flash=gemini-3.8-flash, pro=gemini-3.1-pro-preview, flash-lite=gemini-3.5-flash-lite
[sidekick] 23:28:31 INFO     sidekick.__main__: file tools enabled — sandbox root /Users/you/dev/my-project — artifacts → /Users/you/dev/my-project/sidekick-artifacts
[sidekick] 23:28:31 INFO     sidekick.__main__: starting — primary=gemini default_model=gemini-3.8-flash gemini=on
[sidekick] 23:28:31 INFO     sidekick.__main__: transcript → /Users/you/dev/my-project/session-summaries/20260921-2328-sidekick-transcript.md
```

If the model catalog can't be read, the first line reads `latest models: unresolved (pinned)`.
With an OpenRouter key and no Gemini set up, there is no `latest models` line and the
`starting` line reads `primary=openrouter … gemini=off` (`gemini=unavailable` when Gemini auth
failed). With file tools off, the file-tools line reads `file tools DISABLED (<reason>)`.

**Auth failure (ERROR):**
```
[sidekick] 17:50:10 ERROR    sidekick.auth: keychain item not found (service='sidekick', account='vertex-sa')
[sidekick] 17:50:10 ERROR    sidekick.__main__: startup failed — auth error: …
```

After a startup failure the server keeps running in setup mode: it offers only `setup_status`,
which repeats the problem to Claude (see [tools.md](tools.md#tools-reference)).

**Debug mode (DEBUG):**
```
[sidekick] 17:50:15 DEBUG    sidekick.tools.base: brainstorm session='default' model=default backend=gemini thinking=medium
[sidekick] 17:50:15 DEBUG    sidekick.client: ask: model=gemini-3.8-flash thinking=medium prompt_len=142 tools=5 web=False
[sidekick] 17:50:19 DEBUG    sidekick.client: response_len=847
[sidekick] 17:50:19 DEBUG    sidekick.tools.base: brainstorm session='default' OK
```

`model=default` on the first line means the call omitted `model`; the `ask:` line shows the
concrete model it resolved to. `tools=` counts the file tools offered to Gemini (4 read-only,
5 with `write_file`, 0 when file tools are off). The `ask:` and `response_len` lines are
Gemini's; an OpenRouter call logs only the `sidekick.tools.base` lines.

## Enabling debug mode

Add to your shell before launching Claude Code:
```bash
export SIDEKICK_LOG_LEVEL=DEBUG
claude
```

## Notes

- stdout is the MCP JSON-RPC protocol channel — any non-protocol byte on stdout silently
  corrupts the stream. All logging goes to stderr and the file, never stdout.
- The log file and transcript file are separate: the log captures server events; the transcript
  captures each exchange (prompts, tool calls, responses). See
  [transcripts.md](transcripts.md).
