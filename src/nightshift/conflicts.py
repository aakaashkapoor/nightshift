"""Rebase-conflict helpers: regenerate lockfiles with plain git, and count conflicts per file.

Lockfiles are never worth an LLM call: take the base's version, regenerate from the
merged manifest, stage the result. ``ConflictLog`` keeps a per-file tally so the files
that conflict most can be seen in ``nsh status`` (and made registries or split up).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .check import run_check
from .gitutil import LOCK as _GIT_LOCK

LOCKFILES = {
    "package-lock.json": "npm install --package-lock-only --ignore-scripts",
    "pnpm-lock.yaml": "pnpm install --lockfile-only",
    "yarn.lock": "yarn install --mode update-lockfile",
    "uv.lock": "uv lock",
    "poetry.lock": "poetry lock --no-update",
    "Cargo.lock": "cargo generate-lockfile",
}


def _git(cwd: Path | str, *args: str) -> subprocess.CompletedProcess:
    with _GIT_LOCK:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )


def conflicted_files(worktree: Path | str) -> list[str]:
    """Paths git lists as unmerged (a rebase stopped on them)."""
    out = _git(worktree, "diff", "--name-only", "--diff-filter=U").stdout
    return [line for line in out.splitlines() if line.strip()]


def regenerate_lockfiles(worktree: Path | str, files: list[str]) -> list[str]:
    """For each conflicted lockfile: take the base's version, regenerate, stage it.

    Returns the lockfiles it fixed. During a rebase, 'ours' is the base being rebased onto.
    """
    fixed = []
    for f in files:
        cmd = LOCKFILES.get(Path(f).name)
        if cmd is None:
            continue
        _git(worktree, "checkout", "--ours", "--", f)
        if run_check(cmd, Path(worktree) / Path(f).parent).passed:
            _git(worktree, "add", "--", f)
            fixed.append(f)
    return fixed


class ConflictLog:
    """Conflict counts per file, persisted in ``<repo home>/conflicts.json``."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def counts(self) -> dict[str, int]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(k): int(v) for k, v in data.items()}

    def record(self, files: list[str], how: str) -> None:
        """Count one conflict for each file. ``how`` (lockfile|agent|unresolved) is for events."""
        counts = self.counts()
        for f in files:
            counts[f] = counts.get(f, 0) + 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(counts, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
