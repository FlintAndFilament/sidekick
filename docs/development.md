# Development Guide

## Setup

```bash
git clone git@github.com:FlintAndFilament/sidekick.git
cd sidekick
uv sync --frozen --extra dev
uv tool install pre-commit
pre-commit install
pre-commit install --hook-type commit-msg
```

The `dev` extra installs pytest, pytest-asyncio, ruff, mypy, bandit and types-regex.
Python 3.11 or newer is required.

Always pass `--extra dev`. A plain `uv sync` makes `.venv` match the lock file without the
extras, so it uninstalls pytest, ruff and the rest. If the tests suddenly can't find pytest, run
`uv sync --frozen --extra dev` again.

## Running tests

```bash
.venv/bin/python -m pytest -q
```

Run pytest for the current count. Every test is mocked: no network calls, and the suite runs
in about 3 seconds.

## Running the plugin locally

```bash
claude --plugin-dir .
```

Runs the plugin straight from this checkout — no marketplace, no install step. `uv` builds the
plugin's own venv at `${CLAUDE_PLUGIN_DATA}/venv` on first launch and reuses it after. With no
auth set up, the server connects in setup mode and shows only `setup_status`. Set the key in
`/plugin` → Configure options or run `/sidekick:setup`, reconnect with `/mcp`, then call any
sidekick tool (e.g. `help`) to confirm the `mcp` server connected.

Whenever you add or change a dependency in `pyproject.toml`, run `uv lock`. `uv.lock` is
committed and the server launches with `uv run --frozen`, which refuses to resolve a stale
lock file — a dependency added to `pyproject.toml` without a matching `uv lock` is never
installed for the live server, even though `.venv` (installed separately, above) picks it up
fine for tests.

The sandbox root is the project root: `CLAUDE_PROJECT_DIR` when Claude Code sets it, otherwise
the server's working directory (#102). File tools switch off when that directory is your home
directory, `/`, or an ancestor of home.

After a restart, check the tool list with `help`, `list_models` or `list_sessions`. A resumed
Claude Code conversation can show tool schemas from before the restart.

### The setup CLI

`/sidekick:setup` drives `sidekick-setup`, a non-interactive CLI that prints JSON and never
parses free text as shell, JSON or Python. You can run it yourself:

```bash
.venv/bin/sidekick-setup status        # config path, current values, auth checks
.venv/bin/sidekick-setup write --auth-method api_key --key-source plugin
```

`write` merges into the existing `config.json` and writes atomically. Only a file it can't read
as JSON is replaced, after a timestamped `.bak` copy. `--config PATH` points either command at another file. `write --help` lists
the fields.

### Trying setup mode locally

Setup mode is what users see when config or auth is broken. To reach it without touching your
real config, use a throwaway `HOME`, an `api_key` config and no key:

```bash
(
  export HOME=$(mktemp -d)   # the subshell keeps your real HOME untouched
  .venv/bin/sidekick-setup write --auth-method api_key --key-source plugin
  printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
    | env -u SIDEKICK_API_KEY -u GEMINI_API_KEY .venv/bin/sidekick
)
```

The `initialize` reply carries the problem text as `instructions`, and `tools/list` returns
only `setup_status`. `tests/test_setup_mode.py` covers the same paths without a subprocess.

## Watching logs

The server writes to a daily log file (the 4 most recent are kept):

```bash
tail -f ~/.config/sidekick/logs/$(ls -t ~/.config/sidekick/logs/*.log | head -1 | xargs basename)
```

Set `SIDEKICK_LOG_LEVEL=DEBUG` before starting Claude Code for per-call detail. See [logging.md](logging.md) for full reference.

## Code quality

```bash
# Lint + format
.venv/bin/ruff check src tests
.venv/bin/ruff format src tests

# Types (strict mode, configured in pyproject.toml)
.venv/bin/mypy src

# Security scan
.venv/bin/bandit -c pyproject.toml -r src

# All pre-commit hooks
.venv/bin/pre-commit run --all-files
```

