"""Detached worker: attempts, retries, timeouts, and terminal results."""
from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .harness import ACTIVE_WRITER_TEXT, build_argv, classify, parse_events, warnings_from
from .jobs import (
    FILE_MODE,
    boot_id,
    cancel_path,
    create_json_once,
    events_path,
    last_path,
    now_iso,
    proc_path,
    prompt_path,
    read_json,
    read_request,
    stderr_path,
    write_json,
    write_result,
    write_text,
)


_terminate_requested = False
HOOK_TIMEOUT = 30


def _term_handler(_signum: int, _frame: object) -> None:
    global _terminate_requested
    _terminate_requested = True


def scrubbed_env() -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items()
        if not ((key.startswith("CLAUDE") or key.startswith("CODEX")) and key != "CODEX_HOME")
    }


def _open_private(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    return os.fdopen(fd, "wb", buffering=0)


def _cancel_marker(job: Path) -> dict[str, Any] | None:
    path = cancel_path(job)
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"by": "cli", "at": now_iso(), "invalid": True}


def _claim_cancel(job: Path, source: str) -> dict[str, Any]:
    marker = {"by": source, "at": now_iso()}
    create_json_once(cancel_path(job), marker)
    return _cancel_marker(job) or marker


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _backoffs(request: dict[str, Any]) -> tuple[list[float], bool]:
    raw = os.environ.get("BRIDGE_BACKOFF")
    if raw is not None:
        try:
            values = [max(0.0, float(value.strip())) for value in raw.split(",")]
            values = [value for value in values if value >= 0]
        except ValueError:
            values = []
        if values:
            return values, True
    configured = request.get("backoff", [60, 300])
    if not isinstance(configured, list) or not configured:
        configured = [60, 300]
    return [float(value) for value in configured], False


def _source(job: Path, deadline: float) -> str | None:
    marker = _cancel_marker(job)
    if marker:
        return str(marker.get("by") or "cli")
    if time.monotonic() >= deadline:
        marker = _claim_cancel(job, "max-time")
        return str(marker.get("by") or "max-time")
    if _terminate_requested:
        marker = _claim_cancel(job, "cli")
        return str(marker.get("by") or "cli")
    return None


def _wait_backoff(job: Path, seconds: float, deadline: float) -> str | None:
    end = time.monotonic() + seconds
    while True:
        source = _source(job, deadline)
        if source:
            return source
        remaining = min(end, deadline) - time.monotonic()
        if remaining <= 0:
            return _source(job, deadline)
        time.sleep(min(0.1, remaining))


def _reason(summary: dict[str, Any], stderr: str, classification: str) -> str:
    active_writer = "\n".join((
        str(summary.get("error") or ""),
        str(summary.get("final_text") or ""),
        stderr,
    ))
    if ACTIVE_WRITER_TEXT.search(active_writer):
        lines = [line.strip() for line in active_writer.splitlines() if line.strip()]
        detail = next(
            (line for line in lines if ACTIVE_WRITER_TEXT.search(line)),
            "already has an active writer",
        )[:500]
        return (detail + "; the original harness is still alive - check the original "
                "job with bridge status and run bridge cancel ID before resuming")
    info = summary.get("codex_error_info")
    if isinstance(info, dict):
        for key in ("code", "type", "reason", "error_code"):
            if info.get(key):
                return str(info[key])
    if isinstance(info, str) and info.strip():
        return info.strip()[:500]
    if classification == "needs-review":
        return "needs-review"
    error = str(summary.get("error") or "").strip()
    if error:
        return error.splitlines()[0][:500]
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if lines:
        return lines[-1][:500]
    return classification


def _terminal(
    request: dict[str, Any], status: str, reason: str, attempts: list[dict[str, Any]],
    *, exit_code: int | None, session_id: str | None, usage: object,
    warnings: list[str], source: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": request.get("id"),
        "status": status,
        "reason": reason,
        "exit_code": exit_code,
        "session_id": session_id,
        "attempts": attempts,
        "usage": usage,
        "warnings": list(dict.fromkeys(warnings)),
        "finished_at": now_iso(),
    }
    if request.get("agent") == "codex":
        result["thread_id"] = session_id
    if source:
        result["cancelled_by"] = source
    return result


