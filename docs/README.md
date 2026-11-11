# sidekick Documentation

| Document | Contents |
|---|---|
| [architecture.md](architecture.md) | Component diagram, module map, request flow of a generating call, session and transcript lifecycle |
| [auth.md](auth.md) | Setup paths A/B/C (plugin key option, env var, Google Cloud Agent Platform, formerly Vertex AI), changing settings and reconnecting, updating, each auth method, troubleshooting |
| [configuration.md](configuration.md) | Full config.json field reference |
| [tools.md](tools.md) | All 8 tools (5 generating + `list_models` + `list_sessions` + `help`; in setup mode only `setup_status`): system prompts, parameters, file and web access, examples, when to use each |
| [transcripts.md](transcripts.md) | Transcript format, session boundary behavior, location strategy |
| [logging.md](logging.md) | Log file location, levels, tail command, rotation, debug mode |
| [development.md](development.md) | Dev setup, running the plugin locally (`claude --plugin-dir .`), tests, lint and types, CI workflows, adding a new tool, branching workflow |
| [roadmap.md](roadmap.md) | Features by release, with rationale and shipped status |
| [diagrams/](diagrams/) | Every diagram, as a standalone Mermaid file: [request-flow](diagrams/request-flow.mermaid) (one call, end to end) · [components](diagrams/components.mermaid) (modules and tools) · [generating-call](diagrams/generating-call.mermaid) (one call through the modules) · [model-resolution](diagrams/model-resolution.mermaid) (`model=` to a concrete id) · [list-models](diagrams/list-models.mermaid) (the `list_models` flow) · [startup](diagrams/startup.mermaid) (startup and setup mode) · [auth-resolution](diagrams/auth-resolution.mermaid) (which credential is used) · [setup-flow](diagrams/setup-flow.mermaid) (`/sidekick:setup` paths) · [file-sandbox](diagrams/file-sandbox.mermaid) (Gemini's file access) · [tool-loop](diagrams/tool-loop.mermaid) (function-call rounds) |