`ruff check src tests` and `ruff format --check src tests` are clean. The pre-commit ruff
rev matches the ruff in `uv.lock`; bump both together. `mypy src` reports 2 long-standing
errors in `auth.py` (`from_service_account_info` is untyped).

Before a promotion to `main`, also run:

```bash
claude plugin validate --strict .   # plugin and marketplace manifests
uv lock --check                     # uv.lock still matches pyproject.toml
```

The pre-commit hooks run ruff (lint with `--fix`, and format), bandit, semgrep
(`p/python`, `p/secrets`), and the standard safety hooks, including `detect-private-key` and
`no-commit-to-branch` for `main` and `develop`.

## CI

Three GitHub Actions workflows in `.github/workflows/`. All are security scans; none run the
tests.

| Workflow | Runs on | What it does |
|---|---|---|
| `semgrep.yml` | push to `develop` / `main`, every PR | Semgrep OSS (`p/python`, `p/secrets`, `p/owasp-top-ten`), fails on findings, uploads SARIF to the Security tab |
| `trivy.yml` | push to `develop` / `main`, every PR | Trivy filesystem scan for HIGH and CRITICAL CVEs, uploads SARIF to the Security tab |
| `dependency-review.yml` | PRs that change `pyproject.toml`, `uv.lock`, `requirements*.txt`, `setup.cfg` or `setup.py` | Blocks new dependencies with high-severity advisories |

Each job declares least-privilege permissions (#72).

## Adding a new tool

1. Create `src/sidekick/tools/{name}.py` following an existing tool file. Required: the
   file header docstring, a system prompt, `_WRITE` and `_ARTIFACTS` values, a `CAPABILITY`
   row, and `register(mcp, client, transcript, workspace)`. The body calls `call_gemini()`.

2. In `src/sidekick/tools/__init__.py`, import its `register` and `CAPABILITY`, add the
   capability to `CAPABILITIES`, and add the register function to `__all__`.

3. Call `register_{name}(mcp, client, transcript, workspace)` in
   `src/sidekick/server.py`.

4. Add tests to `tests/test_tools.py`: at minimum, the tool registers, and `call_gemini()`
   errors pass through. `tests/test_capability_metadata.py` and `tests/test_guide.py` cover
   the advertised rows.

5. Document it in `docs/tools.md`.

6. Add the tool to the diagrams in `docs/diagrams/` (at least `components.mermaid`), and
   update every tool count: `docs/tools.md`, `docs/README.md`, `docs/architecture.md`,
   `components.mermaid` and the `server.py` header. The tool list in `guide.py`
   picks up a generating tool from `CAPABILITIES`; a tool without a capability row (like
   `list_models`) needs its own line there. To find the counts:
   `git grep -n -i -E '(8|eight) (registered )?tools'` (roadmap entries are history; leave them).

The server instructions and `help` pick up the new capability row by themselves:
`guide.py` builds its text from `CAPABILITIES`. Keep the instructions under
`INSTRUCTIONS_BUDGET` (2000 characters); Claude Code drops anything past about 2048.

## Branching workflow

git-flow. There is no `CLAUDE.md` in this repository; the rules are:

- `main` is production, `develop` is integration. No direct commits to either.
- Cut `feat/*`, `fix/*`, `chore/*` or `docs/*` branches off `develop`.
- Curate WIP commits into a few clean ones before landing, then merge into `develop` with
  `git merge --no-ff`. No squash merges, no rebase merges. Delete the branch after.
- No PR for landing on `develop`. PRs are only for `develop → main` promotions (and the rare
  emergency fix cherry-picked onto a branch off `main`), merged with `--no-ff`.
- Never merge `main` back into `develop`. After a promotion, fast-forward local `main` with
  `git checkout main && git pull --ff-only`.
- `--no-verify` is only for the `git merge --no-ff` step, which the `no-commit-to-branch`
  hook would otherwise block.
- Commits are Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`) and reference their
  issue: `refs #N` on work commits, `closes #N` on the one that finishes it.

## File header standard

Every `.py` file in `src/` begins with a module docstring: file path, one-line description,
Responsibilities, Design notes (SOLID callouts), Raises, Used by, Imports. Copy the shape of
an existing module.
