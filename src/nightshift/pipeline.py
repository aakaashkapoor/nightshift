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
from .runtime import Runtime
from .slice import Slice
from .source import LocalMdSource
from .worktree import WorktreeManager


@dataclass
class SliceResult:
    slice_id: str
    status: str  # "done" | "blocked"
    attempts: int
    commit: str | None
    branch: str
    detail: str
    session_id: str | None = None  # last agent session (for resume, SPEC §7)


def _git(cwd: Path | str, *args: str) -> str:
    with _GIT_LOCK:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
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


def _commit_if_changes(worktree_path: Path, message: str) -> str:
    """Commit staged changes if any; otherwise return the current HEAD (for resume)."""
    _git(worktree_path, "add", "-A")
    if _git_ok(worktree_path, "diff", "--cached", "--quiet"):  # rc 0 = nothing staged
        return _git(worktree_path, "rev-parse", "HEAD")
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
        if _git_ok(
            worktree_path, "-c", "core.editor=true", "rebase", "--continue"
        ):  # pragma: no branch
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

    if not run_check(check_cmd, worktree_path).passed:  # pragma: no cover - post-rebase red
        return IntegrationResult(False, None, "check failed after rebase")

    try:
        _git(repo_path, "merge", "--ff-only", branch)
    except RuntimeError as exc:  # pragma: no cover - defensive; base moved mid-train
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
    reviewer=None,
    review_rounds: int = 2,
) -> SliceResult:
    wt = worktrees.create(sl.id, base_branch)

    # Work: iterate until the check is green (or the cap trips).
    green = False
    attempts = 0
    last_session: str | None = None
    while attempts < max_attempts:
        attempts += 1
        exec_result = executor.execute(wt.path, sl, resume_session=last_session)
        last_session = exec_result.session_id or last_session
        if run_check(check_cmd, wt.path).passed:
            green = True
            break

    def _blocked(reason: str) -> SliceResult:
        sl.status = "blocked"
        sl.attempts = attempts
        worktrees.preserve(sl.id)
        return SliceResult(sl.id, "blocked", attempts, None, wt.branch, reason, last_session)

    if not green:
        return _blocked(f"check failed after {attempts} attempt(s)")

    # Ship gate: always-on AI review (blocking findings loop back to a fix round).
    if reviewer is not None:
        for _ in range(review_rounds):
            review = reviewer.review(wt.path, sl)
            if not review.blocking:
                break
            from .review import fix_prompt

            res = executor.execute_prompt(wt.path, fix_prompt(review.blocking), last_session)
            last_session = res.session_id or last_session
            if not run_check(check_cmd, wt.path).passed:
                return _blocked("review fix broke the check")
        else:
            return _blocked("blocking review findings unresolved")

    commit = _commit_all(wt.path, _commit_message(sl))
    sl.status = "done"
    sl.attempts = attempts
    return SliceResult(sl.id, "done", attempts, commit, wt.branch, "check passed", last_session)


def resume_slice(
    sl: Slice,
    *,
    check_cmd: str,
    worktrees: WorktreeManager,
    executor: Executor,
    session_id: str | None = None,
    max_attempts: int = 3,
) -> SliceResult:
    """Resume a blocked slice on its PRESERVED worktree (SPEC §7) — never from scratch.

    Attaches to the existing worktree, resumes the agent's session, and re-runs the
    Work→check loop until green (then commits) or the cap trips.
    """
    wt = worktrees.attach(sl.id)
    attempts = 0
    last_session = session_id
    while attempts < max_attempts:
        attempts += 1
        exec_result = executor.execute(wt.path, sl, resume_session=last_session)
        last_session = exec_result.session_id or last_session
        if run_check(check_cmd, wt.path).passed:
            commit = _commit_if_changes(wt.path, _commit_message(sl))
            return SliceResult(
                sl.id, "done", attempts, commit, wt.branch, "resumed & checked", last_session
            )
    return SliceResult(
        sl.id, "blocked", attempts, None, wt.branch, "still blocked after resume", last_session
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
        worktrees=WorktreeManager(repo_cfg.path, symlink_dirs=repo_cfg.symlink_dirs),
        executor=executor if executor is not None else Executor(),
        base_branch=repo_cfg.base_branch,
    )


def run_resume_cli(
    slice_id: str,
    *,
    repo,
    config_path=None,
    executor: Executor | None = None,
    runtime_path=None,
) -> SliceResult:
    """`nsh resume` glue: resume a blocked slice on its worktree, then integrate."""
    cfg = Config.load(config_path)
    repo_cfg = cfg.repo(str(repo))
    source = LocalMdSource(repo_cfg.path)
    sl = source.get(slice_id)
    if sl is None:
        raise FileNotFoundError(f"no slice {slice_id!r} in {source.root}")

    worktrees = WorktreeManager(repo_cfg.path, symlink_dirs=repo_cfg.symlink_dirs)
    runtime = Runtime(runtime_path)
    ex = executor if executor is not None else Executor()

    resumed = resume_slice(
        sl,
        check_cmd=repo_cfg.check,
        worktrees=worktrees,
        executor=ex,
        session_id=runtime.get(slice_id).get("session_id"),
    )
    if resumed.status != "done":
        source.set_blocked(slice_id, resumed.detail)
        runtime.record(slice_id, session_id=resumed.session_id, branch=resumed.branch)
        return resumed

    integ = integrate_branch(
        repo_path=repo_cfg.path,
        worktree_path=worktrees.path_for(slice_id),
        branch=resumed.branch,
        base_branch=repo_cfg.base_branch,
        check_cmd=repo_cfg.check,
    )
    if integ.merged:
        worktrees.teardown(slice_id)
        source.set_status(slice_id, "done")
        runtime.forget(slice_id)
        return SliceResult(
            slice_id,
            "done",
            resumed.attempts,
            integ.commit,
            resumed.branch,
            "resumed & merged",
            resumed.session_id,
        )
    source.set_blocked(slice_id, integ.detail)  # pragma: no cover - resume re-conflict
    return SliceResult(  # pragma: no cover
        slice_id,
        "blocked",
        resumed.attempts,
        resumed.commit,
        resumed.branch,
        integ.detail,
        resumed.session_id,
    )
