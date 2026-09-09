"""Job directories, atomic files, and process-derived state."""
from __future__ import annotations

import errno
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .shared.fs import atomic_write


FILE_MODE = 0o600
DIR_MODE = 0o700


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    path.chmod(DIR_MODE)


def _secure_atomic(path: Path, data: str | bytes) -> None:
    old_umask = os.umask(0o077)
    try:
        atomic_write(path, data)
    finally:
        os.umask(old_umask)
    path.chmod(FILE_MODE)


def write_json(path: str | Path, value: dict[str, Any]) -> None:
    """Write JSON atomically with mode 0600."""
    _secure_atomic(Path(path), json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def write_text(path: str | Path, value: str) -> None:
    """Write text atomically with mode 0600."""
    _secure_atomic(Path(path), value)


def read_json(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def request_path(job: str | Path) -> Path:
    return Path(job) / "request.json"


def prompt_path(job: str | Path) -> Path:
    return Path(job) / "prompt.md"


def proc_path(job: str | Path) -> Path:
    return Path(job) / "proc.json"


def result_path(job: str | Path) -> Path:
    return Path(job) / "result.json"


def cancel_path(job: str | Path) -> Path:
    return Path(job) / "cancel"


def events_path(job: str | Path, attempt: int) -> Path:
    return Path(job) / f"events.{attempt}.jsonl"


def stderr_path(job: str | Path, attempt: int) -> Path:
    return Path(job) / f"stderr.{attempt}.log"


def last_path(job: str | Path) -> Path:
    return Path(job) / "last.md"


def read_request(job: str | Path) -> dict[str, Any]:
    return read_json(request_path(job))


def read_proc(job: str | Path) -> dict[str, Any]:
    return read_json(proc_path(job))


def read_result(job: str | Path) -> dict[str, Any]:
    return read_json(result_path(job))


def new_job(home: str | Path, request: dict[str, Any]) -> tuple[str, Path]:
    """Create an isolated job directory and return its ID and path."""
    home = Path(home)
    jobs = home / "jobs"
    logs = home / "logs"
    for directory in (home, jobs, logs):
        _secure_dir(directory)

    agent = str(request.get("agent") or "")
    if agent not in {"codex", "claude"}:
        raise ValueError("request.agent must be codex or claude")
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    for _ in range(256):
        job_id = f"{agent}-{stamp}-{secrets.token_hex(2)}"
        job = jobs / job_id
        try:
            job.mkdir(mode=DIR_MODE)
        except FileExistsError:
            continue
        job.chmod(DIR_MODE)
        break
    else:
        raise RuntimeError("could not allocate an unused job ID")

    stored = dict(request)
    prompt = stored.pop("prompt", None)
    if not isinstance(prompt, str):
        shutil.rmtree(job)
        raise ValueError("request.prompt must be a string")
    stored["id"] = job_id
    stored["job_dir"] = str(job.resolve())
    stored.setdefault("created_at", now_iso())
    try:
        if stored.get("harness_argv"):
            from .harness import build_argv
            stored["argv"] = build_argv(stored, 1, stored.get("resume_id"))
        write_json(request_path(job), stored)
        _secure_atomic(prompt_path(job), prompt)
    except BaseException:
        shutil.rmtree(job)
        raise
    return job_id, job


def create_json_once(path: str | Path, value: dict[str, Any]) -> bool:
    """Publish JSON atomically only when the destination does not exist."""
    target = Path(path)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, FILE_MODE)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(fd)
        fd = -1
        try:
            os.link(tmp, target)
        except FileExistsError:
            return False
        target.chmod(FILE_MODE)
        try:
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        return True
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def write_result(job: str | Path, result: dict[str, Any]) -> bool:
    """Record a result exactly once."""
    return create_json_once(result_path(job), result)


def boot_id() -> str | None:
    """Return a stable boot identifier, or None when probing is unavailable."""
    system = platform.system().lower()
    if system == "linux":
        try:
            for line in Path("/proc/stat").read_text("ascii").splitlines():
                if line.startswith("btime "):
                    return "linux:" + line.split()[1]
        except (OSError, IndexError):
            pass
    if system == "darwin":
        try:
            raw = subprocess.check_output(
                ["sysctl", "-n", "kern.boottime"], text=True,
                stderr=subprocess.DEVNULL, timeout=2).strip()
            # Seconds only: macOS adjusts kern.boottime during clock correction.
            # Its microseconds changed between probes and once made a live job
            # appear dead with a false "boot-id-mismatch" result.
            match = re.search(r"sec\s*=\s*(\d+)", raw)
            if match:
                return f"darwin:{match.group(1)}"
            if raw:
                return "darwin:" + raw
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def same_boot_id(stored: str, current: str) -> bool:
    """Compare boot IDs with tolerance for NTP clock corrections.

    Boot time may move by seconds during correction, while a reboot moves it by
    minutes or hours.
    """
    if stored == current:
        return True
    try:
        os_a, sec_a = stored.split(":", 1)
        os_b, sec_b = current.split(":", 1)
        return os_a == os_b and abs(float(sec_a) - float(sec_b)) <= 60
    except ValueError:
        return False


def process_group_alive(pgid: object) -> bool:
    if type(pgid) is not int or pgid <= 1:
        return False
    leader_reaped = False
    try:
        waited, _ = os.waitpid(pgid, os.WNOHANG)
        leader_reaped = waited == pgid
    except ChildProcessError:
        pass
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Some seatbelt profiles reject a negative PID even for a probe. The
        # group leader has pid == pgid because we use start_new_session.
        if leader_reaped:
            return False
        try:
            os.kill(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    except OSError as error:
        return error.errno == errno.EPERM
    return True


def _is_zombie(pid: int) -> bool:
    """True when procfs reports the process as a zombie (Linux only).

    A killed worker leaves its harness child as a zombie until the new parent
    reaps it. On Linux getpgid still succeeds for a zombie, so without this check
    a wait issued right after the kill can report a live harness that is already
    dead and return a transient exit code instead of died.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat = handle.read()
    except OSError:
        return False
    # Fields after the last ")" of the command name; the first is the state.
    fields = stat.rsplit(b")", 1)[-1].split()
    return bool(fields) and fields[0] == b"Z"


def process_in_group(pid: object, pgid: object) -> bool:
    """Check that a process is alive and belongs to the expected group."""
    if type(pid) is not int or pid <= 1 or type(pgid) is not int or pgid <= 1:
        return False
    try:
        return os.getpgid(pid) == pgid and not _is_zombie(pid)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A denied probe must not turn a live job into a dead one.
        return True
    except OSError as error:
        return error.errno == errno.EPERM


def _latest_event(job: Path) -> tuple[str, float | None]:
    newest_line = ""
    newest_mtime: float | None = None
    for path in sorted(job.glob("events.*.jsonl")):
        try:
            stat = path.stat()
            lines = [line for line in path.read_text("utf-8", errors="replace").splitlines()
                     if line.strip()]
        except OSError:
            continue
        if newest_mtime is None or stat.st_mtime >= newest_mtime:
            newest_mtime = stat.st_mtime
            newest_line = lines[-1] if lines else ""
    if not newest_line:
        return "", newest_mtime
    try:
        event = json.loads(newest_line)
    except ValueError:
        return newest_line[:240], newest_mtime
    if not isinstance(event, dict):
        return newest_line[:240], newest_mtime
    kind = str(event.get("type") or "event")
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    detail = (event.get("message") or event.get("result") or event.get("error") or item.get("text")
              or item.get("type") or item.get("item_type") or "")
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False)
    return (kind + (": " + str(detail) if detail else ""))[:240], newest_mtime


def _age_seconds(request: dict[str, Any], job: Path) -> int:
    created = _parse_time(request.get("created_at"))
    if created is not None:
        return max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
    try:
        return max(0, int(datetime.now().timestamp() - job.stat().st_mtime))
    except OSError:
        return 0


def derive_state(job: str | Path) -> dict[str, Any]:
    """Derive finished, running, or died without a stored registry."""
    job = Path(job)
    request = read_request(job)
    age = _age_seconds(request, job)
    event, event_mtime = _latest_event(job)
    target = result_path(job)
    if target.is_file():
        result = read_result(job)
        return {
            "state": "finished",
            "reason": str(result.get("reason") or result.get("status") or "finished"),
            "outcome": result.get("status"),
            "age": age,
            "stalled_min": None,
            "last_event_age": None,
            "last_event": event,
        }

    try:
        proc = read_proc(job)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"state": "died", "reason": "missing-or-invalid-proc", "age": age,
                "stalled_min": None, "last_event_age": None, "last_event": event}
    stored_boot = proc.get("boot_id")
    current_boot = boot_id()
    same_boot = (stored_boot is None or current_boot is None or
                 same_boot_id(str(stored_boot), current_boot))
    pgid = proc.get("pgid")
    if not same_boot:
        return {"state": "died", "reason": "boot-id-mismatch", "age": age,
                "stalled_min": None, "last_event_age": None, "last_event": event}
    if not process_group_alive(pgid):
        return {"state": "died", "reason": "dead-pgid", "age": age,
                "stalled_min": None, "last_event_age": None, "last_event": event}

    worker_alive = process_in_group(proc.get("pid"), pgid)
    harness_pid = proc.get("harness_pid")
    harness_alive = process_in_group(harness_pid, pgid)
    if not worker_alive and type(harness_pid) is int and not harness_alive:
        return {"state": "died", "reason": "dead-worker-and-harness", "age": age,
                "stalled_min": None, "last_event_age": None, "last_event": event}
    worker_died = not worker_alive and harness_alive
    reason = (f"worker-died, harness alive pid {harness_pid}"
              if worker_died else "alive")

    now = datetime.now().timestamp()
    activity = event_mtime
    if activity is None:
        started = _parse_time(proc.get("started_at"))
        activity = started.timestamp() if started else job.stat().st_mtime
    quiet = max(0, int(now - activity))
    stall_after = request.get("stall_after", 1200)
    stalled = quiet // 60 if isinstance(stall_after, (int, float)) and quiet >= stall_after else None
    return {"state": "running", "reason": reason, "age": age,
            "worker_died": worker_died,
            "harness_pid": harness_pid if harness_alive else None,
            "stalled_min": stalled, "last_event_age": quiet, "last_event": event}


def list_jobs(home: str | Path) -> list[Path]:
    jobs = Path(home) / "jobs"
    if not jobs.is_dir():
        return []
    return sorted((path for path in jobs.iterdir() if path.is_dir()),
                  key=lambda path: path.name, reverse=True)


def gc(home: str | Path, days: int | float) -> list[str]:
    """Delete only old finished job directories."""
    removed: list[str] = []
    cutoff = datetime.now(timezone.utc).timestamp() - float(days) * 86400
    for job in list_jobs(home):
        try:
            state = derive_state(job)
            if state["state"] != "finished":
                continue
            result = read_result(job)
            finished = _parse_time(result.get("finished_at"))
            timestamp = finished.timestamp() if finished else result_path(job).stat().st_mtime
            if timestamp >= cutoff:
                continue
            shutil.rmtree(job)
            removed.append(job.name)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return removed