def _one_line(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _elapsed(request: dict[str, Any], result: dict[str, Any]) -> int:
    try:
        started = datetime.fromisoformat(str(request["created_at"]).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(result["finished_at"]).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        return max(0, int((finished - started).total_seconds()))
    except (KeyError, TypeError, ValueError):
        return 0


def _short_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def _hook_env(
    job: Path, request: dict[str, Any], result: dict[str, Any],
) -> dict[str, str]:
    status = _one_line(result.get("status") or "fail", 32).lower()
    agent = _one_line(request.get("agent") or "agent", 32).lower()
    detail = _one_line(result.get("reason") or status, 500)
    try:
        lines = last_path(job).read_text("utf-8", errors="replace").splitlines()
        if lines and _one_line(lines[0], 500):
            detail = _one_line(lines[0], 500)
    except OSError:
        pass
    job_id = _one_line(request.get("id") or job.name, 48)
    cwd = Path(str(request.get("cwd") or "?"))
    short_cwd = cwd.name or str(cwd)
    attempts = result.get("attempts")
    attempt_count = len(attempts) if isinstance(attempts, list) else 0
    hook_class = "event" if status in {"ok", "cancelled"} else "alert"
    return {
        "BRIDGE_FROM": "bridge",
        "BRIDGE_ABOUT": job_id,
        "BRIDGE_CLASS": hook_class,
        "BRIDGE_TITLE": _one_line(f"{agent} {status}: {detail}", 120),
        "BRIDGE_LINES": (
            _one_line(short_cwd, 200) + "\n" +
            f"{attempt_count} attempts, {_short_duration(_elapsed(request, result))}"
        ),
        "BRIDGE_NAME": job_id,
        "BRIDGE_KIND": status,
        "BRIDGE_SUBJECT": job_id,
    }


def _hook_log(job: Path, message: str) -> None:
    path = job.parents[1] / "logs" / f"worker-{job.name}.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def run_event_hook(job: Path, request: dict[str, Any], result: dict[str, Any]) -> None:
    hook = request.get("on_event")
    if not isinstance(hook, str) or not hook.strip():
        return
    env = scrubbed_env()
    env.update(_hook_env(job, request, result))
    try:
        completed = subprocess.run(
            ["/bin/sh", "-c", hook],
            text=True,
            capture_output=True,
            cwd=request.get("cwd"),
            env=env,
            timeout=HOOK_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _hook_log(job, f"hook: fail timeout {HOOK_TIMEOUT}s")
    except (OSError, subprocess.SubprocessError) as error:
        _hook_log(job, "hook: fail " + _one_line(error, 500))
    else:
        if completed.returncode != 0:
            _hook_log(job, f"hook: fail exit {completed.returncode}")


def _publish(job: Path, request: dict[str, Any], result: dict[str, Any]) -> bool:
    won = write_result(job, result)
    if won:
        run_event_hook(job, request, result)
    return won


def _stop_process(
    process: subprocess.Popen[bytes], job: Path, source: str, grace: float,
    request: dict[str, Any], attempts: list[dict[str, Any]], warnings: list[str],
    session_id: str | None, usage: object,
) -> None:
    marker = _claim_cancel(job, source)
    source = str(marker.get("by") or source)
    try:
        os.killpg(os.getpgrp(), signal.SIGTERM)
    except PermissionError:
        try:
            os.kill(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass
    end = time.monotonic() + grace
    while process.poll() is None and time.monotonic() < end:
        time.sleep(0.05)
    try:
        current = attempts[-1]
        summary = parse_events(current["events"])
        stderr = Path(current["stderr"]).read_text("utf-8", errors="replace")
        session_id = str(summary["session_id"]) if summary.get("session_id") else session_id
        usage = summary.get("usage") or usage
        warnings.extend(warnings_from(summary, stderr))
        current["session_id"] = summary.get("session_id")
    except (OSError, ValueError, IndexError):
        pass
    status = "timeout" if source == "max-time" else "cancelled"
    result = _terminal(
        request, status, "max-time" if status == "timeout" else source, attempts,
        exit_code=process.poll(), session_id=session_id, usage=usage,
        warnings=warnings, source=source,
    )
    _publish(job, request, result)
    if process.poll() is None:
        try:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        except PermissionError:
            try:
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=1)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        except ProcessLookupError:
            pass


def run_worker(job_dir: str | Path) -> int:
    """Execute a job directory. Called only by the hidden CLI command."""
    global _terminate_requested
    _terminate_requested = False
    os.umask(0o077)
    job = Path(job_dir).resolve()
    request = read_request(job)
    request["job_dir"] = str(job)
    started_at = now_iso()
    process_info = {
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "sid": os.getsid(0),
        "boot_id": boot_id(),
        "started_at": started_at,
        "harness_pid": None,
    }
    write_json(proc_path(job), process_info)
    signal.signal(signal.SIGTERM, _term_handler)
    signal.signal(signal.SIGINT, _term_handler)

    max_time = float(request.get("max_time", 5400))
    deadline = time.monotonic() + max_time
    grace = _float_env("BRIDGE_GRACE", 20.0)
    max_retries = int(request.get("max_retries", 2))
    backoffs, exact_backoff = _backoffs(request)
    attempts: list[dict[str, Any]] = []
    raw_warnings = request.get("warnings", [])
    all_warnings = [str(item) for item in raw_warnings] if isinstance(raw_warnings, list) else []
    session_id = request.get("resume_id") if isinstance(request.get("resume_id"), str) else None
    usage: object = None
    poll = max(0.01, _float_env("BRIDGE_WORKER_POLL", 0.5))

    for number in range(1, max_retries + 2):
        source = _source(job, deadline)
        if source:
            status = "timeout" if source == "max-time" else "cancelled"
            result = _terminal(
                request, status, "max-time" if status == "timeout" else source, attempts,
                exit_code=None, session_id=session_id, usage=usage, warnings=all_warnings,
                source=source,
            )
            _publish(job, request, result)
            return 0

        argv = build_argv(request, number, session_id)
        attempt_started = now_iso()
        out_path = events_path(job, number)
        err_path = stderr_path(job, number)
        out_stream = _open_private(out_path)
        err_stream = _open_private(err_path)
        try:
            attempt_env = scrubbed_env()
            attempt_env["BRIDGE_ATTEMPT"] = str(number)
            with prompt_path(job).open("rb") as prompt_stream:
                process = subprocess.Popen(
                    argv,
                    cwd=request["cwd"],
                    stdin=prompt_stream,
                    stdout=out_stream,
                    stderr=err_stream,
                    env=attempt_env,
                )
                process_info["harness_pid"] = process.pid
                write_json(proc_path(job), process_info)
        finally:
            out_stream.close()
            err_stream.close()

        source = None
        while process.poll() is None:
            source = _source(job, deadline)
            if source:
                break
            time.sleep(poll)
        if source:
            attempts.append({
                "number": number,
                "argv": argv,
                "started_at": attempt_started,
                "finished_at": now_iso(),
                "exit_code": process.poll(),
                "classification": "timeout" if source == "max-time" else "cancelled",
                "events": str(out_path),
                "stderr": str(err_path),
            })
            _stop_process(
                process, job, source, grace, request, attempts, all_warnings,
                session_id, usage,
            )
            return 0

        exit_code = process.wait()
        if last_path(job).is_file():
            last_path(job).chmod(FILE_MODE)
        summary = parse_events(out_path)
        stderr = err_path.read_text("utf-8", errors="replace")
        classification = classify(summary, exit_code, stderr, str(request.get("mode")))
        session_id = str(summary["session_id"]) if summary.get("session_id") else session_id
        usage = summary.get("usage") or usage
        all_warnings.extend(warnings_from(summary, stderr))
        if request.get("agent") == "claude" and summary.get("final_text"):
            write_text(last_path(job), str(summary["final_text"]))
        attempts.append({
            "number": number,
            "argv": argv,
            "started_at": attempt_started,
            "finished_at": now_iso(),
            "exit_code": exit_code,
            "classification": classification,
            "session_id": summary.get("session_id"),
            "events": str(out_path),
            "stderr": str(err_path),
        })

        if classification == "done":
            result = _terminal(
                request, "ok", "completed", attempts, exit_code=exit_code,
                session_id=session_id, usage=usage, warnings=all_warnings,
            )
            _publish(job, request, result)
            return 0
        if classification == "quota":
            result = _terminal(
                request, "quota", _reason(summary, stderr, classification), attempts,
                exit_code=exit_code, session_id=session_id, usage=usage,
                warnings=all_warnings,
            )
            _publish(job, request, result)
            return 0
        if classification in {"permanent", "needs-review"}:
            result = _terminal(
                request, "fail", _reason(summary, stderr, classification), attempts,
                exit_code=exit_code, session_id=session_id, usage=usage,
                warnings=all_warnings,
            )
            _publish(job, request, result)
            return 0

        if number > max_retries:
            result = _terminal(
                request, "fail", "transient-exhausted", attempts,
                exit_code=exit_code, session_id=session_id, usage=usage,
                warnings=all_warnings,
            )
            _publish(job, request, result)
            return 0
        pause = backoffs[min(number - 1, len(backoffs) - 1)]
        if not exact_backoff:
            pause *= random.uniform(0.9, 1.1)
        source = _wait_backoff(job, pause, deadline)
        if source:
            status = "timeout" if source == "max-time" else "cancelled"
            result = _terminal(
                request, status, "max-time" if status == "timeout" else source, attempts,
                exit_code=exit_code, session_id=session_id, usage=usage,
                warnings=all_warnings, source=source,
            )
            _publish(job, request, result)
            return 0
    return 1


def worker_main(job_dir: str | Path) -> int:
    try:
        return run_worker(job_dir)
    except BaseException as error:
        job = Path(job_dir)
        try:
            request = read_request(job)
            result = _terminal(
                request, "fail", "worker-error: " + str(error), [], exit_code=None,
                session_id=None, usage=None, warnings=[],
            )
            _publish(job, request, result)
        except BaseException:
            traceback.print_exc()
        return 1
