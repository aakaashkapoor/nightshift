"""Manage one isolated git worktree per slice (SPEC §6).

Every coding task gets its own worktree on branch ``nightshift/<slice-id>``, which
is what makes parallel Work safe (no shared index, no stepping on each other's
files). A worktree is created for a slice's whole lifecycle and torn down after
merge — or *preserved* for inspection when a slice is blocked.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .gitutil import LOCK as _GIT_LOCK

BRANCH_PREFIX = "nightshift/"


def _force_rmtree(path: Path) -> None:
    """Recursively delete a directory, tolerating Windows >260-char paths."""
    target = rf"\\?\{path}" if os.name == "nt" else str(path)
    shutil.rmtree(target, ignore_errors=True)


def _link_dir(target: Path, link: Path) -> None:
    """Link ``link`` -> ``target`` for a directory, cross-platform.

    Windows uses a directory *junction* (no admin / developer-mode needed); POSIX
    uses a symlink.
    """
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    else:  # pragma: no cover - POSIX branch; the gate runs on Windows
        os.symlink(target, link, target_is_directory=True)


def _unlink_dir(link: Path) -> None:
    """Remove a directory link WITHOUT deleting the target's contents.

    Critical before ``git worktree remove``: otherwise git recurses into the
    junction (long Windows paths, and it could delete the real node_modules).
    """
    if os.name == "nt":
        # `rmdir` (no /s) removes a junction reparse point, leaving the target.
        subprocess.run(
            ["cmd", "/c", "rmdir", str(link)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    else:  # pragma: no cover - POSIX branch; the gate runs on Windows
        link.unlink()


def kill_processes_in(path: Path) -> None:  # pragma: no cover - real process shim
    """Kill every process whose command line or executable lives under ``path``.

    A leftover dev server or test runner holds files open and makes
    ``git worktree remove`` fail on Windows. Best effort: never raises.
    """
    try:
        if os.name == "nt":
            quoted = str(path).replace("'", "''")
            script = (
                "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and "
                f"($_.CommandLine -like '*{quoted}*' -or $_.ExecutablePath -like '{quoted}*') }} "
                "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
            )
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        else:
            argv = ["pkill", "-f", str(path)]
        subprocess.run(argv, capture_output=True, timeout=60)
    except Exception:
        return


@dataclass
class Worktree:
    slice_id: str
    path: Path
    branch: str


class WorktreeManager:
    def __init__(
        self,
        repo_path: Path | str,
        worktrees_root: Path | str | None = None,
        symlink_dirs: list[str] | None = None,
        killer: Callable[[Path], None] = kill_processes_in,
        sleep: Callable[[float], None] = time.sleep,
        registries: Sequence[str] = (),
    ):
        self.repo = Path(repo_path).resolve()
        if worktrees_root is not None:
            self.root = Path(worktrees_root).resolve()
        else:
            self.root = self.repo.parent / ".nightshift-worktrees" / self.repo.name
        # Gitignored dirs (e.g. node_modules, .venv) linked into each worktree so
        # the check can run without a per-worktree reinstall (SPEC §6).
        self.symlink_dirs = list(symlink_dirs or [])
        self.killer = killer
        self.sleep = sleep
        # Append-only files (barrels, route tables) that merge with git's union driver.
        self.registries = list(registries)

    def _git(self, *args: str) -> str:
        with _GIT_LOCK:
            result = subprocess.run(
                ["git", "-C", str(self.repo), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def branch_for(self, slice_id: str) -> str:
        return f"{BRANCH_PREFIX}{slice_id}"

    def path_for(self, slice_id: str) -> Path:
        return self.root / slice_id

    def exists(self, slice_id: str) -> bool:
        return self.path_for(slice_id).exists()

    def _info_file(self, name: str) -> Path:
        """A file in the clone-local, never-committed <git-common-dir>/info/."""
        common = Path(self._git("rev-parse", "--git-common-dir").strip())
        if not common.is_absolute():
            common = self.repo / common
        return common / "info" / name

    @staticmethod
    def _ensure_line(file: Path, line: str) -> None:
        """Make sure ``line`` is in ``file`` (created if needed); idempotent."""
        file.parent.mkdir(parents=True, exist_ok=True)
        current = file.read_text(encoding="utf-8") if file.exists() else ""
        if line not in current.splitlines():
            prefix = current.rstrip("\n") + "\n" if current.strip() else ""
            file.write_text(prefix + line + "\n", encoding="utf-8")

    def _exclude_nightshift_dir(self) -> None:
        """Keep the agent's .nightshift/ (plan, result) out of every commit."""
        self._ensure_line(self._info_file("exclude"), ".nightshift/")

    def _union_merge_registries(self) -> None:
        """Registry files merge with git's union driver, so parallel appends don't conflict."""
        for path in self.registries:
            self._ensure_line(self._info_file("attributes"), f"{path} merge=union")

    def _setup(self, path: Path) -> None:
        """Per-clone git config and shared-dir links for a freshly added worktree."""
        self._exclude_nightshift_dir()
        self._union_merge_registries()
        for name in self.symlink_dirs:
            src, dst = self.repo / name, path / name
            if src.exists() and not dst.exists():
                _link_dir(src, dst)

    def create(self, slice_id: str, base_branch: str = "main") -> Worktree:
        self.root.mkdir(parents=True, exist_ok=True)
        # Deep dependency dirs (node_modules) blow past Windows' 260-char limit;
        # let git handle long paths so worktree add/remove don't fail.
        self._git("config", "core.longpaths", "true")
        path = self.path_for(slice_id)
        branch = self.branch_for(slice_id)
        self._git("worktree", "add", "-b", branch, str(path), base_branch)
        self._setup(path)
        return Worktree(slice_id=slice_id, path=path, branch=branch)

    def checkout_remote(self, slice_id: str) -> Worktree:
        """A worktree on origin's copy of the slice branch (its PR), replacing a stale one."""
        self.root.mkdir(parents=True, exist_ok=True)
        self._git("config", "core.longpaths", "true")
        path = self.path_for(slice_id)
        branch = self.branch_for(slice_id)
        if path.exists():
            self.teardown(slice_id)
        self._git("worktree", "prune")
        self._git("fetch", "origin", branch)
        self._git("worktree", "add", "-B", branch, str(path), f"origin/{branch}")
        self._setup(path)
        return Worktree(slice_id=slice_id, path=path, branch=branch)

    def teardown(self, slice_id: str) -> None:
        """Remove the worktree and delete its branch (post-merge cleanup)."""
        path = self.path_for(slice_id)
        # Drop the shared-dir links FIRST so git doesn't recurse into them.
        for name in self.symlink_dirs:
            link = path / name
            if link.exists():
                _unlink_dir(link)
        if not self._remove(path):
            # Deps materialized into a deep real dir git can't delete — force it.
            _force_rmtree(path)
            with contextlib.suppress(RuntimeError):
                self._git("worktree", "prune")
        with contextlib.suppress(RuntimeError):
            self._git("branch", "-D", self.branch_for(slice_id))  # may be pruned

    def _remove(self, path: Path) -> bool:
        """`git worktree remove`; on failure kill what holds the dir and retry (3x)."""
        for attempt in range(4):
            if attempt:
                self.killer(path)
                self.sleep(1)
            try:
                self._git("worktree", "remove", "--force", str(path))
                return True
            except RuntimeError:
                continue
        return False

    def preserve(self, slice_id: str) -> Worktree:
        """Leave the worktree in place (for a blocked slice); return its handle."""
        return Worktree(slice_id, self.path_for(slice_id), self.branch_for(slice_id))

    def attach(self, slice_id: str) -> Worktree:
        """Handle to an EXISTING worktree (for resume); raise if it's gone."""
        if not self.exists(slice_id):
            raise FileNotFoundError(
                f"no preserved worktree for {slice_id!r} at {self.path_for(slice_id)}"
            )
        return Worktree(slice_id, self.path_for(slice_id), self.branch_for(slice_id))

    def list_slices(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())
