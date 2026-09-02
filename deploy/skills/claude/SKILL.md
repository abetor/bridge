---
name: bridge
description: Delegate tasks from Claude Code to Codex through detached bridge jobs. Use it for independent builds, fixes, diagnosis, research, review, and verification through Codex.
user-invocable: true
---

# Bridge: Claude -> Codex

Use `--write` for builds, fixes, diagnosis, and research. Reviews and verification without tests are read-only. Verification that runs tests needs `--write`, because the sandbox may otherwise deny cache writes.

Call `bridge status` at the start of a session so an existing job is not lost. There are exactly eight public commands: `run`, `wait`, `result`, `log`, `status`, `cancel`, `doctor`, `install`. Continue a thread with `run --resume`; garbage collection is automatic; request a review with a normal prompt.

## Start and wait

1. Write the prompt to a scratch file with a regular file tool, not a heredoc.
2. Run `bridge run --agent codex --cwd "$PWD" [--write] --prompt-file F` and retain the job ID.
3. Wait according to the expected duration:
   - Up to 9 minutes, such as a question or small-diff review: use `bridge run ... --wait 540 --prompt-file F` in one call without a separate `wait`.
   - Longer builds, fixes, or research: start a background waiting subagent with a small model. Its prompt should require foreground `bridge wait ID --timeout 540`, retry exit 111 at most five times, and return the ID, exit code, and final 20 output lines unchanged. Continue other work while it waits.
   - Fallback: run foreground `bridge wait ID --timeout 540` with a shell timeout of 590000 ms and repeat on exit 111.
4. After a notification or completed wait, run `bridge result ID`. A notification or waiter report may contain only the exit code.

Do not put `bridge wait` in a background shell. Context compaction can terminate background shell tasks even though the detached bridge worker remains alive. Check `bridge status ID` and wait again. Never start a duplicate job merely because a waiter disappeared.

A waiter returns within 45 minutes, after five 540-second waits, with exit 111 if the job is still running. Start it again if needed. This keeps session feedback bounded. If `bridge status ID` reports a prolonged stall, inspect `bridge log ID --tail 40` and decide whether to run `bridge cancel ID` or keep waiting.

To continue a finished thread, write only the new turn to F and run `bridge run --agent codex --cwd "$PWD" [--write] --resume THREAD_ID --prompt-file F`. Use `thread_id` from `result --json`, not the bridge job ID.

Do not set a model without a reason; the account default is used. If a model is required, pass its full ID rather than a local nickname. Codex effort defaults to `xhigh` in bridge configuration.

## Outcomes

- Exit 0 with `ok`: read and verify the result.
- Exit 75 with `quota`: wait for the quota window; an immediate retry cannot help.
- Exit 111: the job is still `running`; repeat the wait.
- Exit 1 with `fail`, `died`, `cancelled`, or `timeout`: inspect the reason and log.
- `needs-review`: partial changes may exist; inspect the diff and do not retry blindly.

Bridge neither synthesizes nor validates the Codex answer. Verify output and changes yourself. If `WARNING` lines follow the state, inspect `bridge log ID --tail 40` first because writes or access may have been denied.

## Writing a Codex prompt

- Give one concrete task per run.
- State the role and mode: build, fix, diagnosis, research, or review.
- Start with `READ-FIRST` and exact paths in reading order.
- Provide observable facts or source context rather than assumptions.
- Specify the exact target state.
- Define a verifiable completion criterion.
- List required test and verification commands.
- State authority boundaries and allowed changes.
- List files and areas that must not change.
- Forbid commits, publication, and external mutations unless required.
- Request a compact final format covering files, checks, deviations, and unknowns.
- Require the agent not to guess when context is missing and to verify the result before replying.
