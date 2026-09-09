# bridge

`bridge` runs Claude Code and Codex CLI tasks as detached, observable jobs. A caller can disconnect, reconnect, inspect progress, cancel the whole process group, and retrieve the final answer without relying on an in-memory broker.

[Quick start](#quick-start) | [Offline demo](#demo) | [Architecture](docs/DESIGN.md) | [Tests](#tests) | [Contributing and agent guide](AGENTS.md) | [MIT license](LICENSE)

## Problem

Long agent CLI calls are fragile when they stay attached to an interactive shell. The caller may close, time out, or lose its context while the model process is still useful. Plain background shell jobs also make state, retries, cancellation, and partial failure difficult to inspect consistently.

## What it does

- Creates one private job directory per invocation.
- Starts a detached worker and keeps the agent harness in the same process group.
- Passes prompts through a mode-0600 file and harness stdin, never through process arguments.
- Derives `finished`, `running`, or `died` from durable files and process liveness.
- Supports bounded transient retries, thread resume, wall-time limits, cancellation, and compact logs.
- Exposes the same eight commands for both harnesses: `run`, `wait`, `result`, `log`, `status`, `cancel`, `doctor`, and `install`.
- Provides a common JSON shape for programmatic status and result consumers.

It is not a scheduler, queue, daemon, multi-step agent orchestrator, MCP server, or permission-bypass mechanism.

## Architecture

```text
caller
  |
  | bridge run / wait / status / result / cancel
  v
CLI -> private job directory -> detached worker -> Claude or Codex CLI
          |                        |
          | request, prompt        | events, stderr, final response
          v                        v
       durable files <-------- atomic terminal result
```

There is no shared mutable job registry. For a single job:

1. An atomic `result.json` means `finished`.
2. Without a result, a live process group from the current boot means `running`.
3. Otherwise the job is `died`.

Terminal results are first-writer-wins. Stall detection is a label, not a state transition. See [docs/DESIGN.md](docs/DESIGN.md) for process, retry, security, and storage details.

## Quick start

Requirements:

- Python 3.11 or newer.
- At least one installed and authenticated harness for jobs: `codex` or `claude`.
- Git and Python available on PATH. `doctor` currently checks both harnesses and
  returns 1 if either is absent, even when jobs for the installed harness can run.
- A writable data directory outside the repository.

The repository is `bridge`, the distribution is `tool-bridge`, the Python package is
`tool_bridge`, and the CLI is `bridge`. From a checkout:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
mkdir -p "${BRIDGE_DATA:-${TOOLS_DATA:-$HOME/tools-data}/bridge-data}"
python3 -m tool_bridge install
export PATH="$HOME/.local/bin:$PATH"
bridge doctor
```

`install` creates the launcher, installs one skill for each harness, and installs the Codex execpolicy rule. It does not modify Claude's `settings.json`; add the printed `Bash(bridge:*)` permission yourself if you choose that integration.

Start a read-only Codex job:

```bash
printf '%s\n' 'Inspect the repository and summarize its architecture.' > /tmp/bridge-prompt.md
bridge run --agent codex --cwd "$PWD" --prompt-file /tmp/bridge-prompt.md
# stdout: codex-YYYYMMDD-HHMM-abcd
bridge wait codex-YYYYMMDD-HHMM-abcd --timeout 300
bridge result codex-YYYYMMDD-HHMM-abcd
```

Enable constrained writes explicitly:

```bash
bridge run --agent claude --cwd "$PWD" --write --prompt-file /tmp/bridge-prompt.md
```

Use `run --resume THREAD_ID` to continue a harness thread. The thread ID comes from `result --json`; it is not the bridge job ID.

## Demo

The repository includes a hermetic smoke mode that uses local fake harnesses and creates all state under a temporary directory:

```bash
BRIDGE_SMOKE_FAKES=1 PYTHONDONTWRITEBYTECODE=1 python3 smoke/run.py
```

It exercises job creation, detachment, waiting, cancellation, parallel jobs, resume argv, large prompt transport, quota wording, and process-death handling. Steps that require real account-backed CLIs or execpolicy are reported as skips in fake mode.

The same runner can invoke real installed harnesses:

```bash
python3 smoke/run.py
```

That command uses authenticated external services and may consume subscription quota. Review the runner first and execute it only with accounts and repositories you are authorized to use. Do not attach raw receipts containing prompts, signed URLs, tokens, or local paths to public issues.

## Limitations

- The command name `bridge` may conflict with the Linux iproute2 utility. Resolve PATH ownership explicitly on affected systems.
- `last.md` may not exist after a very early harness death. `result` then falls back to saved events and stderr without inventing an answer.
- An unknown failure after write-side effects becomes `needs-review` and is not retried automatically.
- `install` is designed for a source checkout or editable installation because it installs repository-owned skill assets.
- Codex invocation through execpolicy is an explicit trust boundary: matched `bridge` commands run outside the caller's Codex sandbox, while the child harness still receives the configured read-only or workspace-write mode.
- Bridge transports and records answers but does not validate their quality.

## Data and credential boundary

Job data never uses the current working directory as its default home. Resolution order is:

1. Explicit global `--home DIR`.
2. `BRIDGE_DATA`.
3. `${TOOLS_DATA}/bridge-data`.
4. The documented fallback `~/tools-data/bridge-data`.

The fallback is resolved at runtime through `Path.home()` and can always be overridden. Job directories use mode 0700; prompts, events, stderr, and results use mode 0600.

Bridge does not accept or store provider API keys. It delegates authentication to the installed Claude and Codex CLIs. Before spawning a harness, it removes inherited `CLAUDE*` and `CODEX*` variables except `CODEX_HOME`. Keep OAuth state, tokens, downloaded data, logs, and local configuration outside the repository.

## Tests

Run the hermetic suite without bytecode or pytest cache files:

```bash
python3 -m pip install 'pytest>=8'
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

The tests replace `HOME`, cwd, credential-like sample variables, and harness binaries with temporary local fakes. They cover permissions, prompt transport, process-derived state, boot identity, retries, cancellation, output caps, JSON contracts, installation, execpolicy matching, and environment scrubbing.

This README documents the public commands and boundaries exercised by that suite.

## Provenance

This repository began as a public source snapshot of a personal tool. Earlier local development
history is not included.

## License

MIT. See [LICENSE](LICENSE).
