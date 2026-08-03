"""Manage one isolated git worktree per slice (SPEC §6).

Every coding task gets its own worktree on branch ``nightshift/<slice-id>``, which
is what makes parallel Work safe (no shared index, no stepping on each other's
files). A worktree is created for a slice's whole lifecycle and torn down after
merge — or *preserved* for inspection when a slice is blocked.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .gitutil import LOCK as _GIT_LOCK

BRANCH_PREFIX = "nightshift/"


@dataclass
class Worktree:
    slice_id: str
    path: Path
    branch: str


class WorktreeManager:
    def __init__(self, repo_path: Path | str, worktrees_root: Path | str | None = None):
        self.repo = Path(repo_path).resolve()
        if worktrees_root is not None:
            self.root = Path(worktrees_root).resolve()
        else:
            self.root = self.repo.parent / ".nightshift-worktrees" / self.repo.name

    def _git(self, *args: str) -> str:
        with _GIT_LOCK:
            result = subprocess.run(
                ["git", "-C", str(self.repo), *args],
                capture_output=True,
                text=True,
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed: {result.stderr.strip()}"
            )
        return result.stdout

    def branch_for(self, slice_id: str) -> str:
        return f"{BRANCH_PREFIX}{slice_id}"

    def path_for(self, slice_id: str) -> Path:
        return self.root / slice_id

    def exists(self, slice_id: str) -> bool:
        return self.path_for(slice_id).exists()

    def create(self, slice_id: str, base_branch: str = "main") -> Worktree:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(slice_id)
        branch = self.branch_for(slice_id)
        self._git("worktree", "add", "-b", branch, str(path), base_branch)
        return Worktree(slice_id=slice_id, path=path, branch=branch)

    def teardown(self, slice_id: str) -> None:
        """Remove the worktree and delete its branch (post-merge cleanup)."""
        path = self.path_for(slice_id)
        self._git("worktree", "remove", "--force", str(path))
        try:
            self._git("branch", "-D", self.branch_for(slice_id))
        except RuntimeError:
            pass  # branch already gone (e.g. merged & pruned)

    def preserve(self, slice_id: str) -> Worktree:
        """Leave the worktree in place (for a blocked slice); return its handle."""
        return Worktree(slice_id, self.path_for(slice_id), self.branch_for(slice_id))

    def list_slices(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())
