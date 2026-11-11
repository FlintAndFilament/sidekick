<h1 align="center">sidekick</h1>
<h4 align="center">Gemini as a live second opinion for Claude Code — reads your repo, searches the web with real sources, keeps sessions and transcripts.</h4>

<p align="center">
  <img alt="Version" src="https://img.shields.io/badge/version-0.5.0-blue.svg">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <img alt="MCP" src="https://img.shields.io/badge/MCP-1.28+-green.svg">
  <img alt="Tests" src="https://img.shields.io/badge/tests-677%20passing-brightgreen.svg">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-it-does">What it does</a> ·
  <a href="docs/tools.md">Tools</a> ·
  <a href="docs/auth.md">Auth</a> ·
  <a href="docs/configuration.md">Configuration</a> ·
  <a href="docs/architecture.md">Architecture</a> ·
  <a href="docs/transcripts.md">Transcripts</a> ·
  <a href="docs/logging.md">Logs</a> ·
  <a href="docs/development.md">Development</a> ·
  <a href="docs/roadmap.md">Roadmap</a> ·
  <a href="docs/README.md">All docs</a>
</p>

sidekick is a Claude Code plugin that gives Claude a live Gemini counterpart. When Claude hits a hard problem, such as an architecture decision, a tricky bug or a code review, it can ask Gemini for a second opinion without you switching tools or pasting context.

## Quick start

```
/plugin marketplace add FlintAndFilament/sidekick
/plugin install sidekick@sidekick
/sidekick:setup
```

- **Needs [uv](https://docs.astral.sh/uv/).** `/sidekick:setup` offers to install it, and uv brings its own Python.
- **Auth:** run `/sidekick:setup`. The quickest path is a free [Google AI Studio key](https://aistudio.google.com/apikey) pasted into the plugin's masked **Gemini API key** option. Claude Code keeps it in your system keychain, with no shell setup. An env var and Google Cloud Agent Platform (formerly Vertex AI) are also supported; see [docs/auth.md](docs/auth.md).
- **Update:** `claude plugin marketplace update sidekick && claude plugin update sidekick@sidekick`, then `/reload-plugins`.

Tested on macOS; Windows is not yet tested end to end.

## What it does

| Tool | Use it for | Can write files? |
|---|---|---|
| `ask` | A general question | no |
| `brainstorm` | Unconventional ideas and alternatives | yes |
| `review` | A critical, severity-first review of code, a design or a plan | yes |
| `debug` | Root-cause hypotheses and diagnostic steps | no |
| `architect` | Evaluating a system design, with explicit tradeoffs | yes |
| `list_models` | Lists the Gemini models on your backend | no |
| `list_sessions` | Lists the conversations in memory, to find one to continue | no |
| `help` | Full usage detail by topic, for Claude | no |

- **Reads your repo itself**, inside a sandbox, so Claude names paths instead of pasting files. Secrets and `.git` are always denied. → [Repository access](docs/tools.md#repository-access)
- **Searches the web with real sources** when a call passes `web=true`. It's off by default, and a web call can never write files. → [Web access](docs/tools.md#web-access)
- **Keeps conversations.** The same `session_name` continues one; a new name starts fresh. → [Tools](docs/tools.md)
- **Uses the newest model.** `flash`, `flash-lite` and `pro` track the newest release; an overloaded model falls back with a notice. → [Choosing a model](docs/configuration.md#choosing-a-model)
- **Leaves a record.** Every exchange goes into a Markdown transcript in your project, and `review` / `architect` answers are saved as files. → [Transcripts](docs/transcripts.md)
