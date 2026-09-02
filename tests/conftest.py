"""Isolate tests from user files and credentials by replacing HOME, cwd, and env.

An accidental read from the real home directory or write to the caller's cwd
can silently damage local state. Autouse fixtures make every test hermetic by
construction.
"""
import os
import re
import sys
from pathlib import Path

import pytest

# Import the package without installation so tests can run from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Hide environment variables declared in .env.sample, including commented
# examples. This prevents a real credential inherited by the test process from
# turning a supposedly hermetic test into a live API call. Only declared names
# are removed; broad heuristics over os.environ could break unrelated variables.
_ENV_VAR_RE = re.compile(r"([A-Z][A-Z0-9_]*)=")


def _env_sample_vars(sample: Path | None = None) -> list[str]:
    """Return variable names from lines formatted as ``[# ]NAME= # purpose``."""
    if sample is None:
        sample = Path(__file__).resolve().parents[1] / ".env.sample"
    if not sample.is_file():
        return []
    names = []
    for line in sample.read_text("utf-8").splitlines():
        m = _ENV_VAR_RE.match(line.lstrip("# ").strip())
        if m:
            names.append(m.group(1))
    return names


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    # The underscore avoids collisions with tests that create tmp_path/"home".
    home = tmp_path / "_isolated_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TOOLS_DATA", raising=False)
    monkeypatch.chdir(tmp_path)
    fakes = Path(__file__).resolve().parent / "fakes"
    monkeypatch.setenv("PATH", str(fakes) + os.pathsep + os.environ.get("PATH", ""))
    for name in tuple(os.environ):
        if name.startswith("CLAUDE") or name.startswith("CODEX"):
            monkeypatch.delenv(name, raising=False)
    for name in _env_sample_vars():
        monkeypatch.delenv(name, raising=False)
