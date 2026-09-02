"""Filesystem primitives for atomic writes and self-ignored state directories."""
from __future__ import annotations

import os
from pathlib import Path

# Self-ignore makes the directory invisible to Git in any repository, without
# relying on the repository's root .gitignore. State cannot enter `git add .`.
GITIGNORE_BODY = "# Tool state directory; not part of the repository\n*\n"


def atomic_write(path: str | Path, data: str | bytes, encoding: str = "utf-8") -> None:
    """Write without partial states using a sibling temp file and os.replace.

    Readers never observe an incomplete destination. A crashed process may
    leave a temporary fragment, but never a corrupted original.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding=encoding)
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


def ensure_state_dir(path: str | Path) -> Path:
    """Create a state directory with a self-ignoring .gitignore and return it.

    The operation is idempotent and never overwrites an existing .gitignore.
    Exclusive creation avoids clobbering during concurrent initialization.
    """
    d = Path(path)
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        try:
            with open(gi, "x", encoding="utf-8") as f:
                f.write(GITIGNORE_BODY)
        except FileExistsError:
            pass  # Another process created it after exists(); that is sufficient.
    return d
