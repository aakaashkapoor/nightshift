"""Run one slice through Work -> Ship (SPEC §6).

v0.1 form: no rebase / merge train yet. For a single slice:
  create worktree -> agent implements (Work) -> run check
    -> green: squash to ONE clean commit, mark done
    -> red:  retry (resume the agent) up to ``max_attempts``
    -> exhausted: mark blocked, preserve the worktree for inspection (SPEC §7)

The agent is instructed not to commit, so its work sits uncommitted in the
worktree and a single ``git commit`` produces exactly one clean commit (SPEC §0).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .check import run_check
from .config import Config
from .executor import Executor
from .gitutil import LOCK as _GIT_LOCK
from .slice import Slice
from .worktree import WorktreeManager


@dataclass
class SliceResult:
    slice_id: str
    status: str  # "done" | "blocked"
    attempts: int
    commit: str | None
    branch: str
    detail: str


def _git(cwd: Path | str, *args: str) -> str:
    with _GIT_LOCK:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True
        )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_ok(cwd: Path | str, *args: str) -> bool:
    with _GIT_LOCK:
        return (
            subprocess.run(
                ["git", "-C", str(cwd), *args], capture_output=True, text=True
            ).returncode
            == 0
        )


def _commit_message(sl: Slice) -> str:
    # Informative, outcome-focused subject; body ties it to the slice.
    return f"{sl.title}\n\nImplements slice {sl.id}."


def _commit_all(worktree_path: Path, message: str) -> str:
    _git(worktree_path, "add", "-A")
    _git(worktree_path, "commit", "-m", message)
    return _git(worktree_path, "rev-parse", "HEAD")


@dataclass
class IntegrationResult:
    merged: bool
    commit: str | None
    detail: str


def _has_conflict_markers(cwd: Path | str) -> bool:
    """True if staged content still has leftover conflict markers (ignores whitespace)."""
    with _GIT_LOCK:
        result = subprocess.run(
            ["git", "-C", str(cwd), "diff", "--cached", "--check"],
            capture_output=True,
            text=True,
        )
    return "conflict marker" in result.stdout


def _resolve_conflicted_rebase(worktree_path, resolver, attempts: int) -> bool:
    """Let the resolver agent edit the worktree, then continue the rebase.

    Returns True once the rebase completes cleanly (no leftover conflict markers).
    """
    for _ in range(attempts):
        resolver(worktree_path)
        _git(worktree_path, "add", "-A")
        if _has_conflict_markers(worktree_path):
            continue  # markers remain -> let the resolver try again
        if _git_ok(worktree_path, "-c", "core.editor=true", "rebase", "--continue"):
            return True
    return False


def integrate_branch(
    *,
    repo_path: Path | str,
    worktree_path: Path | str,
    branch: str,
    base_branch: str,
    check_cmd: str,
    resolver=None,
    resolve_attempts: int = 2,
) -> IntegrationResult:
    """Serial merge-train step (SPEC §6): rebase onto base -> re-check -> ff-merge.

    Run one branch at a time so each rebase targets a *stationary* base. On a rebase
    conflict, if a ``resolver`` agent is supplied it edits the worktree to a clean
    merge (up to ``resolve_attempts`` tries); otherwise (or if unresolved) returns
    ``merged=False``. Post-rebase, a red check also returns ``merged=False``.
    """
    if not _git_ok(worktree_path, "rebase", base_branch):
        resolved = resolver is not None and _resolve_conflicted_rebase(
            worktree_path, resolver, resolve_attempts
        )
        if not resolved:
            _git_ok(worktree_path, "rebase", "--abort")
            detail = "unresolved conflict" if resolver else "rebase conflict"
            return IntegrationResult(False, None, detail)

    if not run_check(check_cmd, worktree_path).passed:
        return IntegrationResult(False, None, "check failed after rebase")

    try:
        _git(repo_path, "merge", "--ff-only", branch)
    except RuntimeError as exc:
        return IntegrationResult(False, None, f"merge failed: {exc}")

    return IntegrationResult(True, _git(repo_path, "rev-parse", base_branch), "merged")


def run_slice(
    sl: Slice,
    *,
    repo_path: Path | str,
    check_cmd: str,
    worktrees: WorktreeManager,
    executor: Executor,
    base_branch: str = "main",
    max_attempts: int = 3,
) -> SliceResult:
    wt = worktrees.create(sl.id, base_branch)
    attempts = 0
    last_session: str | None = None

    while attempts < max_attempts:
        attempts += 1
        exec_result = executor.execute(wt.path, sl, resume_session=last_session)
        last_session = exec_result.session_id
        check = run_check(check_cmd, wt.path)
        if check.passed:
            commit = _commit_all(wt.path, _commit_message(sl))
            sl.status = "done"
            sl.attempts = attempts
            return SliceResult(sl.id, "done", attempts, commit, wt.branch, "check passed")

    sl.status = "blocked"
    sl.attempts = attempts
    worktrees.preserve(sl.id)
    return SliceResult(
        sl.id, "blocked", attempts, None, wt.branch,
        f"check failed after {attempts} attempt(s)",
    )


def _load_slice(repo_path: Path | str, slice_arg: str) -> Slice:
    """Resolve a slice from a direct .md path or a repo `.slices/<id>.md`."""
    direct = Path(slice_arg)
    if direct.suffix == ".md" and direct.exists():
        return Slice.load(direct)
    candidate = Path(repo_path) / ".slices" / f"{slice_arg}.md"
    if candidate.exists():
        return Slice.load(candidate)
    raise FileNotFoundError(f"slice not found: {slice_arg!r} (looked in {candidate})")


def run_slice_cli(
    slice_arg: str,
    *,
    repo: Path | str,
    config_path: Path | str | None = None,
    executor: Executor | None = None,
) -> SliceResult:
    """`nsh run` glue: load config, resolve repo + slice, run it.

    ``executor`` defaults to a real headless-Claude Executor; tests inject a fake.
    """
    cfg = Config.load(config_path)
    repo_cfg = cfg.repo(str(repo))
    sl = _load_slice(repo_cfg.path, slice_arg)
    return run_slice(
        sl,
        repo_path=repo_cfg.path,
        check_cmd=repo_cfg.check,
        worktrees=WorktreeManager(repo_cfg.path),
        executor=executor if executor is not None else Executor(),
        base_branch=repo_cfg.base_branch,
    )
