#!/usr/bin/env python3
"""Live smoke matrix for real Codex and Claude bridge jobs.

BRIDGE_SMOKE_FAKES=1 keeps the run hermetic: harnesses come from tests/fakes,
and checks whose evidence inherently needs a real model are reported as skip.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
FAKES = REPO_ROOT / "tests" / "fakes"
EXIT_SKIP = 77
JOB_ID_RE = re.compile(r"^(?:codex|claude)-\d{8}-\d{4}-[0-9a-f]{4}$")


class StepFailure(RuntimeError):
    """A smoke assertion failed, with a table-safe diagnostic."""


class StepSkip(RuntimeError):
    """The selected environment cannot prove this step."""


@dataclass(frozen=True)
class Command:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float


@dataclass(frozen=True)
class Step:
    name: str
    expectation: str
    run: Callable[[], str]


@dataclass(frozen=True)
class Row:
    name: str
    expectation: str
    fact: str
    outcome: str


def _one_line(value: object, limit: int = 500) -> str:
    return " ".join(str(value).split())[:limit]


def _cell(value: object) -> str:
    return _one_line(value).replace("|", "\\|")


class Smoke:
    def __init__(self, *, home: Path | None, keep: bool) -> None:
        self.fake = os.environ.get("BRIDGE_SMOKE_FAKES") == "1"
        self.keep = keep
        self.root = Path(tempfile.mkdtemp(prefix="bridge-smoke-"))
        self.home = home.expanduser().resolve() if home else self.root / "bridge-data"
        self.work = self.root / "work"
        self.prompts = self.root / "prompts"
        self.work.mkdir(mode=0o700)
        self.prompts.mkdir(mode=0o700)
        self.env = os.environ.copy()
        self.env.pop("BRIDGE_DATA", None)
        pythonpath = self.env.get("PYTHONPATH")
        self.env["PYTHONPATH"] = str(REPO_ROOT) + (
            os.pathsep + pythonpath if pythonpath else ""
        )
        if self.fake:
            for name in tuple(self.env):
                if name.startswith("CLAUDE") or name.startswith("CODEX"):
                    self.env.pop(name, None)
            fake_user_home = self.root / "fake-user-home"
            fake_user_home.mkdir(mode=0o700)
            self.env["HOME"] = str(fake_user_home)
            self.env.pop("TOOLS_DATA", None)
            self.env["PATH"] = str(FAKES) + os.pathsep + self.env.get("PATH", "")
            self.env["BRIDGE_POLL"] = "0.05"
            self.env["BRIDGE_WORKER_POLL"] = "0.05"
            self.env["BRIDGE_GRACE"] = "0.2"
        self._prompt_number = 0
        self._jobs: list[Path] = []
        self._codex_ro: tuple[str, Path] | None = None

    def close(self) -> None:
        self._cleanup_processes()
        if not self.keep:
            shutil.rmtree(self.root, ignore_errors=True)

    def missing_harnesses(self) -> list[str]:
        path = self.env.get("PATH", os.defpath)
        return [name for name in ("codex", "claude") if not shutil.which(name, path=path)]

    def steps(self) -> list[Step]:
        resume_expectation = (
            "fake: a new finished/ok job continues the same thread_id through resume argv"
            if self.fake else "exit 0, finished/ok, a new job continues thread_id and answers ok2"
        )
        return [
            Step("codex-ro", "exit 0, finished/ok, last.md, prompt absent from argv, files 0600",
                 self.step_codex_ro),
            Step("claude-ro", "exit 0, finished/ok, last.md, argv contains dontAsk",
                 self.step_claude_ro),
            Step("detach", "immediately running; 3s wait = 111; shared pgid, separate sid; then ok",
                 self.step_detach),
            Step("cancel", "cancel after 10s; group dead within 25s; cancelled/cli",
                 self.step_cancel),
            Step("parallel", "two concurrent Codex jobs appear in status and both finish ok",
                 self.step_parallel),
            Step("write-commit", "a write job creates a second Git commit with no warnings",
                 self.step_write_commit),
            Step("resume", resume_expectation, self.step_resume),
            Step("quota-text", "usage-limit text in a successful answer remains finished/ok",
                 self.step_quota_text),
            Step("big-prompt", "exactly 70000 bytes pass through prompt.md/stdin; finished/ok",
                 self.step_big_prompt),
            Step("execpolicy", "Codex runs bridge status outside the sandbox and returns the codex-ro ID",
                 self.step_execpolicy),
            Step("claude-from-codex", "Codex starts Claude through bridge and observes finished/ok",
                 self.step_claude_from_codex),
            Step("died", "SIGKILL on worker reports a live harness; killing the group yields died/1",
                 self.step_died),
        ]

    def _command(
        self,
        argv: list[str],
        *,
        timeout: float,
        env_extra: dict[str, str] | None = None,
        input_text: str | None = None,
        cwd: Path | None = None,
    ) -> Command:
        env = self.env.copy()
        if env_extra:
            env.update(env_extra)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd or REPO_ROOT),
                env=env,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            detail = _one_line(shlex.join(argv), 180)
            raise StepFailure(f"timeout after {timeout:g}s: {detail}") from error
        except OSError as error:
            raise StepFailure(
                f"could not start {_one_line(argv[0])}: {_one_line(error)}"
            ) from error
        return Command(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            time.monotonic() - started,
        )

    def _bridge(
        self,
        args: list[str],
        *,
        timeout: float = 270,
        env_extra: dict[str, str] | None = None,
    ) -> Command:
        return self._command(
            [sys.executable, "-m", "tool_bridge", "--home", str(self.home), *args],
            timeout=timeout,
            env_extra=env_extra,
        )

    @staticmethod
    def _require(condition: bool, fact: str) -> None:
        if not condition:
            raise StepFailure(fact)

    @staticmethod
    def _command_fact(command: Command) -> str:
        stdout = _one_line(command.stdout, 220) or "-"
        stderr = _one_line(command.stderr, 220) or "-"
        return f"exit {command.returncode}; stdout={stdout}; stderr={stderr}"

    def _require_exit(self, command: Command, expected: int) -> None:
        self._require(command.returncode == expected, self._command_fact(command))

    def _repo(self, name: str) -> Path:
        repo = self.work / name
        repo.mkdir(parents=True, mode=0o700)
        for args in (
            ["git", "init", "-q"],
            ["git", "config", "user.name", "Bridge Smoke"],
            ["git", "config", "user.email", "bridge-smoke@example.invalid"],
            ["git", "config", "commit.gpgsign", "false"],
            ["git", "config", "core.hooksPath", "/dev/null"],
        ):
            command = self._command(args, timeout=15, cwd=repo)
            self._require_exit(command, 0)
        (repo / "a.txt").write_text("first\n", "utf-8")
        for args in (["git", "add", "a.txt"], ["git", "commit", "-qm", "first"]):
            command = self._command(args, timeout=15, cwd=repo)
            self._require_exit(command, 0)
        return repo

    def _prompt(self, text: str) -> Path:
        self._prompt_number += 1
        path = self.prompts / f"prompt-{self._prompt_number}.md"
        path.write_text(text, "utf-8")
        path.chmod(0o600)
        return path

    def _run_job(
        self,
        *,
        agent: str,
        repo: Path,
        prompt: str,
        extra: list[str] | None = None,
        wait: float | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> tuple[str, Path, Command]:
        prompt_path = self._prompt(prompt)
        args = ["run", "--agent", agent, "--cwd", str(repo)]
        args.extend(extra or [])
        if wait is not None:
            args.extend(["--wait", str(wait)])
        args.extend(["--prompt-file", str(prompt_path)])
        timeout = (wait + 30) if wait is not None and wait > 0 else 30
        command = self._bridge(args, timeout=timeout, env_extra=env_extra)
        lines = [line.strip() for line in command.stdout.splitlines() if line.strip()]
        job_id = lines[0] if lines else ""
        self._require(bool(JOB_ID_RE.fullmatch(job_id)), self._command_fact(command))
        job = self.home / "jobs" / job_id
        self._jobs.append(job)
        return job_id, job, command

    @staticmethod
    def _json(path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StepFailure(f"cannot read {path.name}: {_one_line(error)}") from error
        if not isinstance(value, dict):
            raise StepFailure(f"{path.name}: expected a JSON object")
        return value

    def _finished_ok(self, job: Path, command: Command | None = None) -> dict[str, object]:
        if command is not None:
            self._require_exit(command, 0)
            self._require("finished/ok" in command.stdout, self._command_fact(command))
        result = self._json(job / "result.json")
        self._require(result.get("status") == "ok", f"result={_one_line(result)}")
        return result

    def _status(self, job_id: str) -> tuple[dict[str, object], Command]:
        command = self._bridge(["status", job_id, "--json"], timeout=15)
        self._require_exit(command, 0)
        try:
            value = json.loads(command.stdout)
        except json.JSONDecodeError as error:
            raise StepFailure(f"status is not JSON: {_one_line(command.stdout)}") from error
        self._require(isinstance(value, dict), f"status is not an object: {_one_line(value)}")
        return value, command

    def _status_all(self) -> list[dict[str, object]]:
        command = self._bridge(["status", "--json"], timeout=15)
        self._require_exit(command, 0)
        try:
            value = json.loads(command.stdout)
        except json.JSONDecodeError as error:
            raise StepFailure(f"status is not JSON: {_one_line(command.stdout)}") from error
        self._require(isinstance(value, list), f"status is not a list: {_one_line(value)}")
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _private_files(job: Path) -> tuple[bool, list[str]]:
        wrong: list[str] = []
        for path in job.rglob("*"):
            if path.is_file() and stat.S_IMODE(path.stat().st_mode) != 0o600:
                wrong.append(f"{path.name}:{stat.S_IMODE(path.stat().st_mode):04o}")
        return not wrong, wrong

    @staticmethod
    def _group_alive(pgid: int) -> bool:
        if pgid <= 1:
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _group_rows(pgid: int) -> tuple[list[int], str | None]:
        try:
            completed = subprocess.run(
                ["ps", "-o", "pid=", "-g", str(pgid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return [], _one_line(error)
        if completed.returncode != 0:
            stderr = _one_line(completed.stderr) or "stderr is empty"
            return [], f"exit {completed.returncode}: {stderr}"
        rows: list[int] = []
        for line in completed.stdout.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            rows.append(pid)
        return rows, None

    def _wait_group_members(
        self, pgid: int, count: int, timeout: float,
    ) -> tuple[list[int], str | None]:
        deadline = time.monotonic() + timeout
        rows: list[int] = []
        while time.monotonic() < deadline:
            rows, error = self._group_rows(pgid)
            if error is not None:
                return rows, error
            if len(rows) >= count:
                return rows, None
            time.sleep(0.05)
        return rows, None

    def _wait_group_dead(self, pgid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._group_alive(pgid):
                return True
            time.sleep(0.05)
        return not self._group_alive(pgid)

    def _terminate_group(self, pgid: int) -> None:
        if pgid <= 1 or pgid == os.getpgrp() or not self._group_alive(pgid):
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        if self._wait_group_dead(pgid, 1):
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self._wait_group_dead(pgid, 1)

    def _cleanup_processes(self) -> None:
        for job in self._jobs:
            if (job / "result.json").is_file():
                continue
            proc_path = job / "proc.json"
            if not proc_path.is_file():
                continue
            try:
                pgid = self._json(proc_path).get("pgid")
            except StepFailure:
                continue
            if type(pgid) is int:
                self._terminate_group(pgid)

    def _codex_ro_job(self) -> tuple[str, Path]:
        if self._codex_ro is not None:
            return self._codex_ro
        repo = self._repo("codex-ro")
        job_id, job, command = self._run_job(
            agent="codex",
            repo=repo,
            prompt="run ls and answer ok",
            wait=240,
            env_extra={"FAKE_MODE": "ok"} if self.fake else None,
        )
        self._codex_ro = (job_id, job)
        self._finished_ok(job, command)
        return job_id, job

    def step_codex_ro(self) -> str:
        job_id, job = self._codex_ro_job()
        request = self._json(job / "request.json")
        argv = request.get("argv")
        self._require(isinstance(argv, list), f"request.argv={_one_line(argv)}")
        self._require("run ls and answer ok" not in json.dumps(argv, ensure_ascii=False),
                      "prompt found in argv")
        last = job / "last.md"
        self._require(last.is_file() and bool(last.read_text("utf-8").strip()),
                      "last.md is missing or empty")
        private, wrong = self._private_files(job)
        self._require(private, "not mode 0600: " + ", ".join(wrong))
        result = self._json(job / "result.json")
        return f"{job_id}: finished/{result['status']}, last.md exists, argv clean, all files 0600"

    def step_claude_ro(self) -> str:
        repo = self._repo("claude-ro")
        prompt = "run ls and answer ok"
        job_id, job, command = self._run_job(
            agent="claude",
            repo=repo,
            prompt=prompt,
            wait=240,
            env_extra={"FAKE_MODE": "ok"} if self.fake else None,
        )
        self._finished_ok(job, command)
        request = self._json(job / "request.json")
        argv = request.get("argv")
        self._require(isinstance(argv, list) and "dontAsk" in argv,
                      f"dontAsk is missing: argv={_one_line(argv)}")
        self._require(prompt not in json.dumps(argv, ensure_ascii=False), "prompt found in argv")
        self._require((job / "last.md").is_file(), "last.md is missing")
        private, wrong = self._private_files(job)
        self._require(private, "not mode 0600: " + ", ".join(wrong))
        return f"{job_id}: finished/ok, dontAsk present, last.md exists, all files 0600"

    def step_detach(self) -> str:
        repo = self._repo("detach")
        fake_env = {"FAKE_MODE": "slow", "FAKE_SLEEP": "5"} if self.fake else None
        job_id, job, run = self._run_job(
            agent="codex",
            repo=repo,
            prompt="run sleep 20 and then answer ok",
            env_extra=fake_env,
        )
        self._require_exit(run, 0)
        state, _ = self._status(job_id)
        self._require(state.get("state") == "running", f"immediate state={_one_line(state)}")
        proc = self._json(job / "proc.json")
        pid, pgid, sid = proc.get("pid"), proc.get("pgid"), proc.get("sid")
        self._require(type(pid) is int and type(pgid) is int and type(sid) is int,
                      f"invalid proc.json: {_one_line(proc)}")
        try:
            worker_pgid = os.getpgid(pid)
        except OSError as error:
            raise StepFailure(f"os.getpgid({pid}): {_one_line(error)}") from error
        self._require(worker_pgid == pgid,
                      f"worker pid {pid} is in pgid {worker_pgid}, expected {pgid}")
        rows, ps_error = self._wait_group_members(pgid, 2, 5)
        self._require(ps_error is not None or len(rows) >= 2,
                      f"worker and harness are not both visible in pgid {pgid}: {rows}")
        self._require(sid != os.getsid(0), f"sid worker={sid}, sid smoke={os.getsid(0)}")
        short_wait = self._bridge(["wait", job_id, "--timeout", "3"], timeout=8)
        self._require(short_wait.returncode == 111, self._command_fact(short_wait))
        self._require(job_id in short_wait.stdout and "running" in short_wait.stdout,
                      f"state line is missing: {_one_line(short_wait.stdout)}")
        final_wait = self._bridge(
            ["wait", job_id, "--timeout", "240"],
            timeout=270,
        )
        self._finished_ok(job, final_wait)
        if ps_error is not None:
            raise StepSkip(
                "running, wait, and separate sid verified, but pgid members unavailable: " + ps_error
            )
        return (f"immediately running; wait 3s -> 111/running; pgid={pgid}, "
                f"members={len(rows)}, sid {sid}!={os.getsid(0)}; then finished/ok")

    def step_cancel(self) -> str:
        repo = self._repo("cancel")
        fake_env = {"FAKE_MODE": "slow", "FAKE_SLEEP": "90"} if self.fake else None
        job_id, job, run = self._run_job(
            agent="codex",
            repo=repo,
            prompt="run sleep 90",
            env_extra=fake_env,
        )
        self._require_exit(run, 0)
        proc = self._json(job / "proc.json")
        pgid = proc.get("pgid")
        self._require(type(pgid) is int, f"invalid pgid: {_one_line(proc)}")
        rows, ps_error = self._wait_group_members(pgid, 2, 5)
        events = job / "events.1.jsonl"
        deadline = time.monotonic() + 5
        while not events.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        self._require(events.is_file() and self._group_alive(pgid),
                      f"harness did not start in live pgid {pgid}")
        self._require(ps_error is not None or len(rows) >= 2,
                      f"harness did not start in pgid {pgid}: {rows}")
        time.sleep(0.2 if self.fake else 10)
        started = time.monotonic()
        cancelled = self._bridge(["cancel", job_id], timeout=30)
        self._require_exit(cancelled, 0)
        remaining = max(0.0, 25 - (time.monotonic() - started))
        dead = self._wait_group_dead(pgid, remaining)
        elapsed = time.monotonic() - started
        if not dead:
            leftovers, ps_error = self._group_rows(pgid)
            self._terminate_group(pgid)
            detail = ps_error or str(leftovers)
            raise StepFailure(f"pgid {pgid} is alive after {elapsed:.1f}s: {detail}")
        self._require(elapsed <= 25, f"pgid {pgid} died only after {elapsed:.1f}s")
        result = self._json(job / "result.json")
        self._require(result.get("status") == "cancelled" and result.get("reason") == "cli",
                      f"result={_one_line(result)}")
        return f"cancel exit 0; pgid {pgid} dead after {elapsed:.1f}s; cancelled/cli"

    def step_parallel(self) -> str:
        repo = self._repo("parallel")
        prompts = [self._prompt("run sleep 10 and answer ok") for _ in range(2)]
        env = self.env.copy()
        if self.fake:
            env.update({"FAKE_MODE": "slow", "FAKE_SLEEP": "3"})
        argv = [
            [sys.executable, "-m", "tool_bridge", "--home", str(self.home),
             "run", "--agent", "codex", "--cwd", str(repo),
             "--prompt-file", str(prompt)]
            for prompt in prompts
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=str(REPO_ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for command in argv
        ]
        commands: list[Command] = []
        for process in processes:
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.communicate()
                raise StepFailure("parallel: bridge run did not detach within 10s") from error
            commands.append(Command(process.returncode, stdout, stderr, 0))
        ids: list[str] = []
        for command in commands:
            self._require_exit(command, 0)
            job_id = command.stdout.strip().splitlines()[0] if command.stdout.strip() else ""
            self._require(bool(JOB_ID_RE.fullmatch(job_id)), self._command_fact(command))
            ids.append(job_id)
            self._jobs.append(self.home / "jobs" / job_id)
        self._require(ids[0] != ids[1], f"identical IDs: {ids}")
        statuses = {str(item.get("id")): item for item in self._status_all()}
        self._require(all(statuses.get(job_id, {}).get("state") == "running" for job_id in ids),
                      f"both jobs are not running: {_one_line({i: statuses.get(i) for i in ids})}")
        for job_id in ids:
            waited = self._bridge(["wait", job_id, "--timeout", "240"], timeout=270)
            self._finished_ok(self.home / "jobs" / job_id, waited)
        return f"distinct IDs {ids[0]}, {ids[1]}; status=running,running; then ok,ok"

    def step_write_commit(self) -> str:
        if self.fake:
            raise StepSkip("the fake harness does not modify a Git repository and cannot prove a commit")
        repo = self._repo("write-commit")
        job_id, job, command = self._run_job(
            agent="codex",
            repo=repo,
            prompt="append the line second to a.txt, run git commit -am second, and answer ok",
            extra=["--write"],
            wait=240,
        )
        result = self._finished_ok(job, command)
        count = self._command(["git", "rev-list", "--count", "HEAD"], timeout=15, cwd=repo)
        self._require_exit(count, 0)
        warnings = result.get("warnings")
        self._require(count.stdout.strip() == "2" and warnings == [],
                      f"commits={_one_line(count.stdout)}, warnings={_one_line(warnings)}")
        return f"{job_id}: 2 commits, finished/ok, warnings=[]"

    def step_resume(self) -> str:
        _base_id, base_job = self._codex_ro_job()
        base = self._json(base_job / "result.json")
        thread_id = base.get("thread_id")
        self._require(isinstance(thread_id, str) and bool(thread_id),
                      f"base job has no thread_id: {_one_line(base)}")
        base_request = self._json(base_job / "request.json")
        repo_value = base_request.get("cwd")
        self._require(isinstance(repo_value, str), f"base job has no cwd: {base_request}")
        repo = Path(repo_value)
        job_id, job, command = self._run_job(
            agent="codex",
            repo=repo,
            prompt="answer with exactly one word and no spaces: ok2",
            extra=["--resume", thread_id],
            wait=240,
            env_extra={"FAKE_MODE": "ok"} if self.fake else None,
        )
        self._finished_ok(job, command)
        result = self._json(job / "result.json")
        attempts = result.get("attempts")
        argv = attempts[0].get("argv") if isinstance(attempts, list) and attempts else None
        self._require(isinstance(argv, list) and "resume" in argv and thread_id in argv,
                      f"resume argv is invalid: {_one_line(argv)}")
        last = (job / "last.md").read_text("utf-8", errors="replace")
        if not self.fake:
            normalized = re.sub(r"\s+", "", last).casefold()
            self._require("ok2" in normalized,
                          f"response does not contain ok2: {_one_line(last)}")
            return f"{job_id}: continued {thread_id}, finished/ok, normalized response contains ok2"
        return f"{job_id}: fake continued {thread_id} through resume argv, finished/ok"

    def step_quota_text(self) -> str:
        repo = self._repo("quota-text")
        job_id, job, command = self._run_job(
            agent="codex",
            repo=repo,
            prompt="answer verbatim: usage limit reached, resets at noon",
            wait=240,
            env_extra={"FAKE_MODE": "limit-text-ok"} if self.fake else None,
        )
        self._finished_ok(job, command)
        last = (job / "last.md").read_text("utf-8", errors="replace")
        self._require("usage limit reached" in last.lower(), f"unexpected response: {_one_line(last)}")
        return f"{job_id}: exit 0, finished/ok with usage-limit text"

    def step_big_prompt(self) -> str:
        repo = self._repo("big-prompt")
        suffix = "\nanswer ok".encode("utf-8")
        pattern = b"padding-line\n"
        prefix_size = 70_000 - len(suffix)
        payload = (pattern * math.ceil(prefix_size / len(pattern)))[:prefix_size] + suffix
        self._require(len(payload) == 70_000, f"internal size error: {len(payload)}")
        seen = self.root / "fake-seen-big-prompt"
        fake_env = ({"FAKE_MODE": "ok", "FAKE_SEEN_PROMPT": str(seen)}
                    if self.fake else None)
        job_id, job, command = self._run_job(
            agent="codex",
            repo=repo,
            prompt=payload.decode("utf-8"),
            wait=240,
            env_extra=fake_env,
        )
        self._finished_ok(job, command)
        stored = (job / "prompt.md").read_bytes()
        self._require(stored == payload, f"prompt.md differs: {len(stored)} bytes")
        request = self._json(job / "request.json")
        argv = request.get("argv")
        self._require(isinstance(argv, list) and argv[-1:] == ["-"],
                      f"stdin marker is missing: {_one_line(argv)}")
        if self.fake:
            self._require(seen.is_file() and seen.read_bytes() == payload,
                          "fake harness received different 70000-byte content")
        return f"{job_id}: prompt.md={len(stored)} bytes, argv ends with '-', finished/ok"

    def _live_doors(self) -> tuple[str, Path]:
        if self.fake:
            raise StepSkip("BRIDGE_SMOKE_FAKES=1: live execpolicy is not executed")
        rule = Path.home() / ".codex" / "rules" / "bridge.rules"
        launcher = shutil.which("bridge", path=self.env.get("PATH", os.defpath))
        if not rule.is_file() or not launcher:
            missing = []
            if not rule.is_file():
                missing.append(str(rule))
            if not launcher:
                missing.append("bridge in PATH")
            raise StepSkip("missing " + ", ".join(missing))
        return launcher, rule

    def _codex_exec(self, prompt: str, repo: Path) -> Command:
        codex = shutil.which("codex", path=self.env.get("PATH", os.defpath))
        self._require(bool(codex), "codex disappeared from PATH")
        return self._command(
            [str(codex), "exec", "--sandbox", "workspace-write",
             "-c", "approval_policy=never", "-c", "notify=[]", "-"],
            timeout=300,
            input_text=prompt,
            cwd=repo,
        )

    def step_execpolicy(self) -> str:
        launcher, rule = self._live_doors()
        job_id, _job = self._codex_ro_job()
        repo = self._repo("execpolicy")
        bridge_command = shlex.join(["bridge", "--home", str(self.home), "status"])
        command = self._codex_exec(
            f"run exactly this command and answer with its output: {bridge_command}",
            repo,
        )
        combined = command.stdout + "\n" + command.stderr
        self._require(command.returncode == 0 and job_id in combined,
                      self._command_fact(command))
        return f"{Path(launcher).name}, {rule.name}: Codex returned ID {job_id}, exit 0"

    def step_claude_from_codex(self) -> str:
        launcher, rule = self._live_doors()
        repo = self._repo("claude-from-codex")
        inner_prompt = self._prompt("answer ok")
        bridge_command = shlex.join([
            "bridge", "--home", str(self.home), "run", "--agent", "claude",
            "--cwd", str(repo), "--wait", "120", "--prompt-file", str(inner_prompt),
        ])
        jobs_dir = self.home / "jobs"
        before = set(jobs_dir.iterdir()) if jobs_dir.is_dir() else set()
        try:
            command = self._codex_exec(
                f"run exactly this command and answer with its output: {bridge_command}",
                repo,
            )
        finally:
            after = set(jobs_dir.iterdir()) if jobs_dir.is_dir() else set()
            self._jobs.extend(sorted(after - before))
        combined = command.stdout + "\n" + command.stderr
        self._require(command.returncode == 0 and "finished/ok" in combined,
                      self._command_fact(command))
        return f"{Path(launcher).name}, {rule.name}: nested Claude returned finished/ok"

    def step_died(self) -> str:
        repo = self._repo("died")
        fake_env = {"FAKE_MODE": "slow", "FAKE_SLEEP": "60"} if self.fake else None
        job_id, job, run = self._run_job(
            agent="codex",
            repo=repo,
            prompt="run sleep 60",
            env_extra=fake_env,
        )
        self._require_exit(run, 0)
        proc = self._json(job / "proc.json")
        pid, pgid = proc.get("pid"), proc.get("pgid")
        self._require(type(pid) is int and type(pgid) is int,
                      f"invalid proc.json: {_one_line(proc)}")
        try:
            worker_pgid = os.getpgid(pid)
        except OSError as error:
            raise StepFailure(f"os.getpgid({pid}): {_one_line(error)}") from error
        self._require(worker_pgid == pgid,
                      f"worker pid {pid} is in pgid {worker_pgid}, expected {pgid}")
        rows, ps_error = self._wait_group_members(pgid, 2, 5)
        if ps_error is not None:
            raise StepSkip("pgid member list unavailable before SIGKILL: " + ps_error)
        self._require(len(rows) >= 2, f"harness did not start in pgid {pgid}: {rows}")
        proc = self._json(job / "proc.json")
        harness_pid = proc.get("harness_pid")
        self._require(type(harness_pid) is int and harness_pid in rows,
                      f"harness_pid not found in pgid {pgid}: proc={proc}, pids={rows}")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as error:
            raise StepFailure(f"SIGKILL worker {pid}: {_one_line(error)}") from error
        deadline = time.monotonic() + 5
        state: dict[str, object] = {}
        while time.monotonic() < deadline:
            state, _ = self._status(job_id)
            if state.get("worker_died"):
                break
            time.sleep(0.1)
        marker = f"worker-died, harness alive pid {harness_pid}"
        if state.get("state") != "running" or state.get("reason") != marker:
            leftovers, list_error = self._group_rows(pgid)
            self._terminate_group(pgid)
            detail = list_error or str(leftovers)
            raise StepFailure(
                f"after worker SIGKILL state={_one_line(state)}, pgid={detail}")
        waited = self._bridge(["wait", job_id, "--timeout", "5"], timeout=10)
        self._require(waited.returncode == 111 and marker in waited.stdout and waited.elapsed < 2,
                      self._command_fact(waited))
        self._terminate_group(pgid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state, _ = self._status(job_id)
            if state.get("state") == "died":
                break
            time.sleep(0.1)
        self._require(state.get("state") == "died", f"state after group kill={state}")
        dead_wait = self._bridge(["wait", job_id, "--timeout", "5"], timeout=10)
        self._require(dead_wait.returncode == 1 and "died" in dead_wait.stdout,
                      self._command_fact(dead_wait))
        return (f"worker pid {pid} killed: {marker}, wait 111; "
                f"after killing pgid {pgid}: died, wait 1")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Live tool-bridge smoke matrix")
    parser.add_argument("--home", type=Path, help="persistent bridge home instead of a temporary one")
    parser.add_argument("--only", metavar="STEP,...", help="run only the listed steps")
    parser.add_argument("--keep", action="store_true", help="keep temporary artifacts")
    return parser


def _render(rows: list[Row]) -> str:
    lines = [
        "| Step | Expectation | Observation | Outcome |",
        "|---|---|---|---|",
    ]
    lines.extend(
        f"| `{_cell(row.name)}` | {_cell(row.expectation)} | {_cell(row.fact)} | {row.outcome} |"
        for row in rows
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    smoke = Smoke(home=args.home, keep=args.keep)
    try:
        missing = smoke.missing_harnesses()
        if missing:
            print("skip: missing from PATH: " + ", ".join(missing))
            return EXIT_SKIP
        steps = smoke.steps()
        known = {step.name for step in steps}
        selected = known
        if args.only is not None:
            requested = {item.strip() for item in args.only.split(",") if item.strip()}
            unknown = sorted(requested - known)
            if unknown:
                print("unknown steps: " + ", ".join(unknown))
                return 1
            if not requested:
                print("no steps selected")
                return 1
            selected = requested
        rows: list[Row] = []
        for step in steps:
            if step.name not in selected:
                continue
            try:
                fact = step.run()
            except StepSkip as error:
                rows.append(Row(step.name, step.expectation, _one_line(error), "skip"))
            except Exception as error:
                detail = _one_line(error) or error.__class__.__name__
                rows.append(Row(step.name, step.expectation, detail, "fail"))
            else:
                rows.append(Row(step.name, step.expectation, fact, "ok"))
        print(_render(rows))
        if args.keep:
            print(f"\nArtifacts: {smoke.root}")
        return 1 if any(row.outcome == "fail" for row in rows) else 0
    finally:
        smoke.close()


if __name__ == "__main__":
    raise SystemExit(main())
