---
name: "bridge"
description: "Delegate tasks from Codex to Claude through detached bridge jobs. Use it for independent builds, fixes, diagnosis, research, review, or verification through Claude."
---

# Bridge: Codex -> Claude

Call `bridge status` at the start of a session to find existing work. There are exactly eight public commands: `run`, `wait`, `result`, `log`, `status`, `cancel`, `doctor`, `install`. Continue a thread with `run --resume`; garbage collection is automatic; request a review with a normal prompt.

Use `--write` for builds, fixes, diagnosis, and research. Reviews and verification without tests are read-only. Verification that runs tests needs `--write`, because caches may need writes. For Claude, this selects `acceptEdits`, not a permission bypass.

## Start and wait

1. Write the prompt to a scratch file with a regular file tool.
2. Do not use a heredoc, pipe, or stdin: execpolicy may not match the wrapped command, and `run` will refuse execution under `CODEX_SANDBOX`.
3. Invoke the bare name: `bridge run --agent claude --cwd "$PWD" [--write] --prompt-file F`, then retain the job ID.
4. Wait with `bridge wait ID --timeout 300`. On exit 111, repeat the same wait instead of starting a new job.
5. After a terminal outcome, run `bridge result ID`.

For work expected to finish within five minutes, use `bridge run --agent claude --cwd "$PWD" [--write] --wait 300 --prompt-file F`.

If a job is silent longer than expected, run `bridge status ID`, then `bridge log ID --tail 40`, and decide whether to cancel or wait. The default wait is limited to 50 minutes to provide feedback; from Codex, prefer short 300-second wait cycles.

To continue a thread, write the new turn to F and run `bridge run --agent claude --cwd "$PWD" [--write] --resume THREAD_ID --prompt-file F`. Use `thread_id` from `result --json`, not the bridge job ID.

Do not set model or effort without a reason; Claude account settings are used. If a model is required, pass its full ID rather than a local nickname.

## Outcomes

- Exit 0 with `ok`: read and verify the result.
- Exit 75 with `quota`: wait for the quota window; an immediate retry cannot help.
- Exit 111: the job is still `running`; repeat the wait.
- Exit 1 with `fail`, `died`, `cancelled`, or `timeout`: inspect the reason and log.
- `needs-review`: inspect possible partial changes manually.

Bridge neither synthesizes an answer nor proves its quality. Verify output and changes yourself. If `WARNING` follows the state line, inspect `bridge log ID --tail 40` first.

## Writing a Claude prompt

- Give one concrete task per run.
- State the role and working mode.
- Include `READ-FIRST` with exact paths in reading order.
- Provide observable facts and source context.
- Specify the exact target state.
- Define a verifiable completion criterion.
- List required tests and verification commands.
- State authority boundaries and allowed changes.
- List files and areas that must not change.
- Forbid commits and external mutations unless required.
- Request a compact final format covering files, checks, deviations, and unknowns.
- Require the agent not to guess and to verify the result.
