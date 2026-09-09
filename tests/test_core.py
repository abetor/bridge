"""Hermetic bridge-core contract tests using streaming CLI fakes."""
from __future__ import annotations

import json
import io
import os
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tool_bridge.cli as cli_module
import tool_bridge.jobs as jobs_module
from tool_bridge.cli import (
    EXIT_FAIL,
    EXIT_OK,
    EXIT_QUOTA,
    EXIT_TRANSIENT,
    REPO_ROOT,
    RULE_CONTENT,
    main,
)
from tool_bridge.harness import (
    STRUCTURED,
    build_argv,
    classify,
    parse_events,
    warnings_from,
)
from tool_bridge.jobs import (
    boot_id,
    create_json_once,
    derive_state,
    gc,
    new_job,
    now_iso,
    process_group_alive,
    read_proc,
    read_request,
    read_result,
    same_boot_id,
    write_json,
    write_result,
    write_text,
)
from tool_bridge.render import OUTPUT_CAP, render_log
from tool_bridge.worker import scrubbed_env


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    home = tmp_path / "bridge-data"
    cwd = tmp_path / "work"
    cwd.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("secret prompt text", "utf-8")
    monkeypatch.setenv("BRIDGE_POLL", "0.01")
    monkeypatch.setenv("BRIDGE_WORKER_POLL", "0.01")
    monkeypatch.setenv("BRIDGE_BACKOFF", "0.01,0.01")
    monkeypatch.setenv("BRIDGE_GRACE", "0.05")
    monkeypatch.setenv("FAKE_SLEEP", "0.1")
    return home, cwd, prompt


def _wait_path(path: Path, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"path did not appear: {path}"


def _start(bridge, monkeypatch, capsys, *, mode="ok", agent="codex", extra=None):
    home, cwd, prompt = bridge
    monkeypatch.setenv("FAKE_MODE", mode)
    argv = ["--home", str(home), "run", "--agent", agent, "--cwd", str(cwd)]
    argv += list(extra or [])
    argv += ["--prompt-file", str(prompt)]
    started = time.monotonic()
    assert main(argv) == EXIT_OK
    elapsed = time.monotonic() - started
    job_id = capsys.readouterr().out.strip().splitlines()[0]
    return job_id, home / "jobs" / job_id, elapsed


def _wait(job_id: str, home: Path, capsys, timeout=5):
    code = main(["--home", str(home), "wait", job_id, "--timeout", str(timeout)])
    output = capsys.readouterr().out
    return code, output


def test_help_has_eight_public_commands_and_hides_worker(capsys):
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    for command in (
            "run", "wait", "result", "log", "status", "cancel", "doctor", "install"):
        assert command in output
    assert "_worker" not in output
    assert "tool-bridge" not in output.splitlines()[0]

    expected = {
        "run": ("--write", "--wait", "--resume", "--max-time", "--full",
                "--json", "--prompt-file", "stdin"),
        "wait": ("ID", "--timeout", "use 0 forever", "--full"),
        "result": ("ID", "--full", "--json"),
        "log": ("ID", "--tail", "0 means the full log", "--raw"),
        "status": ("ID", "--json"),
        "cancel": ("ID",),
        "doctor": ("Inspect the environment",),
        "install": ("--bin-dir", "~/.local/bin"),
    }
    for command, fragments in expected.items():
        with pytest.raises(SystemExit) as command_help:
            main([command, "--help"])
        assert command_help.value.code == 0
        rendered = capsys.readouterr().out
        assert "--home" in rendered
        for fragment in fragments:
            assert fragment in rendered


def test_install_writes_owned_doors_checks_execpolicy_and_is_idempotent(
        tmp_path, monkeypatch, capsys):
    user_home = Path.home()
    settings = user_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"permissions":{"allow":["Bash(git:*)"]}}\n')
    before_settings = settings.read_bytes()
    foreign_rule = user_home / ".codex" / "rules" / "foreign.rules"
    foreign_rule.parent.mkdir(parents=True)
    foreign_rule.write_text("foreign\n", "utf-8")
    seen = tmp_path / "execpolicy.jsonl"
    monkeypatch.setenv("FAKE_SEEN_EXECPOLICY", str(seen))

    assert main(["install"]) == EXIT_OK
    first = capsys.readouterr().out
    assert "execpolicy decision: allow" in first
    assert '"Bash(bridge:*)"' in first
    assert settings.read_bytes() == before_settings
    assert foreign_rule.read_text("utf-8") == "foreign\n"

    targets = {
        "claude": user_home / ".claude" / "skills" / "bridge" / "SKILL.md",
        "codex": user_home / ".codex" / "skills" / "bridge" / "SKILL.md",
    }
    for agent, target in targets.items():
        source = REPO_ROOT / "deploy" / "skills" / agent / "SKILL.md"
        assert target.read_bytes() == source.read_bytes()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        skill = target.read_text("utf-8")
        assert ("`run`, `wait`, `result`, `log`, `status`, `cancel`, `doctor`, "
                "`install`") in skill
        assert "run --resume" in skill
        assert "bridge resume" not in skill and "bridge gc" not in skill
    rule = user_home / ".codex" / "rules" / "bridge.rules"
    assert rule.read_text("utf-8") == RULE_CONTENT
    assert stat.S_IMODE(rule.stat().st_mode) == 0o600
    launcher = user_home / ".local" / "bin" / "bridge"
    assert launcher.read_text("utf-8").startswith("#!/bin/sh\nexport PYTHONPATH=")
    expected_python = shlex.quote(str(Path(sys.executable).resolve()))
    assert f"exec {expected_python} -m tool_bridge \"$@\"" in launcher.read_text("utf-8")
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o755

    calls = [json.loads(line) for line in seen.read_text("utf-8").splitlines()]
    assert calls == [["execpolicy", "check", "--rules", str(rule), "bridge", "status"]]
    neighbor = subprocess.run(
        ["codex", "execpolicy", "check", "--rules", str(rule), "not-bridge", "status"],
        capture_output=True, text=True, check=False,
    )
    assert neighbor.returncode == 0
    assert json.loads(neighbor.stdout)["decision"] != "allow"

    launched = subprocess.run(
        [str(launcher), "--help"], capture_output=True, text=True, check=False,
    )
    assert launched.returncode == 0
    assert "{run,wait,result,log,status,cancel,doctor,install}" in launched.stdout

    assert main(["install"]) == EXIT_OK
    second = capsys.readouterr().out
    assert second.count("unchanged") == 4
    assert settings.read_bytes() == before_settings

    data_home = tmp_path / "bridge-data"
    data_home.mkdir()
    assert main(["--home", str(data_home), "doctor"]) == EXIT_FAIL
    missing_path = capsys.readouterr().out
    assert f"{launcher.parent} is not in PATH" in missing_path
    monkeypatch.setenv("PATH", str(launcher.parent) + os.pathsep + os.environ["PATH"])
    assert main(["--home", str(data_home), "doctor"]) == EXIT_OK
    doctor_output = capsys.readouterr().out
    assert "launcher:" in doctor_output and "in PATH, matches" in doctor_output
    assert "execpolicy rule:" in doctor_output and "(matches)" in doctor_output
    assert "skills: claude=matches, codex=matches" in doctor_output


