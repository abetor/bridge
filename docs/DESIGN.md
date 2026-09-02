# Design

## Scope

`tool-bridge` is a local durability layer around account-backed Claude Code and Codex CLI invocations. It gives another process a stable command contract while leaving model selection, authentication, and answer quality to the harness.

The design deliberately excludes a daemon, central queue, scheduler, multi-turn planner, shared state database, and MCP facade. One `run` creates one isolated job and one detached worker.

## State model

There is no stored `running` flag and no global registry. State is derived from a job directory:

1. If `result.json` exists, the state is `finished` and its recorded outcome is authoritative.
2. Otherwise, if `proc.json` identifies a live process group from the current boot, the state is `running`.
3. Otherwise the state is `died`.

`result.json` is created atomically with exclusive publication. Competing terminal writers cannot overwrite the first result. Cancel and wall-time enforcement therefore cannot replace a result that completed at the same moment.

Stall detection only annotates a live job whose most recent event is old. It neither kills the job nor changes its state.

## Data layout

The data home is selected in this order:

1. Explicit `--home`.
2. `BRIDGE_DATA`.
3. `TOOLS_DATA` plus `bridge-data`.
4. Runtime fallback `Path.home() / "tools-data" / "bridge-data"`.

The current working directory is never a data-home fallback.

```text
bridge-data/
  config.toml
  jobs/
    <job-id>/
      request.json
      prompt.md
      proc.json
      events.1.jsonl
      stderr.1.log
      last.md
      cancel
      result.json
  logs/
    worker-<job-id>.log
```

Directories are mode 0700. Job files are mode 0600. Atomic writes use a sibling temporary file followed by `os.replace`; terminal result publication uses exclusive creation and directory sync where available.

## Launch and detachment

`run` validates the working directory, prompt, bridge configuration, and harness path before detachment. It skips harness binaries found only in per-user temporary shim directories when a stable PATH candidate exists. An explicitly configured absolute harness path has priority.

The CLI writes the private job directory and starts the worker with `start_new_session=True`. The worker receives `stdin=DEVNULL`. Each harness stays in the worker's process group, so loss of the caller does not terminate the job while cancellation can address the whole group.

The prompt is saved in `prompt.md` and opened directly as harness stdin. It is never included in process arguments. This supports large prompts without pipe backpressure and keeps process listings free of prompt content.

The worker removes inherited environment variables whose names start with `CLAUDE` or `CODEX`, except `CODEX_HOME`. This prevents nested-session and sandbox metadata from leaking into the child while retaining the harness's explicit home.

## Harness invocation

Codex is invoked as `codex exec --json -o <job>/last.md` or through `codex exec resume <thread-id>`. Notification hooks are disabled with `-c notify=[]`.

Claude is invoked as `claude -p --output-format stream-json`, with `--resume <session-id>` when continuing a thread.

Read-only is the default:

- Codex receives `sandbox_mode=read-only`.
- Claude receives `--permission-mode dontAsk` and read/web tools only.

With `--write`:

- Codex receives `workspace-write`, `approval_policy=never`, and writable roots containing at least `<cwd>/.git` plus explicitly configured additional roots.
- Claude receives `acceptEdits` and an explicit write-capable tool list.

Neither harness receives `danger-full-access` or `bypassPermissions`.

## Classification and retries

Structured Codex error codes take precedence over text patterns:

| Error class | Examples | Action |
|---|---|---|
| Quota | subscription usage exhausted | Stop, outcome `quota`, exit 75 |
| Transient | 429, capacity, 5xx, stream disconnect | Retry with backoff and resume when possible |
| Permanent | unauthorized, bad request, sandbox error, cyber policy | Stop, outcome `fail`, exit 1 |
| Unknown read-only | unclassified failure without side effects | Treat as transient |
| Unknown write | unclassified failure after file or command side effects | Stop as `needs-review` |

Rate limiting and subscription quota are separate because their remedies differ. A rate limit can improve after a short wait; exhausted quota requires waiting for its reset window.

By default, transient failures receive two retries after the original attempt, with 60- and 300-second backoffs plus jitter. If a thread or session ID is known, the next attempt resumes it. The cancel marker is checked before every attempt and during backoff. Exhausted transient retries produce `transient-exhausted`, not exit 111.

## Cancellation and wall time

`cancel ID` records its source, sends SIGTERM to the process group, waits for the configured grace period, sends SIGKILL if necessary, and verifies that the group disappeared. The job-level `max_time` uses the same mechanism with source `max-time`.

If the group is already dead and no result exists, cancellation reports `died` rather than claiming `cancelled`. If the worker dies but its harness child remains alive, derived state stays `running` with an explicit `worker-died, harness alive` reason so the operator can inspect or cancel the orphan.

Boot identity protects against process-ID reuse. Linux uses the boot ID. Darwin uses boot time rounded to seconds and permits a small correction window because NTP adjustments can move the reported value slightly during one boot.

## Output and machine contract

Human output is bounded to 12,000 characters unless `--full` is requested. Truncation points to the saved full artifact. `log --tail N` renders normalized events; `log --raw` exposes saved JSONL.

`status --json` and `result --json` share these top-level fields:

```text
id, agent, cwd, mode, state, outcome, reason, age_s,
attempts, thread_id, warnings, paths
```

Result output also contains a machine result and capped text. Status output contains a short preview and stall metadata.

Public exit codes are:

- 0 for a successful terminal outcome or successful information request.
- 75 for exhausted quota.
- 111 only when an explicit wait timeout expires while the job is still running.
- 1 for failure, death, cancellation, wall timeout, usage error, unknown ID, or corrupted required data.

## Installation boundary

`bridge install` owns four integration artifacts:

- A launcher, mode 0755.
- A Claude skill, mode 0600.
- A Codex skill, mode 0600.
- A Codex execpolicy rule, mode 0600.

Installation is idempotent and does not modify Claude `settings.json` or unrelated Codex rules. The rule matches the bare `bridge` command. A `run` that still sees `CODEX_SANDBOX` fails closed with instructions to use the bare name and a prompt file.

This execpolicy match is a trust boundary because the bridge process runs outside the caller's Codex sandbox. The boundary remains narrow through a fixed command set, required working directory, prompt-file transport, explicit write mode, and constrained child-harness permissions.

## Optional event hook

`on_event` is an optional shell command disabled by default. It receives a short environment-only completion envelope after the first terminal result is published. Hook failure is recorded in the worker log but cannot alter the job result or trigger a second notification.

## Security invariants

- Prompts are absent from argv and stored with mode 0600.
- Job state and logs live outside the repository.
- Harness authentication remains owned by the harness; bridge stores no provider keys.
- Inherited Claude and Codex session variables are scrubbed before spawn.
- Unknown write failures with possible side effects are never retried automatically.
- Cancellation targets the recorded process group only after liveness and boot checks.
- Configuration is fail-closed: unknown fields, invalid types, and non-absolute writable roots are errors.
