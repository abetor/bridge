"""Harness argv construction, streaming events, and outcome classification."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .shared.adapters.base import RunResult
from .shared.adapters.claude import ClaudeAdapter
from .shared.adapters.codex import CodexAdapter


CLAUDE_READ_TOOLS = "Read Grep Glob LS WebFetch WebSearch"
CLAUDE_WRITE_TOOLS = (
    "Bash Edit Write MultiEdit NotebookEdit Read Grep Glob WebFetch WebSearch"
)
STRUCTURED = {
    "usage_limit_exceeded": "quota",
    "rate_limit_exceeded": "transient",
    "server_overloaded": "transient",
    "http_connection_failed": "transient",
    "response_stream_connection_failed": "transient",
    "response_stream_disconnected": "transient",
    "context_window_exceeded": "permanent",
    "unauthorized": "permanent",
    "bad_request": "permanent",
    "sandbox_error": "permanent",
    "cyber_policy": "permanent",
}
PERMANENT_TEXT = re.compile(
    r"bad[_ -]?request|invalid[_ -]?request|not supported|unauthorized|"
    r"context window|cyber[_ -]?policy|cybersecurity risk|sandbox[_ -]?error",
    re.I,
)
DENIAL_TEXT = re.compile(
    r"writing outside of the project|\.git/index\.lock|seatbelt|operation not permitted",
    re.I,
)
ACTIVE_WRITER_TEXT = re.compile(r"already has an active writer", re.I)


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    if isinstance(value, dict):
        for key in ("message", "text", "error", "detail"):
            found = _text(value.get(key))
            if found:
                return found
    return ""


def _find_key(value: object, wanted: str) -> object | None:
    if isinstance(value, dict):
        if wanted in value:
            return value[wanted]
        for child in value.values():
            found = _find_key(child, wanted)
            if found not in (None, "", {}):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, wanted)
            if found not in (None, "", {}):
                return found
    return None


def _structured_code(value: object) -> str:
    if isinstance(value, str):
        lowered = value.lower()
        for code in STRUCTURED:
            if code in lowered:
                return code
        return value.strip().lower()
    if isinstance(value, dict):
        for key in ("code", "type", "reason", "error_code"):
            code = _structured_code(value.get(key))
            if code:
                return code
        for child in value.values():
            code = _structured_code(child)
            if code in STRUCTURED:
                return code
    return ""


def command_denials(event: dict[str, Any]) -> list[str]:
    """Return denials only from the output of completed failed commands."""
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    item_type = item.get("type") or item.get("item_type")
    if item_type != "command_execution":
        return []
    exit_code = item.get("exit_code", item.get("exitCode"))
    if exit_code in (None, 0, "0"):
        return []
    output = _text(item.get("aggregated_output"))
    return [match.group(0) for match in DENIAL_TEXT.finditer(output)]


def build_argv(request: dict[str, Any], attempt: int, resume_id: str | None) -> list[str]:
    """Build argv without the prompt; stdin always belongs to the worker."""
    base = request.get("harness_argv")
    if not isinstance(base, list) or not base or not all(isinstance(x, str) for x in base):
        raise ValueError("request.harness_argv must be a non-empty list of strings")
    agent = request.get("agent")
    mode = request.get("mode", "read-only")
    model = request.get("model")
    effort = request.get("effort")
    job = Path(str(request["job_dir"]))

    if agent == "codex":
        argv = [*base, "exec"]
        if resume_id:
            argv += ["resume", resume_id]
        argv += ["--json", "-c", "approval_policy=never"]
        if resume_id:
            argv += ["-c", f"sandbox_mode={'workspace-write' if mode == 'write' else 'read-only'}"]
        else:
            argv += ["--sandbox", "workspace-write" if mode == "write" else "read-only"]
        argv += ["--skip-git-repo-check", "-c", "notify=[]"]
        if mode == "write":
            roots = request.get("codex_writable_roots") or []
            encoded = json.dumps(roots, ensure_ascii=False, separators=(",", ":"))
            argv += ["-c", f"sandbox_workspace_write.writable_roots={encoded}"]
        if model:
            argv += ["--model", str(model)]
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        argv += ["-o", str(job / "last.md"), "-"]
        return argv

    if agent == "claude":
        argv = [*base, "-p", "--verbose", "--output-format", "stream-json"]
        argv += ["--permission-mode", "acceptEdits" if mode == "write" else "dontAsk"]
        argv += ["--allowedTools", CLAUDE_WRITE_TOOLS if mode == "write" else CLAUDE_READ_TOOLS]
        if model:
            argv += ["--model", str(model)]
        if effort:
            argv += ["--effort", str(effort)]
        if resume_id:
            argv += ["--resume", resume_id]
        return argv
    raise ValueError(f"unknown agent: {agent}")


def parse_events(path: str | Path) -> dict[str, Any]:
    """Parse saved JSONL from either harness into one summary structure."""
    raw = Path(path).read_text("utf-8", errors="replace")
    summary: dict[str, Any] = {
        "agent": None,
        "thread_id": None,
        "session_id": None,
        "final_text": "",
        "usage": None,
        "error": "",
        "rate_limit": "",
        "codex_error_info": None,
        "side_effects": False,
        "denials": [],
        "started": False,
        "terminal": False,
        "terminal_success": False,
        "turn_failed": False,
        "event_count": 0,
    }
    denials: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        summary["event_count"] += 1
        kind = event.get("type")
        if kind in {"thread.started", "turn.started", "turn.completed", "turn.failed"} or str(kind).startswith("item."):
            summary["agent"] = "codex"
        elif kind in {"system", "assistant", "user", "rate_limit_event", "result"}:
            summary["agent"] = "claude"

        session_id = event.get("thread_id") or event.get("session_id")
        if session_id:
            summary["session_id"] = session_id
        if kind == "thread.started":
            summary["started"] = True
            summary["thread_id"] = event.get("thread_id")
        if kind == "system" and (event.get("subtype") == "init" or session_id):
            summary["started"] = True

        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_type = item.get("type") or item.get("item_type")
        if kind in {"item.started", "item.completed"}:
            if item_type == "agent_message" and kind == "item.completed":
                message = _text(item.get("text") or item.get("content"))
                if message:
                    summary["final_text"] = message
            if item_type == "file_change":
                summary["side_effects"] = True
            if item_type == "command_execution" and kind == "item.completed":
                summary["side_effects"] = True
        if kind == "turn.completed":
            summary["terminal"] = True
            if not summary["turn_failed"]:
                summary["terminal_success"] = True
                summary["error"] = ""
                summary["codex_error_info"] = None
            summary["usage"] = event.get("usage")
        if kind in {"turn.failed", "error"}:
            summary["terminal"] = True
            summary["terminal_success"] = False
            if kind == "turn.failed":
                summary["turn_failed"] = True
            summary["error"] = _text(event.get("error") or event.get("message")) or str(kind)

        if kind == "assistant":
            message = event.get("message") if isinstance(event.get("message"), dict) else event
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        summary["final_text"] = str(block["text"])
                    if block.get("type") == "tool_use" and block.get("name") in {
                            "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"}:
                        summary["side_effects"] = True
            if isinstance(message, dict) and message.get("usage"):
                summary["usage"] = message["usage"]
        if kind == "rate_limit_event":
            summary["rate_limit"] = _text(event) or "rate limit"
        if kind == "result":
            summary["terminal"] = True
            final = _text(event.get("result"))
            if final:
                summary["final_text"] = final
            summary["usage"] = event.get("usage") or summary["usage"]
            if event.get("is_error"):
                summary["terminal_success"] = False
                summary["error"] = final or str(event.get("subtype") or "claude error")
            elif event.get("subtype") == "success":
                summary["terminal_success"] = True
                summary["error"] = ""
                summary["codex_error_info"] = None

        info = _find_key(event, "codex_error_info")
        if info not in (None, "", {}):
            summary["codex_error_info"] = info
        denials.extend(command_denials(event))
    summary["denials"] = list(dict.fromkeys(denials))
    return summary


def warnings_from(summary: dict[str, Any], stderr: str) -> list[str]:
    warnings = [str(item) for item in summary.get("denials", []) if item]
    for line in stderr.splitlines():
        if DENIAL_TEXT.search(line):
            warnings.append(line.strip()[:500])
    return list(dict.fromkeys(warnings))


def classify(summary: dict[str, Any], exit_code: int, stderr: str, mode: str) -> str:
    """Return done, quota, transient, permanent, or needs-review."""
    if exit_code == 0 and summary.get("terminal_success"):
        return "done"

    code = _structured_code(summary.get("codex_error_info"))
    if code in STRUCTURED:
        return STRUCTURED[code]

    error = str(summary.get("error") or "")
    blob = "\n".join((str(summary.get("final_text") or ""), error,
                       str(summary.get("rate_limit") or ""), stderr))
    if ACTIVE_WRITER_TEXT.search(blob):
        return "permanent"
    adapter = ClaudeAdapter() if summary.get("agent") == "claude" else CodexAdapter()
    inherited = adapter.classify(RunResult(
        ok=False,
        text=str(summary.get("final_text") or ""),
        exit_code=exit_code,
        raw={"error": error or summary.get("rate_limit") or "",
             "codex_error_info": summary.get("codex_error_info") or ""},
        stderr=stderr,
    ))
    if inherited in {"done", "quota", "transient"}:
        return inherited
    if PERMANENT_TEXT.search(blob):
        return "permanent"
    if mode == "write" and summary.get("side_effects"):
        return "needs-review"
    return "transient"
