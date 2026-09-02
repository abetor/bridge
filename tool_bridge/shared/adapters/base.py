"""Harness port for LLM work through interchangeable subscription-backed CLIs.

The command-line mechanics and failure boundaries were validated in live use.
Do not change flags or patterns without another live check.

The minimum harness contract is a headless one-shot process with a prompt and
working directory, a textual final response on stdout, and a distinguishable
failure. Other abilities are capability flags; the caller emulates missing
features instead of claiming the harness provides them. This module owns the
done/quota/transient/fatal classifier because no machine-readable quota probe
exists across the supported CLIs.
"""
from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

# These patterns are shared by CLI harnesses; adapters add quota patterns.
#
# CLASS BOUNDARY: 429 and "rate limit" are transient throttling, not exhausted
# subscription quota. Misclassifying a short concurrency burst as quota returns
# exit 75 and can make an outer driver drop queued work that only needed a short
# wait. Classes are separated by the remedy, not by a shared word such as limit.
#
# _QUOTA means the subscription budget is exhausted. The only remedy is waiting
# for a reset window, often hours or days. Match budget language, not request rate.
# Observed forms include:
# claude - "Claude usage limit reached ... resets at", "You've hit your weekly limit",
# "5-hour limit reached"; codex - "You've hit your usage limit", "out of credits".
# The negative lookbehind is intentional: "usage limit exceeded" is quota, while
# "rate limit exceeded" is the common wording for transient throttling.
_QUOTA = re.compile(r"usage limit|weekly limit|session limit|5-hour|out of credits|quota"
                    r"|(?:hit|reached) your [^.\n]{0,24}limit"
                    r"|(?<!rate[ -])\blimit (?:reached|exceeded)", re.I)
# _TRANSIENT means retrying after seconds or minutes can help. It includes burst
# throttling (429, "too many requests", and "rate limit"). Both harnesses retry
# this class internally, so reaching this layer warrants another delayed retry,
# not a quota checkpoint. A mixed message such as "429: usage limit reached" is
# resolved by classifier order: quota wins because budget language is more
# specific and repeatedly hitting a real quota is the riskier mistake.
_TRANSIENT = re.compile(r"\b(?:429|500|502|503|529)\b|too many requests|rate.?limit"
                        r"|at capacity|try a different model|server[_ -]?overloaded|\boverloaded\b|timed?.?out|connection|"
                        r"temporarily|try again|ECONNRESET|EAI_AGAIN", re.I)

# Map stop classes to the public CLI exit-code contract.
STOP_TO_EXIT = {"done": 0, "quota": 75, "transient": 111, "fatal": 1}


@dataclass
class Capabilities:
    """Capabilities provided natively; the caller emulates everything else."""
    json_events: bool
    schema_output: bool
    native_resume: bool
    subagents: bool
    mcp: bool


@dataclass
class RunResult:
    ok: bool
    text: str
    exit_code: int
    session_id: Optional[str] = None
    stop: str = "done"                 # done | quota | transient | fatal
    cost_usd: Optional[float] = None
    raw: dict = field(default_factory=dict)
    stderr: str = ""


class HarnessAdapter(ABC):
    """Per-CLI command construction and parsing; subclasses do not override run()."""
    name: str = "harness"

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def build_cmd(self, prompt: str, *, model: Optional[str] = None,
                  effort: Optional[str] = None, resume_session_id: Optional[str] = None,
                  schema_path: Optional[str] = None, system_prompt_path: Optional[str] = None,
                  allowed_tools: Optional[str] = None) -> list[str]: ...

    @abstractmethod
    def parse_output(self, stdout: str, exit_code: int) -> RunResult: ...

    def quota_patterns(self) -> Optional[re.Pattern]:
        """Return per-CLI quota patterns in addition to the shared patterns."""
        return None

    def classify(self, result: RunResult) -> str:
        """Classify as done, quota, transient, or fatal in contract order.

        1. Quota is checked even for exit 0 because a harness may return success
           alongside "you have hit your limit". It also wins mixed messages.
        2. A successful result is done. Throttling is checked afterward so a
           recovered 429, or source material discussing rate limits, cannot turn
           a successful run into a transient failure.
        3. Transient patterns cover burst throttling and network failures on an
           unsuccessful invocation.

        Raw fields are part of the port contract: subtype/api_error_status for
        Claude, and error/codex_error_info for Codex JSONL events.
        """
        blob = f"{result.text}\n{result.stderr}\n{result.raw.get('subtype', '')}\n" \
               f"{result.raw.get('api_error_status', '')}\n{result.raw.get('error', '')}\n" \
               f"{result.raw.get('codex_error_info', '')}"
        extra = self.quota_patterns()
        if _QUOTA.search(blob) or (extra and extra.search(blob)):
            return "quota"
        if result.ok:
            return "done"
        if _TRANSIENT.search(blob):
            return "transient"
        return "fatal"

    def run(self, prompt: str, *, cwd: str, timeout: Optional[float] = None,
            **build_kw) -> RunResult:
        """Invoke a harness once. This is the only process-spawning location."""
        cmd = self.build_cmd(prompt, **build_kw)
        try:
            # stdin=DEVNULL prevents Codex from reading inherited stdin until EOF
            # and hanging after "Reading additional input from stdin...".
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") if isinstance(e.stdout, str) else ""
            return RunResult(ok=False, text=out, exit_code=-1,
                             stderr="wall-timeout", stop="transient")
        res = self.parse_output(proc.stdout, proc.returncode)
        res.stderr = proc.stderr or ""
        res.stop = self.classify(res)
        if res.stop != "done":
            res.ok = False
        return res
