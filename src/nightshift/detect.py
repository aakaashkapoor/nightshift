"""Auto-detect a repo's check command for the concierge (SPEC §11).

Best-effort guess so setup can pre-fill the verification gate; the user confirms
or edits. Language-specific markers win; a Makefile is the fallback.
"""

from __future__ import annotations

from pathlib import Path

_MARKERS: list[tuple[str, str]] = [
    ("pyproject.toml", "pytest"),
    ("setup.py", "pytest"),
    ("package.json", "npm test"),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
    ("Makefile", "make test"),
]


def detect_check(repo_path: Path | str) -> str | None:
    p = Path(repo_path)
    for marker, command in _MARKERS:
        if (p / marker).exists():
            return command
    return None
