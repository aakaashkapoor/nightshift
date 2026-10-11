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


_INSTALLS: list[tuple[str, str]] = [
    ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
    ("package-lock.json", "npm ci"),
    ("yarn.lock", "yarn install --frozen-lockfile"),
    ("uv.lock", "uv sync --frozen"),
]


def detect_install(repo_path: Path | str) -> str | None:
    """The per-worktree dependency install, from the lockfile present."""
    p = Path(repo_path)
    for lockfile, command in _INSTALLS:
        if (p / lockfile).exists():
            return command
    return None


def detect_sync(repo_path: Path | str) -> str | None:
    """The post-merge sync in the main checkout: the same lockfile-exact install."""
    return detect_install(repo_path)


def detect_check(repo_path: Path | str) -> str | None:
    p = Path(repo_path)
    for marker, command in _MARKERS:
        if (p / marker).exists():
            return command
    return None
