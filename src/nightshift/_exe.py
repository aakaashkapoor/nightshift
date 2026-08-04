"""Locate external executables robustly.

`shutil.which` only searches PATH — but on Windows a freshly-updated PATH often
isn't visible to an already-running terminal, so tools like `gh` "disappear". This
falls back to well-known install locations so Nightshift doesn't hard-crash.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Standard install dirs to check when a tool isn't on PATH.
GH_DIRS = [r"C:\Program Files\GitHub CLI", r"C:\Program Files (x86)\GitHub CLI"]


def which(name: str, extra_dirs: list[str] | None = None) -> str | None:
    """Resolve ``name`` via PATH, else via ``extra_dirs``; None if not found."""
    found = shutil.which(name)
    if found:
        return found
    suffix = ".exe" if os.name == "nt" else ""
    for directory in extra_dirs or []:
        candidate = Path(directory) / f"{name}{suffix}"
        if candidate.is_file():
            return str(candidate)
    return None
