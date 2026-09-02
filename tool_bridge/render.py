"""Compact rendering for result, log, and status commands."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .harness import DENIAL_TEXT, command_denials, parse_events
from .jobs import derive_state, last_path, read_request, read_result, result_path


OUTPUT_CAP = 12_000


def _attempt_files(job: Path) -> list[Path]:
    def number(path: Path) -> int:
        try:
            return int(path.name.split(".")[1])
        except (IndexError, ValueError):
            return 0
    return sorted(job.glob("events.*.jsonl"), key=number)


def _fallback_text(job: Path) -> tuple[str, Path | None]:
    final = error = ""
    final_path: Path | None = None
    error_path: Path | None = None
    for path in _attempt_files(job):
        try:
            summary = parse_events(path)
        except OSError:
            continue
        if summary.get("final_text"):
            final = str(summary["final_text"])
            final_path = path
        if summary.get("error"):
            error = str(summary["error"])
            error_path = path
    if final:
        return final, final_path
    if error:
        return error, error_path
    stderr_files = sorted(job.glob("stderr.*.log"))
    for path in reversed(stderr_files):
        try:
            lines = [line for line in path.read_text("utf-8", errors="replace").splitlines()
                     if line.strip()]
        except OSError:
            continue
        if lines:
            return lines[-1], path
    return "", None


def result_text(job: str | Path) -> tuple[str, Path | None]:
    job = Path(job)
    path = last_path(job)
    if path.is_file():
        return path.read_text("utf-8", errors="replace"), path
    return _fallback_text(job)


def capped(text: str, full_path: Path | None, *, full: bool) -> str:
    if full or len(text) <= OUTPUT_CAP:
        return text
    path = str(full_path) if full_path else "job directory artifacts"
    suffix = f"\n[output truncated; full output: {path}]"
    keep = max(0, OUTPUT_CAP - len(suffix))
    return text[:keep] + suffix


def result_payload(job: str | Path, *, full: bool = False) -> dict[str, Any]:
    job = Path(job)
    text, full_path = result_text(job)
    result = read_result(job) if result_path(job).is_file() else None
    payload = _machine_payload(job, result=result)
    warnings = payload["warnings"]
    state_line = str(payload["state"])
    if payload["state"] == "finished" and payload.get("outcome"):
        state_line += "/" + str(payload["outcome"])
        reason = str(payload.get("reason") or "")
        if payload.get("outcome") != "ok" and reason not in {"", str(payload["outcome"])}:
            state_line += " " + reason
    prefix = state_line + "\n"
    if warnings:
        prefix += "WARNING: " + "; ".join(str(item) for item in warnings) + "\n"
    if full:
        rendered = prefix + text
    else:
        rendered = capped(prefix + text, full_path, full=False)
    return {
        **payload,
        "result": result,
        "text": rendered,
    }


def render_result(job: str | Path, *, full: bool = False) -> str:
    return str(result_payload(job, full=full)["text"])


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(block.get("text") or "") for block in value if isinstance(block, dict)
        )
    return ""


def _short(value: object, limit: int) -> str:
    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        rendered = str(value or "")
    return " ".join(rendered.split())[:limit]


def _changed_paths(item: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    if item.get("path"):
        paths.append(str(item["path"]))
    changes = item.get("changes")
    if isinstance(changes, list):
        paths.extend(str(change["path"]) for change in changes
                     if isinstance(change, dict) and change.get("path"))
    return list(dict.fromkeys(paths))


def _render_event(event: dict[str, Any]) -> list[str]:
    kind = event.get("type")
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    item_type = item.get("type") or item.get("item_type")
    denials = command_denials(event)
    if denials:
        return ["denied: " + _short(detail, 500) for detail in denials]
    if kind == "thread.started":
        return ["thread: " + str(event.get("thread_id") or "?")]
    if kind in {"turn.started", "turn.completed"}:
        return []
    if kind in {"item.started", "item.completed"}:
        if item_type == "agent_message":
            if kind == "item.completed":
                return ["message: " + _content_text(item.get("text") or item.get("content"))]
            return []
        if item_type == "file_change":
            if kind == "item.completed":
                return ["edited: " + path for path in _changed_paths(item)]
            return []
        if item_type == "command_execution":
            command = _short(item.get("command") or "command", 500)
            suffix = ""
            if kind == "item.completed":
                exit_code = item.get("exit_code", item.get("exitCode"))
                suffix = " -> exit " + str(exit_code if exit_code is not None else "?")
            return ["running: " + command + suffix]
        if item_type == "mcp_tool_call":
            server = item.get("server") or item.get("server_name") or "mcp"
            tool = item.get("tool") or item.get("tool_name") or item.get("name") or "tool"
            return [f"tool: {server}.{tool}"]
    if kind in {"turn.failed", "error"}:
        return ["error: " + _short(event.get("error") or event.get("message") or kind, 500)]
    if kind == "system":
        return []
    if kind == "assistant":
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        content = message.get("content") if isinstance(message, dict) else []
        rendered: list[str] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    rendered.append("message: " + str(block.get("text") or ""))
                elif block.get("type") == "tool_use":
                    tool_input = _short(block.get("input") or "", 80)
                    suffix = " " + tool_input if tool_input else ""
                    rendered.append(f"tool: {block.get('name') or 'tool'}{suffix}")
        return rendered
    if kind == "rate_limit_event":
        return []
    if kind == "result":
        if event.get("is_error"):
            return ["error: " + _short(event.get("result") or event.get("subtype"), 500)]
        return []
    return []


def render_log(job: str | Path, *, tail: int | None = None, raw: bool = False) -> str:
    job = Path(job)
    lines: list[str] = []
    for path in _attempt_files(job):
        attempt = path.name.split(".")[1]
        try:
            source = path.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if raw:
            lines.extend(line for line in source if line.strip())
            continue
        for line in source:
            try:
                event = json.loads(line)
            except ValueError:
                if line.strip():
                    lines.append(f"error[{attempt}]: {line[:500]}")
                continue
            if isinstance(event, dict):
                lines.extend(_render_event(event))
        stderr = job / f"stderr.{attempt}.log"
        try:
            for line in stderr.read_text("utf-8", errors="replace").splitlines():
                if DENIAL_TEXT.search(line):
                    lines.append("denied: " + line.strip()[:500])
        except OSError:
            pass
    if tail is not None and tail < 0:
        raise ValueError("tail must be non-negative")
    if tail:
        lines = lines[-tail:]
    return "\n".join(lines)


def _machine_payload(
    job: Path, *, result: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = read_request(job)
    if state is None:
        state = derive_state(job)
    if result is None and result_path(job).is_file():
        result = read_result(job)
    attempts = result.get("attempts", []) if isinstance(result, dict) else []
    if not isinstance(attempts, list):
        attempts = []
    warnings = result.get("warnings", []) if isinstance(result, dict) else []
    if not isinstance(warnings, list):
        warnings = []
    thread_id = None
    if isinstance(result, dict):
        thread_id = result.get("thread_id") or result.get("session_id")
    return {
        "id": request.get("id") or job.name,
        "agent": request.get("agent"),
        "cwd": request.get("cwd"),
        "mode": request.get("mode"),
        "state": state.get("state"),
        "outcome": state.get("outcome"),
        "reason": state.get("reason"),
        "age_s": state.get("age"),
        "attempts": attempts,
        "thread_id": thread_id,
        "warnings": warnings,
        "paths": {
            "job": str(job),
            "result": str(result_path(job)),
            "last": str(last_path(job)),
        },
    }


def status_payload(job: str | Path) -> dict[str, Any]:
    job = Path(job)
    state = derive_state(job)
    preview, _ = result_text(job)
    if state.get("outcome") not in (None, "ok"):
        preview = str(state.get("reason") or preview)
    return {
        **_machine_payload(job, state=state),
        "worker_died": bool(state.get("worker_died")),
        "harness_pid": state.get("harness_pid"),
        "preview": preview.splitlines()[0][:160] if preview else state.get("last_event", ""),
        "last_event": state.get("last_event", ""),
        "last_event_age_s": state.get("last_event_age"),
        "stalled_min": state.get("stalled_min"),
    }


def _duration(seconds: object) -> str:
    if not isinstance(seconds, (int, float)):
        return "?"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds) // 60}m"
    return f"{int(seconds) // 3600}h"


def render_wait_timeout(job: str | Path, state: dict[str, Any]) -> str:
    job = Path(job)
    latest = render_log(job, tail=1) or str(state.get("last_event") or "no events")
    latest = latest.replace("\n", " ")[:500]
    state_label = str(state.get("reason")) if state.get("worker_died") else "running"
    line = (f"{job.name} {state_label} {_duration(state.get('age'))}"
            f" · last event {_duration(state.get('last_event_age'))}: {latest}")
    if state.get("stalled_min") is not None:
        line += f" · stalled {state['stalled_min']}m"
    return line


def render_status(job: str | Path) -> str:
    data = status_payload(job)
    cwd = Path(str(data.get("cwd") or "?")).name or str(data.get("cwd") or "?")
    state = str(data["state"])
    if state == "finished" and data.get("outcome"):
        state += "/" + str(data["outcome"])
    if data.get("stalled_min") is not None:
        state += f" stalled={data['stalled_min']}m"
    if data.get("worker_died"):
        state += " " + str(data.get("reason"))
    last = str(data.get("preview") or data.get("last_event") or data.get("reason") or "")
    last = last.replace("\n", " ")[:160]
    return " ".join((
        str(data["id"]), str(data.get("agent") or "?"), cwd,
        str(data.get("mode") or "?"), state, _duration(data.get("age_s")), last,
    )).rstrip()
