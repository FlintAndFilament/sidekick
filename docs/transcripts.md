# Transcripts

## File format

Each file is named `YYYYMMDD-HHMM-sidekick-transcript.md` using the **server startup timestamp**.
All tool calls within one Claude Code session (one server process) append to the same file.

**Per-exchange format:**

```markdown
## [14:32:07] review — thinking: medium | session: default

**Prompt:**
Review src/sidekick/client.py, the session cache in particular.

Focus: Is there a race condition in the session cleanup logic?

**Tool calls:**
- → read_file(path='src/sidekick/client.py') → 24.1 KiB
- ✗ read_file(path='.env') → rejected: path is denied by policy: .env
- → write_file(path='notes/review.md', content='# Review\n\n…') → wrote 1.2 KiB

**Response:**
Two problems, in order of severity...

---
```

- **Header:** time, tool name, the requested thinking level (the call's `thinking`, else
  `default_thinking`), and the `session_name`. The model is not recorded here; artifacts name it
  in their header.
- **Prompt:** the text sent to Gemini. Tools with two inputs combine them, e.g.
  `review` appends `Focus: <question>`, `architect` `Question: …`,
  `brainstorm` and `debug` `Context: …`.
- **Tool calls:** present only when Gemini called a tool. One line per call, in order (see below).
- **Response:** Gemini's answer, followed by the full sources footer when the call was
  web-grounded.

Only the five generating tools write entries. `list_models`, `list_sessions` and `help`
write nothing.

## Tool-call lines

Each line is `→` for a call that succeeded or `✗` for one that failed, the call with its
arguments (each argument cut at 60 characters with `…`), then a short summary:

| Line | Summary means |
|---|---|
| `→ list_dir() → 802 B` | Size of the result returned into Gemini's context |
| `→ write_file(path='x.txt', content='CANARY') → wrote 6 B` | Size of the file written (#77) |
| `✗ read_file(path='.env') → rejected: …` | The error Gemini got back |
| `→ google_search(query='…') → 2 source(s): google.com, android.com` | A web search, naming up to five sources by site title |
| `→ url_context(url='https://…') → retrieved` | A URL Gemini fetched; a failed fetch reads `✗ … → not retrieved (<status>)` |

Web searches and fetches run inside the Gemini API, not in the bridge, but they are recorded
here from the response's grounding metadata so every page that entered the context leaves a
trace. See [tools.md](tools.md#what-is-recorded).

If the requested model was overloaded (503/429) and the call was retried on the fallback model,
a marker line separates the two attempts:

```markdown
- → read_file(path='README.md') → 8.2 KiB
- — gemini-3.8-flash unavailable; retried on fallback model gemini-3.5-flash-lite —
- → read_file(path='README.md') → 8.2 KiB
```

## Web sources

A web-grounded response ends with the sources the bridge recorded from Google's grounding
metadata — the same footer the caller sees, but never truncated: the reply shows at most 30
sources, the transcript lists all of them.

```markdown
[sidekick] Sources the bridge recorded from Google's grounding metadata (these are not typed by Gemini):
1. google.com — https://aistudio.google.com/models/gemini-3
2. android.com — https://developer.android.com/ai/gemini
```

Search sources are resolved from Google's redirect links to the real page URL before logging; one
that could not be resolved keeps its redirect link, marked `(unresolved Google redirect)`. See
[tools.md](tools.md#what-is-recorded).

## What is not in the transcript

- Notices the bridge adds to the reply: the fallback-model notice, the "web access unavailable"
  notice, and the `artifact saved` / `artifact not saved` line.
- Failed calls in which Gemini made no tool call. A call that fails after at least one tool call
  is logged, with the error text as its response, so the calls it made are still on record.
- Anything from `list_models`, `list_sessions` or `help`.

## Session boundaries

A new transcript file is created each time the MCP server starts: each time Claude Code
starts, and each time the server is restarted with `/reload-plugins` or `/mcp` → Reconnect.
Consecutive Claude Code sessions produce separate files.

This means:
- Calling `brainstorm` then `review` in one Claude Code session → same file
- Restarting Claude Code, `/reload-plugins` or `/mcp` → Reconnect → new file, new timestamp,
  every in-memory session dropped
- Two servers started in the same minute with the same `transcript_dir` (e.g. two Claude Code
  windows in one project) share a file name, so both append to one file

Gemini conversations (`session_name`) live in server memory, not in the transcript. A restart
starts every session fresh even though the old transcript is still on disk.

## Transcript directory

**Default:** `./session-summaries` — resolved relative to the project root: `CLAUDE_PROJECT_DIR`
when Claude Code sets it, otherwise the server's working directory (#102). `~` is expanded.

Configured in `transcript_dir` in `~/.config/sidekick/config.json`.

The directory, including any missing parents, is created at startup if it doesn't exist. The
startup log names the full transcript path — see [logging.md](logging.md). If it can't be
created or used, the server starts in setup mode, offering only `setup_status`, which names the
directory and the fix.

**Per-project routing is automatic.** Open Claude Code on `~/dev/my-project` and transcripts
land in `~/dev/my-project/session-summaries/`. No config change needed when switching
projects — the server reads `CLAUDE_PROJECT_DIR` from Claude Code.

## Changing the transcript directory

To override the default and collect all transcripts in one place, edit
`~/.config/sidekick/config.json` and restart the server
([how](auth.md#change-settings--reconnect)):
```json
{"transcript_dir": "~/sidekick-transcripts"}
```

## Write failure behavior

If appending an entry fails (disk full, permissions error), a `transcript write failed: …`
warning goes to the log file and the tool call completes normally. Transcript failures never
surface to Claude or the user as tool errors.
