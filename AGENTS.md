# AGENTS.md - tool-bridge

## Purpose and boundaries

This repository contains a file-based bridge that runs Claude Code and Codex CLI
tasks as detached, observable jobs. One run creates one private job directory and one
detached worker. Callers can disconnect, inspect progress, cancel the process group,
and collect the final answer without an in-memory broker.

The bridge is not a scheduler, queue, daemon, multi-step orchestrator, MCP server, or
permission-bypass mechanism. It transports results but does not judge their quality.
The package is `tool_bridge`, and the console entry point is `bridge`.

Job state, logs, prompts, receipts, and configuration live outside the checkout in a
caller-configurable data home. Resolution uses an explicit home first, then
BRIDGE_DATA, then TOOLS_DATA, then the documented user-home fallback. Authentication
and account state belong to installed harnesses; the bridge accepts no provider key.

## Reading order

1. Read `README.md` for commands, quick start, boundaries, and limitations.
2. Read `docs/DESIGN.md` for state derivation, launch, retries, cancellation,
   installation, and security invariants.
3. Read `tests/test_core.py` and `tests/conftest.py` as the executable specification.
4. Read `deploy/skills/` only when changing installation artifacts.

## Verification

Run the complete hermetic suite from the repository root before reporting success:

```bash
python3 -m pytest -q -p no:cacheprovider
```

Report the observed result. Changes to detachment, cancellation, retries, harness
arguments, or installation may also need the fake-harness matrix in `smoke/run.py`.
State explicitly when a live harness property was not exercised.

## Working rules

- Tests must not contact the network or real harnesses, read the user's actual home
  directory, or write outside temporary paths. Use `tests/fakes/` for harnesses.
- Never commit prompts, job data, receipts, logs, credentials, account state, private
  hostnames, signed URLs, or developer-machine absolute paths.
- State is derived from durable job files plus current-boot process liveness. There is
  no registry or stored running flag. Terminal results are first-writer-wins, and a
  stall is only a label on a live job.
- Prompts travel through a private file and harness stdin, never through process
  arguments.
- Read-only is the default. Write mode must stay constrained to the selected
  workspace. Never add unrestricted access or permission-bypass modes.
- Classification reads structured harness errors before text patterns. Quota is not
  retried; transient failures have bounded retries; unknown failures after possible
  write effects require review rather than automatic retry.
- Cancellation targets only the recorded process group after liveness and boot
  identity checks, and the cancel marker is checked before every attempt.
- Configuration fails closed on unknown fields, invalid types, and non-absolute
  writable roots.
- `tool_bridge/shared/` is vendored support code owned by this repository. Keep
  bridge-specific logic outside it; explain deliberate forks in the commit message.
- No neighboring repository is imported. Other tools are invoked as CLI processes.

## Contract changes

The eight public commands, exit codes, shared machine-readable status and result
fields, job-file names, state derivation, permission modes, retry classification, and
installation artifacts are public contracts. Update `README.md`, `docs/DESIGN.md`,
and tests in the same change. A new command, daemon, queue, or MCP facade is a scope
change rather than a refactor.

## Style

Use English for code, comments, documentation, diagnostics, and commit messages. Use
no emoji or dash characters in place of a plain hyphen. Keep commit subjects short
and imperative; use the body to explain why. Add complexity only for a current need.