def test_install_supports_custom_bin_dir(tmp_path, capsys):
    custom = tmp_path / "commands"
    assert main(["install", "--bin-dir", str(custom)]) == EXIT_OK
    capsys.readouterr()
    assert (custom / "bridge").is_file()
    assert not (Path.home() / ".local" / "bin" / "bridge").exists()


def test_run_job_dir_prompt_file_stdin_permissions_and_detachment(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    seen_prompt = home.parent / "seen-prompt"
    seen_argv = home.parent / "seen-argv"
    monkeypatch.setenv("FAKE_SEEN_PROMPT", str(seen_prompt))
    monkeypatch.setenv("FAKE_SEEN_ARGV", str(seen_argv))
    job_id, job, elapsed = _start(bridge, monkeypatch, capsys, mode="slow")
    assert elapsed < 1
    code, output = _wait(job_id, home, capsys)
    assert code == EXIT_OK
    assert output.strip().splitlines() == ["finished/ok", "ok"]
    request = read_request(job)
    proc = read_proc(job)
    assert request["wait_default"] == 3000
    assert Path(request["harness_argv"][0]).is_absolute()
    assert request["argv"][0] == request["harness_argv"][0]
    assert "--sandbox" in request["argv"] and request["argv"][-1] == "-"
    assert proc["sid"] != os.getsid(0)
    assert type(proc["harness_pid"]) is int
    assert seen_prompt.read_text("utf-8") == "secret prompt text"
    recorded = seen_argv.read_text("utf-8")
    assert "secret prompt text" not in recorded
    assert "secret prompt text" not in json.dumps(proc)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(job.stat().st_mode) == 0o700
    for name in ("request.json", "prompt.md", "proc.json", "events.1.jsonl",
                 "stderr.1.log", "result.json", "last.md"):
        assert stat.S_IMODE((job / name).stat().st_mode) == 0o600


def _harness_wrapper(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nexec '{target}' \"$@\"\n", "utf-8")
    path.chmod(0o755)


def test_binary_resolution_skips_temporary_path_shim(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    real_dir = Path(__file__).parent / "fakes"
    shim_dir = home.parent / "cmux-cli-shims" / "window"
    _harness_wrapper(shim_dir / "codex", real_dir / "codex")
    monkeypatch.setenv("TMPDIR", str(home.parent))
    monkeypatch.setenv("PATH", os.pathsep.join((str(shim_dir), str(real_dir), "/usr/bin", "/bin")))

    job_id, job, _ = _start(bridge, monkeypatch, capsys)
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    request = read_request(job)
    assert request["harness_argv"][0] == str((real_dir / "codex").resolve())
    assert request["warnings"] == []


def test_temporary_binary_warning_is_saved_and_doctor_reports_it(
        bridge, monkeypatch, capsys):
    home, _, _ = bridge
    shim_dir = home.parent / "cmux-cli-shims" / "window"
    _harness_wrapper(shim_dir / "codex", Path(__file__).parent / "fakes" / "codex")
    monkeypatch.setenv("TMPDIR", str(home.parent))
    monkeypatch.setenv("PATH", os.pathsep.join((str(shim_dir), "/usr/bin", "/bin")))

    job_id, job, _ = _start(bridge, monkeypatch, capsys)
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    warning = read_request(job)["warnings"][0]
    assert "only a temporary codex binary" in warning
    assert main(["--home", str(home), "doctor"]) == EXIT_FAIL
    assert "warning: " + warning in capsys.readouterr().out


def test_configured_absolute_harness_precedes_path_filter(bridge, monkeypatch, capsys):
    home, cwd, _ = bridge
    tools = home.parent / "tools-data"
    tools.mkdir()
    shim = home.parent / "configured" / "codex"
    _harness_wrapper(shim, Path(__file__).parent / "fakes" / "codex")
    (tools / "config.toml").write_text(
        "schema_version = 1\n"
        "[paths]\n"
        f'vault = "{cwd}"\n'
        f'topics_root = "{cwd}"\n'
        f'sources_root = "{cwd}"\n'
        "[harness.codex]\n"
        f'argv = ["{shim}"]\n',
        "utf-8",
    )
    monkeypatch.setenv("TOOLS_DATA", str(tools))
    monkeypatch.setenv("TMPDIR", str(home.parent))

    job_id, job, _ = _start(bridge, monkeypatch, capsys)
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    request = read_request(job)
    assert request["harness_argv"][0] == str(shim.resolve())
    assert request["warnings"] == []


@pytest.mark.parametrize("mode,resumed", [("capacity-then-ok", True), ("die-early", False)])
def test_transient_retry_and_resume(bridge, monkeypatch, capsys, mode, resumed):
    home, _, _ = bridge
    counter = home.parent / "counter"
    monkeypatch.setenv("FAKE_COUNTER", str(counter))
    job_id, job, _ = _start(bridge, monkeypatch, capsys, mode=mode)
    code, _ = _wait(job_id, home, capsys)
    assert code == EXIT_OK
    result = read_result(job)
    assert len(result["attempts"]) == 2
    assert (job / "events.1.jsonl").is_file()
    assert (job / "events.2.jsonl").is_file()
    second = result["attempts"][1]["argv"]
    assert (("resume" in second and "fake-thread" in second) is resumed)


@pytest.mark.parametrize("agent", ("codex", "claude"))
def test_structural_success_ignores_quota_words_in_answer(
        bridge, monkeypatch, capsys, agent):
    home, _, _ = bridge
    job_id, job, _ = _start(
        bridge, monkeypatch, capsys, mode="limit-text-ok", agent=agent)
    code, _ = _wait(job_id, home, capsys)
    result = read_result(job)
    assert code == EXIT_OK
    assert result["status"] == "ok"
    assert len(result["attempts"]) == 1


def test_codex_completed_turn_clears_recovered_stream_error(
        bridge, monkeypatch, capsys):
    home, _, _ = bridge
    job_id, job, _ = _start(bridge, monkeypatch, capsys, mode="recovered-stream")
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    result = read_result(job)
    assert result["status"] == "ok"
    assert len(result["attempts"]) == 1
    summary = parse_events(job / "events.1.jsonl")
    assert summary["error"] == ""
    assert summary["codex_error_info"] is None


def test_large_prompt_reaches_harness_without_pipe_deadlock(
        bridge, monkeypatch, capsys):
    home, _, prompt = bridge
    prompt.write_text("p" * 70_000, "utf-8")
    seen = home.parent / "large-prompt"
    monkeypatch.setenv("FAKE_SEEN_PROMPT", str(seen))
    job_id, job, _ = _start(bridge, monkeypatch, capsys)
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    assert read_result(job)["status"] == "ok"
    assert seen.stat().st_size == 70_000


def test_large_prompt_hang_still_obeys_max_time(bridge, monkeypatch, capsys):
    home, _, prompt = bridge
    prompt.write_text("p" * 70_000, "utf-8")
    job_id, job, _ = _start(
        bridge, monkeypatch, capsys, mode="hang", extra=["--max-time", "1"])
    assert _wait(job_id, home, capsys, timeout=3)[0] == EXIT_FAIL
    assert read_result(job)["status"] == "timeout"


@pytest.mark.parametrize(
    "mode,exit_code,status,reason",
    [
        ("quota", EXIT_QUOTA, "quota", "usage_limit_exceeded"),
        ("permanent", EXIT_FAIL, "fail", "bad_request"),
        ("cyber", EXIT_FAIL, "fail", "cyber_policy"),
    ],
)
def test_terminal_failures_do_not_retry(bridge, monkeypatch, capsys, mode, exit_code, status, reason):
    home, _, _ = bridge
    job_id, job, _ = _start(bridge, monkeypatch, capsys, mode=mode)
    code, output = _wait(job_id, home, capsys)
    assert code == exit_code
    result = read_result(job)
    assert result["status"] == status
    assert reason in result["reason"]
    assert len(result["attempts"]) == 1
    assert output.strip()
    assert reason in render_status_for(home, job_id, capsys)


def test_active_writer_resume_error_points_to_orphaned_harness(
        bridge, monkeypatch, capsys):
    home, _, _ = bridge
    job_id, job, _ = _start(
        bridge, monkeypatch, capsys, mode="active-writer",
        extra=["--resume", "busy-thread"],
    )
    code, output = _wait(job_id, home, capsys)
    result = read_result(job)
    assert code == EXIT_FAIL
    assert len(result["attempts"]) == 1
    assert "already has an active writer" in result["reason"]
    assert "check the original job with bridge status" in result["reason"]
    assert "bridge cancel ID before resuming" in output


def render_status_for(home: Path, job_id: str, capsys) -> str:
    assert main(["--home", str(home), "status", job_id]) == EXIT_OK
    return capsys.readouterr().out


def test_unknown_write_after_side_effect_needs_review(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    job_id, job, _ = _start(
        bridge, monkeypatch, capsys, mode="write-then-fail", extra=["--write"])
    code, _ = _wait(job_id, home, capsys)
    assert code == EXIT_FAIL
    result = read_result(job)
    assert result["reason"] == "needs-review"
    assert len(result["attempts"]) == 1
    assert main(["--home", str(home), "result", job_id]) == EXIT_OK
    assert capsys.readouterr().out.splitlines()[0] == "finished/fail needs-review"


def test_unknown_read_only_exhausts_retries(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    job_id, job, _ = _start(bridge, monkeypatch, capsys, mode="write-then-fail")
    code, _ = _wait(job_id, home, capsys)
    assert code == EXIT_FAIL
    result = read_result(job)
    assert result["reason"] == "transient-exhausted"
    assert len(result["attempts"]) == 3


def test_max_time_marks_timeout_and_kills_group(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    job_id, job, _ = _start(
        bridge, monkeypatch, capsys, mode="hang", extra=["--max-time", "0.15"])
    pgid = read_proc(job)["pgid"]
    code, _ = _wait(job_id, home, capsys)
    assert code == EXIT_FAIL
    result = read_result(job)
    assert result["status"] == "timeout"
    assert result["cancelled_by"] == "max-time"
    deadline = time.monotonic() + 2
    while process_group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not process_group_alive(pgid)


def test_cancel_escalates_to_sigkill_and_preserves_source(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    monkeypatch.setenv("FAKE_SLEEP", "30")
    job_id, job, _ = _start(bridge, monkeypatch, capsys, mode="ignore-term")
    _wait_path(job / "events.1.jsonl")
    pgid = read_proc(job)["pgid"]
    assert main(["--home", str(home), "cancel", job_id]) == EXIT_OK
    capsys.readouterr()
    result = read_result(job)
    assert result["status"] == "cancelled"
    assert result["cancelled_by"] == "cli"
    deadline = time.monotonic() + 2
    while process_group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not process_group_alive(pgid)


def test_cancel_during_retry_backoff(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    monkeypatch.setenv("BRIDGE_BACKOFF", "30,30")
    monkeypatch.setenv("BRIDGE_GRACE", "0.5")
    monkeypatch.setenv("FAKE_COUNTER", str(home.parent / "counter"))
    job_id, job, _ = _start(bridge, monkeypatch, capsys, mode="capacity-then-ok")
    _wait_path(job / "events.1.jsonl")
    deadline = time.monotonic() + 2
    while "model at capacity" not in (job / "events.1.jsonl").read_text("utf-8") \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    assert main(["--home", str(home), "cancel", job_id]) == EXIT_OK
    capsys.readouterr()
    result = read_result(job)
    assert result["status"] == "cancelled"
    assert len(result["attempts"]) == 1


def test_cancel_marker_before_first_attempt(tmp_path):
    home = tmp_path / "data"
    fake = Path(__file__).parent / "fakes" / "codex"
    _, job = new_job(home, {
        "agent": "codex", "cwd": str(tmp_path), "mode": "read-only",
        "model": None, "effort": "xhigh", "resume_id": None,
        "max_time": 30, "max_retries": 2, "backoff": [1, 1],
        "stall_after": 1200, "on_event": [], "codex_writable_roots": [],
        "harness_argv": [str(fake.resolve())], "prompt": "x",
    })
    create_json_once(job / "cancel", {"by": "cli", "at": "before"})
    result = subprocess.run(
        [sys.executable, "-m", "tool_bridge", "--home", str(home),
         "_worker", str(job)],
        cwd=Path(__file__).parents[1],
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0
    terminal = read_result(job)
    assert terminal["status"] == "cancelled"
    assert terminal["attempts"] == []
    assert not list(job.glob("events.*.jsonl"))


def test_state_boot_mismatch_and_missing_proc_are_died(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "boot_id", lambda: "current-boot")
    request = {"agent": "codex", "cwd": str(tmp_path), "mode": "read-only",
               "stall_after": 1200, "prompt": "x"}
    _, missing = new_job(tmp_path / "home", request)
    assert derive_state(missing)["state"] == "died"
    _, mismatch = new_job(tmp_path / "other", request)
    write_json(mismatch / "proc.json", {
        "pid": os.getpid(), "pgid": os.getpgrp(), "boot_id": "old-boot",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    state = derive_state(mismatch)
    assert state["state"] == "died"
    assert state["reason"] == "boot-id-mismatch"


def test_darwin_boot_id_tolerates_clock_correction_only_within_one_boot():
    assert same_boot_id("darwin:100", "darwin:130")
    assert not same_boot_id("darwin:100", "darwin:1000")
    assert not same_boot_id("linux:100", "darwin:100")


def test_unknown_boot_id_relies_on_live_process_group(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "boot_id", lambda: None)
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        _, job = new_job(tmp_path / "data", {
            "agent": "codex", "cwd": str(tmp_path), "mode": "read-only", "prompt": "x",
        })
        write_json(job / "proc.json", {
            "pid": process.pid, "pgid": process.pid, "boot_id": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
        assert derive_state(job)["state"] == "running"
    finally:
        process.terminate()
        process.wait(timeout=2)


def test_running_stall_is_only_a_label(tmp_path):
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        _, job = new_job(tmp_path / "data", {
            "agent": "codex", "cwd": str(tmp_path), "mode": "read-only",
            "stall_after": 1, "prompt": "x",
        })
        write_json(job / "proc.json", {
            "pid": process.pid, "pgid": process.pid, "boot_id": boot_id(),
            "started_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        })
        state = derive_state(job)
        assert state["state"] == "running"
        assert state["stalled_min"] is not None
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=2)


def test_cancel_dead_group_records_died(tmp_path, capsys):
    home = tmp_path / "data"
    received = tmp_path / "died-hook.json"
    hook = tmp_path / "died-hook.py"
    hook.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(os.environ)))\n",
        "utf-8",
    )
    command = " ".join((shlex.quote(sys.executable), shlex.quote(str(hook)),
                         shlex.quote(str(received))))
    job_id, job = new_job(home, {
        "agent": "codex", "cwd": str(tmp_path), "mode": "read-only", "prompt": "x",
        "on_event": command,
    })
    write_json(job / "proc.json", {
        "pid": 99_999_999, "pgid": 99_999_999, "boot_id": boot_id(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    assert main(["--home", str(home), "cancel", job_id]) == EXIT_FAIL
    capsys.readouterr()
    assert read_result(job)["status"] == "died"
    hook_env = json.loads(received.read_text("utf-8"))
    assert hook_env["BRIDGE_CLASS"] == "alert"
    assert hook_env["BRIDGE_KIND"] == "died"


def test_cancel_finished_job_reports_actual_outcome(tmp_path, capsys):
    home = tmp_path / "data"
    job_id, job = new_job(home, {
        "agent": "codex", "cwd": str(tmp_path), "mode": "read-only", "prompt": "x",
    })
    write_result(job, {"status": "ok", "reason": "completed", "finished_at": now_iso()})
    assert main(["--home", str(home), "cancel", job_id]) == EXIT_FAIL
    assert capsys.readouterr().out.strip() == f"{job_id} already finished: ok"


def test_cancel_losing_result_race_reports_actual_outcome(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / "data"
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        job_id, job = new_job(home, {
            "agent": "codex", "cwd": str(tmp_path), "mode": "read-only", "prompt": "x",
        })
        write_json(job / "proc.json", {
            "pid": process.pid, "pgid": process.pid, "boot_id": boot_id(),
            "started_at": now_iso(),
        })
        real_write_result = cli_module.write_result

        def lose_to_ok(target, value):
            if value.get("status") == "cancelled":
                real_write_result(target, {
                    "status": "ok", "reason": "completed", "finished_at": now_iso(),
                })
                return False
            return real_write_result(target, value)

        monkeypatch.setattr(cli_module, "write_result", lose_to_ok)
        assert main(["--home", str(home), "cancel", job_id]) == EXIT_FAIL
        assert capsys.readouterr().out.strip() == f"{job_id} already finished: ok"
        assert read_result(job)["status"] == "ok"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)


def test_wait_timeout_is_111_only_while_running(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    monkeypatch.setenv("FAKE_SLEEP", "0.25")
    job_id, _, _ = _start(bridge, monkeypatch, capsys, mode="slow")
    code, output = _wait(job_id, home, capsys, timeout=0.03)
    assert code == EXIT_TRANSIENT
    assert "running" in output
    code, output = _wait(job_id, home, capsys, timeout=2)
    assert code == EXIT_OK
    assert output.strip().splitlines() == ["finished/ok", "ok"]


def test_wait_timeout_renders_age_last_log_event_and_stall(
        bridge, monkeypatch, capsys):
    home, _, _ = bridge
    home.mkdir()
    (home / "config.toml").write_text("stall_after = 0\n", "utf-8")
    monkeypatch.setenv("FAKE_SLEEP", "2")
    job_id, _, _ = _start(bridge, monkeypatch, capsys, mode="slow")
    code, output = _wait(job_id, home, capsys, timeout=1)
    assert code == EXIT_TRANSIENT
    assert output.count("\n") == 1
    assert output.startswith(f"{job_id} running ")
    assert " · last event " in output
    assert ": thread: fake-thread" in output
    assert " · stalled 0m" in output
    assert main(["--home", str(home), "cancel", job_id]) == EXIT_OK
    capsys.readouterr()


def test_wait_and_run_wait_without_seconds_use_config_default(
        bridge, monkeypatch, capsys):
    home, cwd, prompt = bridge
    home.mkdir()
    (home / "config.toml").write_text("wait_default = 0.03\n", "utf-8")
    monkeypatch.setenv("FAKE_SLEEP", "0.2")

    job_id, _, _ = _start(bridge, monkeypatch, capsys, mode="slow")
    assert main(["--home", str(home), "wait", job_id]) == EXIT_TRANSIENT
    assert " running " in capsys.readouterr().out
    assert _wait(job_id, home, capsys, timeout=2)[0] == EXIT_OK

    monkeypatch.setenv("FAKE_MODE", "slow")
    code = main(["--home", str(home), "run", "--agent", "codex", "--cwd", str(cwd),
                 "--wait", "--prompt-file", str(prompt)])
    assert code == EXIT_TRANSIENT
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2 and lines[0].startswith("codex-") and " running " in lines[1]
    assert main(["--home", str(home), "cancel", lines[0]]) == EXIT_OK
    capsys.readouterr()


def test_wait_timeout_zero_is_infinite(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    monkeypatch.setenv("FAKE_SLEEP", "0.05")
    job_id, _, _ = _start(bridge, monkeypatch, capsys, mode="slow")
    code, output = _wait(job_id, home, capsys, timeout=0)
    assert code == EXIT_OK
    assert output.splitlines()[0] == "finished/ok"


def test_killed_worker_in_backoff_is_died_immediately(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    monkeypatch.setenv("BRIDGE_BACKOFF", "30,30")
    monkeypatch.setenv("FAKE_COUNTER", str(home.parent / "counter"))
    job_id, job, _ = _start(bridge, monkeypatch, capsys, mode="capacity-then-ok")
    _wait_path(job / "events.1.jsonl")
    deadline = time.monotonic() + 2
    while "model at capacity" not in (job / "events.1.jsonl").read_text("utf-8") \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    # The event is recorded while the harness is still exiting. Kill the worker
    # only once the harness is gone, otherwise wait correctly reports a live
    # harness with a dead worker, which is a transient state, not died.
    proc = read_proc(job)
    deadline = time.monotonic() + 2
    while jobs_module.process_in_group(proc.get("harness_pid"), proc["pgid"]) \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    os.kill(proc["pid"], signal.SIGKILL)
    started = time.monotonic()
    code, output = _wait(job_id, home, capsys, timeout=5)
    assert code == EXIT_FAIL, output
    assert "died" in output
    assert time.monotonic() - started < 1


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="zombie state is read from procfs")
def test_zombie_process_is_not_alive_in_its_group():
    # An exited child that nobody has reaped yet is a zombie: getpgid still
    # answers, but the process must not count as a live worker or harness.
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    deadline = time.monotonic() + 5
    while not jobs_module._is_zombie(child.pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert jobs_module._is_zombie(child.pid)
        assert not jobs_module.process_in_group(child.pid, child.pid)
    finally:
        child.wait()
    assert not jobs_module._is_zombie(child.pid)


def test_dead_worker_with_live_harness_is_reported_and_wait_does_not_block(
        tmp_path, capsys):
    home = tmp_path / "data"
    job_id, job = new_job(home, {
        "agent": "codex", "cwd": str(tmp_path), "mode": "read-only",
        "stall_after": 1200, "prompt": "x",
    })
    leader_code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen(['/bin/sleep','30']); "
        "print(child.pid, flush=True); time.sleep(30)"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert worker.stdout is not None
    harness_pid = int(worker.stdout.readline().strip())
    write_json(job / "proc.json", {
        "pid": worker.pid,
        "pgid": worker.pid,
        "harness_pid": harness_pid,
        "boot_id": boot_id(),
        "started_at": now_iso(),
    })
    write_text(job / "events.1.jsonl", json.dumps({
        "type": "thread.started", "thread_id": "busy-thread",
    }) + "\n")
    try:
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=2)
        expected = f"worker-died, harness alive pid {harness_pid}"
        state = derive_state(job)
        assert state["state"] == "running"
        assert state["reason"] == expected

        assert main(["--home", str(home), "status", job_id]) == EXIT_OK
        assert expected in capsys.readouterr().out

        started = time.monotonic()
        assert main(["--home", str(home), "wait", job_id, "--timeout", "5"]) \
            == EXIT_TRANSIENT
        assert time.monotonic() - started < 1
        assert expected in capsys.readouterr().out
    finally:
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_run_wait_returns_result_in_one_call(bridge, monkeypatch, capsys):
    home, cwd, prompt = bridge
    monkeypatch.setenv("FAKE_MODE", "ok")
    code = main(["--home", str(home), "run", "--agent", "codex", "--cwd", str(cwd),
                 "--wait", "2", "--prompt-file", str(prompt)])
    assert code == EXIT_OK
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1] == "ok"
    assert lines[0].startswith("codex-")


def test_result_cap_full_and_warning_first(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    job_id, job, _ = _start(bridge, monkeypatch, capsys, mode="long")
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    capsys.readouterr()
    assert main(["--home", str(home), "result", job_id]) == EXIT_OK
    short = capsys.readouterr().out.rstrip("\n")
    assert len(short) <= OUTPUT_CAP
    assert str(job / "last.md") in short
    assert main(["--home", str(home), "result", job_id, "--full"]) == EXIT_OK
    full = capsys.readouterr().out.rstrip("\n")
    assert full.startswith("finished/ok\n")
    assert len(full.split("\n", 1)[1]) == 30_000

    monkeypatch.setenv("FAKE_MODE", "denied")
    denied, denied_job, _ = _start(bridge, monkeypatch, capsys, mode="denied")
    assert _wait(denied, home, capsys)[0] == EXIT_OK
    assert read_result(denied_job)["warnings"]
    assert main(["--home", str(home), "result", denied]) == EXIT_OK
    assert capsys.readouterr().out.startswith("finished/ok\nWARNING:")


def test_successful_command_denial_text_is_not_a_warning(
        bridge, monkeypatch, capsys):
    home, _, _ = bridge
    job_id, job, _ = _start(
        bridge, monkeypatch, capsys, mode="successful-denied-output")
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    assert read_result(job)["warnings"] == []
    capsys.readouterr()
    assert main(["--home", str(home), "log", job_id]) == EXIT_OK
    rendered = capsys.readouterr().out
    assert "running: read docs -> exit 0" in rendered
    assert "denied" not in rendered.lower()


def test_log_renders_codex_and_claude_and_raw(tmp_path):
    _, job = new_job(tmp_path / "data", {
        "agent": "codex", "cwd": str(tmp_path), "mode": "read-only", "prompt": "x",
    })
    codex = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {
            "type": "command_execution", "command": "git status"}},
        {"type": "item.completed", "item": {
            "type": "command_execution", "command": "git status", "exit_code": 0}},
        {"type": "item.completed", "item": {
            "type": "command_execution", "command": "touch /locked", "exit_code": 1,
            "aggregated_output": "Operation not permitted"}},
        {"type": "item.completed", "item": {
            "type": "file_change", "changes": [{"path": "a.py"}]}},
        {"type": "item.completed", "item": {
            "type": "mcp_tool_call", "server": "github", "tool": "search"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}},
        {"type": "turn.completed"},
        {"type": "error", "message": "boom"},
    ]
    claude = [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "system", "subtype": "hook_started", "session_id": "s"},
        {"type": "rate_limit_event", "message": "rate limit"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 100}},
            {"type": "text", "text": "world"},
        ]}},
        {"type": "result", "subtype": "success", "result": "world", "is_error": False},
    ]
    raw = "\n".join(json.dumps(item) for item in codex) + "\n"
    write_text(job / "events.1.jsonl", raw)
    write_text(job / "stderr.1.log", "seatbelt operation not permitted\n")
    write_text(job / "events.2.jsonl", "\n".join(json.dumps(item) for item in claude) + "\n")
    rendered = render_log(job)
    assert warnings_from(parse_events(job / "events.1.jsonl"), "") == [
        "Operation not permitted",
    ]
    assert rendered.splitlines() == [
        "thread: t",
        "running: git status",
        "running: git status -> exit 0",
        "denied: Operation not permitted",
        "edited: a.py",
        "tool: github.search",
        "message: hello",
        "error: boom",
        "denied: seatbelt operation not permitted",
        'tool: Bash {"command":"' + "x" * 68,
        "message: world",
    ]
    assert render_log(job, tail=0) == rendered
    assert render_log(job, tail=1) == "message: world"
    raw_log = render_log(job, raw=True)
    assert json.loads(raw_log.splitlines()[0])["type"] == "thread.started"
    assert "rate_limit_event" in raw_log and "hook_started" in raw_log


def test_log_rejects_negative_tail(tmp_path, capsys):
    home = tmp_path / "data"
    job_id, _ = new_job(home, {
        "agent": "codex", "cwd": str(tmp_path), "mode": "read-only", "prompt": "x",
    })
    assert main(["--home", str(home), "log", job_id, "--tail", "-1"]) == EXIT_FAIL
    assert "--tail must be non-negative" in capsys.readouterr().err


def test_result_falls_back_to_event_without_terminal_file(tmp_path, capsys):
    home = tmp_path / "data"
    job_id, job = new_job(home, {
        "agent": "codex", "cwd": str(tmp_path), "mode": "read-only", "prompt": "x",
    })
    write_text(job / "events.1.jsonl", json.dumps({
        "type": "item.completed", "item": {"type": "agent_message", "text": "saved"},
    }) + "\n")
    assert main(["--home", str(home), "result", job_id]) == EXIT_OK
    assert capsys.readouterr().out.strip().splitlines() == ["died", "saved"]


def test_status_json_and_gc_only_old_finished(tmp_path, capsys):
    home = tmp_path / "data"
    request = {"agent": "codex", "cwd": str(tmp_path), "mode": "read-only", "prompt": "x"}
    old_id, old = new_job(home, request)
    old_time = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    write_result(old, {"status": "ok", "reason": "completed", "finished_at": old_time})
    fresh_id, fresh = new_job(home, request)
    write_result(fresh, {"status": "ok", "reason": "completed",
                         "finished_at": datetime.now(timezone.utc).isoformat()})
    died_id, died = new_job(home, request)
    running_process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        running_id, running = new_job(home, request)
        write_json(running / "proc.json", {
            "pid": running_process.pid, "pgid": running_process.pid,
            "boot_id": boot_id(), "started_at": datetime.now(timezone.utc).isoformat(),
        })
        assert gc(home, 14) == [old_id]
        assert not old.exists()
        assert fresh.exists() and died.exists() and running.exists()
        assert main(["--home", str(home), "status", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert {item["id"] for item in payload} == {fresh_id, died_id, running_id}
        common = {
            "id", "agent", "cwd", "mode", "state", "outcome", "reason", "age_s",
            "attempts", "thread_id", "warnings", "paths",
        }
        assert all(common <= set(item) for item in payload)
        assert {item["state"] for item in payload} == {"finished", "died", "running"}
        for job_id, expected_state in (
                (fresh_id, "finished"), (died_id, "died"), (running_id, "running")):
            assert main(["--home", str(home), "result", job_id, "--json"]) == EXIT_OK
            result_json = json.loads(capsys.readouterr().out)
            assert common <= set(result_json)
            assert result_json["state"] == expected_state
    finally:
        running_process.terminate()
        running_process.wait(timeout=2)


def test_status_keeps_good_jobs_when_one_directory_is_broken(tmp_path, capsys):
    home = tmp_path / "data"
    good_id, _ = new_job(home, {
        "agent": "codex", "cwd": str(tmp_path), "mode": "read-only", "prompt": "x",
    })
    broken = home / "jobs" / "codex-20260902-0000-bad0"
    broken.mkdir()

    assert main(["--home", str(home), "status"]) == EXIT_OK
    output = capsys.readouterr().out
    assert good_id in output
    assert f"{broken.name} broken " in output

    assert main(["--home", str(home), "status", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    by_id = {item["id"]: item for item in payload}
    assert by_id[broken.name]["state"] == "broken"
    assert good_id in by_id


def test_sandbox_guard_input_validation_and_json_run(bridge, monkeypatch, capsys):
    home, cwd, prompt = bridge
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    code = main(["--home", str(home), "run", "--agent", "claude", "--cwd", str(cwd),
                 "--prompt-file", str(prompt)])
    assert code == EXIT_FAIL
    assert "execpolicy" in capsys.readouterr().err
    assert not home.exists()
    monkeypatch.delenv("CODEX_SANDBOX")
    with pytest.raises(SystemExit) as error:
        main(["--home", str(home), "run", "--agent", "codex",
              "--prompt-file", str(prompt)])
    assert error.value.code == EXIT_FAIL
    capsys.readouterr()
    assert main(["--home", str(home), "run", "--agent", "codex", "--cwd", str(cwd),
                 "--json", "--prompt-file", str(prompt)]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"].startswith("codex-")
    assert set(payload) == {"id", "dir", "agent", "cwd", "mode"}
    assert payload["dir"] == str(home / "jobs" / payload["id"])
    assert payload["agent"] == "codex"
    assert payload["cwd"] == str(cwd)
    assert payload["mode"] == "read-only"
    assert _wait(payload["id"], home, capsys)[0] == EXIT_OK


def test_run_json_with_wait_matches_result_json(bridge, monkeypatch, capsys):
    home, cwd, prompt = bridge
    monkeypatch.setenv("FAKE_MODE", "ok")
    assert main([
        "--home", str(home), "run", "--agent", "codex", "--cwd", str(cwd),
        "--json", "--wait", "2", "--prompt-file", str(prompt),
    ]) == EXIT_OK
    waited = json.loads(capsys.readouterr().out)
    assert main(["--home", str(home), "result", waited["id"], "--json"]) == EXIT_OK
    direct = json.loads(capsys.readouterr().out)
    assert set(waited) == set(direct)
    for key in set(waited) - {"age_s"}:
        assert waited[key] == direct[key]
    assert abs(waited["age_s"] - direct["age_s"]) <= 1
    common = {
        "id", "agent", "cwd", "mode", "state", "outcome", "reason", "age_s",
        "attempts", "thread_id", "warnings", "paths",
    }
    assert common <= set(waited)
    assert waited["state"] == "finished"
    assert waited["outcome"] == "ok"
    assert waited["thread_id"] == "fake-thread"
    assert waited["text"].startswith("finished/ok\n")


def test_prompt_from_explicit_stdin_marker(bridge, monkeypatch, capsys):
    home, cwd, _ = bridge
    seen = home.parent / "stdin-prompt"
    monkeypatch.setenv("FAKE_SEEN_PROMPT", str(seen))
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.setattr("sys.stdin", io.StringIO("prompt from stdin"))
    assert main(["--home", str(home), "run", "--agent", "codex", "--cwd", str(cwd),
                 "-"]) == EXIT_OK
    job_id = capsys.readouterr().out.strip()
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    assert seen.read_text("utf-8") == "prompt from stdin"


def test_environment_scrub_keeps_only_codex_home(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    seen = home.parent / "env.json"
    monkeypatch.setenv("FAKE_SEEN_ENV", str(seen))
    monkeypatch.setenv("CLAUDECODE", "secret")
    monkeypatch.setenv("CODEX_CI", "secret")
    monkeypatch.setenv("CODEX_HOME", str(home.parent / "codex-home"))
    job_id, _, _ = _start(bridge, monkeypatch, capsys)
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    env = json.loads(seen.read_text("utf-8"))
    assert "CLAUDECODE" not in env
    assert "CODEX_CI" not in env
    assert env["CODEX_HOME"].endswith("codex-home")
    direct = scrubbed_env()
    assert "CLAUDECODE" not in direct and "CODEX_CI" not in direct


def test_claude_stream_json_modes_and_resume(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    job_id, job, _ = _start(
        bridge, monkeypatch, capsys, agent="claude", extra=["--resume", "old-session"])
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    result = read_result(job)
    argv = result["attempts"][0]["argv"]
    assert ["--output-format", "stream-json"] == argv[
        argv.index("--output-format"):argv.index("--output-format") + 2]
    assert "--verbose" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--resume") + 1] == "old-session"
    assert result["session_id"] == "old-session"
    assert (job / "last.md").read_text("utf-8") == "ok"


def test_doctor_does_not_mutate_home(tmp_path, capsys):
    home = tmp_path / "data"
    home.mkdir()
    before = list(home.rglob("*"))
    assert main(["--home", str(home), "doctor"]) == EXIT_FAIL
    output = capsys.readouterr().out
    assert "fake-codex" in output and "fake-claude" in output
    assert "bridge launcher is missing" in output
    assert list(home.rglob("*")) == before


def test_common_harness_and_bridge_config_are_resolved(bridge, monkeypatch, capsys):
    home, cwd, _ = bridge
    tools = home.parent / "tools-data"
    tools.mkdir()
    fake = Path(__file__).parent / "fakes" / "codex"
    config = (
        "schema_version = 1\n"
        "[paths]\n"
        f'vault = "{cwd}"\n'
        f'topics_root = "{cwd}"\n'
        f'sources_root = "{cwd}"\n'
        "[harness.codex]\n"
        f'argv = ["{fake}"]\n'
        'model_default = "configured-model"\n'
        'effort_default = "medium"\n'
    )
    (tools / "config.toml").write_text(config, "utf-8")
    home.mkdir()
    extra_root = home.parent / "extra-root"
    (home / "config.toml").write_text(
        f'codex_writable_roots = ["{extra_root}"]\n', "utf-8")
    monkeypatch.setenv("TOOLS_DATA", str(tools))
    job_id, job, _ = _start(bridge, monkeypatch, capsys, extra=["--write"])
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    request = read_request(job)
    assert request["model"] == "configured-model"
    assert request["effort"] == "medium"
    assert request["harness_argv"][0] == str(fake.resolve())
    assert str(extra_root) in request["codex_writable_roots"]
    assert str(cwd / ".git") in request["codex_writable_roots"]


@pytest.mark.parametrize(
    "mode,exit_code,outcome,event_class,title",
    [
        ("ok", EXIT_OK, "ok", "event", "codex ok: ok"),
        ("quota", EXIT_QUOTA, "quota", "alert", "codex quota: usage_limit_exceeded"),
    ],
)
def test_on_event_receives_gateway_v2_env_once(
        bridge, monkeypatch, capsys, mode, exit_code, outcome, event_class, title):
    home, _, _ = bridge
    home.mkdir()
    hook = home.parent / "hook.py"
    received = home.parent / f"hook-{mode}.jsonl"
    hook.write_text(
        "import json, os, pathlib, sys\n"
        "keys = [k for k in os.environ if k.startswith('BRIDGE_')]\n"
        "with pathlib.Path(sys.argv[1]).open('a') as stream:\n"
        "    stream.write(json.dumps({k: os.environ[k] for k in keys}) + '\\n')\n",
        "utf-8",
    )
    command = " ".join((shlex.quote(sys.executable), shlex.quote(str(hook)),
                         shlex.quote(str(received))))
    (home / "config.toml").write_text(
        "on_event = " + json.dumps(command) + "\n",
        "utf-8",
    )
    job_id, job, _ = _start(bridge, monkeypatch, capsys, mode=mode)
    assert _wait(job_id, home, capsys)[0] == exit_code
    _wait_path(received)
    rows = [json.loads(line) for line in received.read_text("utf-8").splitlines()]
    assert len(rows) == 1
    env = rows[0]
    assert env["BRIDGE_FROM"] == "bridge"
    assert env["BRIDGE_ABOUT"] == job_id
    assert env["BRIDGE_CLASS"] == event_class
    assert env["BRIDGE_TITLE"] == title
    assert env["BRIDGE_LINES"].splitlines()[0] == "work"
    assert env["BRIDGE_LINES"].splitlines()[1].startswith("1 attempts, ")
    assert env["BRIDGE_NAME"] == job_id
    assert env["BRIDGE_KIND"] == outcome
    assert env["BRIDGE_SUBJECT"] == job_id
    assert read_result(job)["status"] == outcome


def test_empty_on_event_is_not_called_and_hook_failure_does_not_change_result(
        bridge, monkeypatch, capsys):
    home, _, _ = bridge
    home.mkdir()
    (home / "config.toml").write_text('on_event = ""\n', "utf-8")
    job_id, job, _ = _start(bridge, monkeypatch, capsys)
    assert _wait(job_id, home, capsys)[0] == EXIT_OK
    assert read_request(job)["on_event"] == ""
    assert "hook:" not in (home / "logs" / f"worker-{job_id}.log").read_text("utf-8")

    (home / "config.toml").write_text('on_event = "exit 7"\n', "utf-8")
    failed_id, failed_job, _ = _start(bridge, monkeypatch, capsys)
    assert _wait(failed_id, home, capsys)[0] == EXIT_OK
    log = home / "logs" / f"worker-{failed_id}.log"
    deadline = time.monotonic() + 2
    while "hook: fail exit 7" not in log.read_text("utf-8") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "hook: fail exit 7" in log.read_text("utf-8")
    assert read_result(failed_job)["status"] == "ok"


def test_first_writer_wins(tmp_path):
    _, job = new_job(tmp_path / "data", {
        "agent": "codex", "cwd": str(tmp_path), "mode": "read-only", "prompt": "x",
    })
    assert write_result(job, {"status": "ok", "finished_at": "first"})
    assert not write_result(job, {"status": "fail", "finished_at": "second"})
    assert read_result(job)["status"] == "ok"


def test_first_writer_wins_under_race(tmp_path):
    _, job = new_job(tmp_path / "data", {
        "agent": "codex", "cwd": str(tmp_path), "mode": "read-only", "prompt": "x",
    })
    barrier = threading.Barrier(3)
    outcomes: list[bool] = []

    def writer(status: str) -> None:
        barrier.wait()
        outcomes.append(write_result(job, {"status": status, "finished_at": status}))

    threads = [threading.Thread(target=writer, args=(status,)) for status in ("ok", "fail")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [False, True]
    assert read_result(job)["status"] in {"ok", "fail"}


def test_structured_codes_precede_regex_and_boundaries(tmp_path):
    for code, expected in STRUCTURED.items():
        summary = {"codex_error_info": {"code": code}, "agent": "codex",
                   "terminal": True, "error": "usage limit at capacity"}
        assert classify(summary, 1, "", "read-only") == expected
    base = {"agent": "codex", "terminal": True, "error": "", "final_text": "",
            "codex_error_info": None, "side_effects": False}
    assert classify({**base, "error": "usage limit exceeded"}, 1, "", "read-only") == "quota"
    assert classify({**base, "error": "rate limit exceeded"}, 1, "", "read-only") == "transient"
    assert classify({**base, "error": "unknown"}, 1, "", "read-only") == "transient"
    assert classify({**base, "error": "unknown", "side_effects": True}, 1, "", "write") == "needs-review"


def test_build_argv_security_modes(tmp_path):
    base = {"agent": "codex", "mode": "write", "model": "gpt-test", "effort": "high",
            "job_dir": str(tmp_path), "harness_argv": ["/fake/codex"],
            "codex_writable_roots": [str(tmp_path / ".git")]}
    argv = build_argv(base, 1, "thread-1")
    assert argv[:4] == ["/fake/codex", "exec", "resume", "thread-1"]
    assert "sandbox_mode=workspace-write" in argv
    assert "approval_policy=never" in argv
    assert "notify=[]" in argv
    assert argv[-1] == "-"
    assert "danger-full-access" not in argv
    assert any("writable_roots" in item and ".git" in item for item in argv)

    claude = {**base, "agent": "claude", "mode": "read-only",
              "harness_argv": ["/fake/claude"]}
    read = build_argv(claude, 1, None)
    write = build_argv({**claude, "mode": "write"}, 1, "session")
    assert read[read.index("--permission-mode") + 1] == "dontAsk"
    assert write[write.index("--permission-mode") + 1] == "acceptEdits"
    assert "Write" not in read[read.index("--allowedTools") + 1].split()
    assert "Write" in write[write.index("--allowedTools") + 1].split()
    assert write[write.index("--resume") + 1] == "session"


def test_parse_claude_stream_summary(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join((
        json.dumps({"type": "system", "subtype": "init", "session_id": "s"}),
        json.dumps({"type": "result", "subtype": "error", "result": "bad",
                    "session_id": "s", "is_error": True, "usage": {"x": 1}}),
    )), "utf-8")
    summary = parse_events(path)
    assert summary["session_id"] == "s"
    assert summary["final_text"] == "bad"
    assert summary["error"] == "bad"
    assert summary["usage"] == {"x": 1}


@pytest.mark.parametrize(
    "message,expected",
    (("Claude usage limit reached", "quota"),
     ("503 service unavailable", "transient")),
)
def test_claude_error_result_classification(tmp_path, message, expected):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({
        "type": "result", "subtype": "error", "result": message,
        "session_id": "s", "is_error": True,
    }) + "\n", "utf-8")
    summary = parse_events(path)
    assert classify(summary, 1, "", "read-only") == expected


def test_two_jobs_have_no_shared_mutable_state(bridge, monkeypatch, capsys):
    home, _, _ = bridge
    monkeypatch.setenv("FAKE_SLEEP", "0.15")
    first, first_job, _ = _start(bridge, monkeypatch, capsys, mode="slow")
    second, second_job, _ = _start(bridge, monkeypatch, capsys, mode="slow")
    assert first != second and first_job != second_job
    assert not (home / "state.json").exists()
    assert _wait(first, home, capsys)[0] == EXIT_OK
    assert _wait(second, home, capsys)[0] == EXIT_OK
