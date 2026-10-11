"""Where Nightshift keeps per-repo machine-local files (events, state, runtime)."""

from __future__ import annotations

from pathlib import Path


def repo_home(name: str, root: Path | None = None) -> Path:
    base = root if root is not None else Path.home() / ".nightshift"
    return base / "repos" / name
