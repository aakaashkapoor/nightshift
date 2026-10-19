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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .check import run_check
from .config import Config
from .conflicts import conflicted_files, regenerate_lockfiles
from .executor import Executor
from .gates import content_problems
from .gitutil import LOCK as _GIT_LOCK
from .limits import UsageLimitReached
from .prompts import check_failed_prompt, repair_prompt
from .result import AgentResult, clear_result, read_result
from .slice import Slice
from .worktree import WorktreeManager

CONTINUE_PROMPT = (
    "You were interrupted by the Claude usage limit and the limit has now reset. "
    "Continue the same task from where you stopped: check what is already done in "
    "this worktree, finish the rest, and make sure the acceptance criteria are met. "
    "Do NOT commit, push, or create branches — the Nightshift harness handles git."
)


@dataclass
class SliceResult:
    slice_id: str
    status: str  # "done" | "blocked"
    attempts: int
    commit: str | None
    branch: str
    detail: str
    session_id: str | None = None  # last agent session (for resume, SPEC §7)
    decisions: list[str] = field(default_factory=list)
    question: str = ""
    needs_files: list[str] = field(default_factory=list)


def _git(cwd: Path | str, *args: str) -> str:
    with _GIT_LOCK:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_ok(cwd: Path | str, *args: str) -> bool:
    with _GIT_LOCK:
        return (
            subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).returncode
            == 0
        )


def _commit_message(sl: Slice, decisions: list[str] | None = None) -> str:
    # Informative, outcome-focused subject; body ties it to the slice.
    message = f"{sl.title}\n\nImplements slice {sl.id}."
    if decisions:
        message += "\n\nDecisions:\n" + "\n".join(f"- {d}" for d in decisions)
    return message


def _has_changes(path: Path | str) -> bool:
    return bool(_git(path, "status", "--porcelain"))


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


@dataclass
class PrepResult:
    ok: bool
    detail: str


def _has_conflict_markers(cwd: Path | str) -> bool:
    """True if staged content still has leftover conflict markers (ignores whitespace)."""
    with _GIT_LOCK:
        result = subprocess.run(
            ["git", "-C", str(cwd), "diff", "--cached", "--check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    return "conflict marker" in result.stdout


def _resolve_conflicted_rebase(worktree_path, resolver, attempts: int) -> bool:
    """Let the resolver agent edit the worktree until no conflict markers remain.

    Returns True once the stop is resolved and staged (the caller continues the rebase).
    """
    for _ in range(attempts):
        resolver(worktree_path)
        _git(worktree_path, "add", "-A")
        if not _has_conflict_markers(worktree_path):
            return True
    return False


def _continue_rebase(worktree_path) -> bool:
    return _git_ok(worktree_path, "-c", "core.editor=true", "rebase", "--continue")


def prepare_branch(
    *,
    worktree_path: Path | str,
    base_branch: str,
    check_cmd: str,
    resolver=None,
    resolve_attempts: int = 2,
    on_conflict: Callable[[list[str], str], None] | None = None,
) -> PrepResult:
    """Rebase a slice branch onto base and re-run the check, without merging.

    The first half of the merge-train step; babysit's Ship path stops here and
    opens a PR instead of merging (SPEC §9). Every time the rebase stops, lockfiles
    are regenerated with plain git first; the resolver agent only sees what's left.
    ``on_conflict(files, how)`` is told what was fixed and how.
    """

    def report(files: list[str], how: str) -> None:
        if on_conflict is not None and files:
            on_conflict(files, how)

    def give_up(files: list[str], detail: str) -> PrepResult:
        report(files, "unresolved")
        _git_ok(worktree_path, "rebase", "--abort")
        return PrepResult(False, detail)

    rebased = _git_ok(worktree_path, "rebase", base_branch)
    while not rebased:
        files = conflicted_files(worktree_path)
        fixed = regenerate_lockfiles(worktree_path, files)
        rest = [f for f in files if f not in fixed]
        report(fixed, "lockfile")
        if rest:
            if resolver is None:
                return give_up(rest, "rebase conflict")
            if not _resolve_conflicted_rebase(worktree_path, resolver, resolve_attempts):
                return give_up(rest, "unresolved conflict")
            report(rest, "agent")
        elif not fixed:  # the rebase failed with nothing to resolve
            return give_up([], "rebase conflict")
        rebased = _continue_rebase(worktree_path)
    if not run_check(check_cmd, worktree_path).passed:  # pragma: no cover - post-rebase red
        return PrepResult(False, "check failed after rebase")
    return PrepResult(True, "ready")


def sync_base(repo_path: Path | str, base_branch: str, sync_cmd: str | None = None) -> str:
    """Fast-forward the local base from origin (PRs merge on GitHub under babysit).

    Best-effort, never raises: returns a short detail for the log. ``sync_cmd`` runs
    only when the pull actually brought new commits.
    """
    try:
        before = _git(repo_path, "rev-parse", base_branch)
    except RuntimeError as exc:
        return f"pull failed: {exc}"
    if not _git_ok(repo_path, "pull", "--ff-only", "origin", base_branch):
        return "pull failed (will retry next tick)"
    if _git(repo_path, "rev-parse", base_branch) == before:
        return "up to date"
    if sync_cmd:
        result = run_check(sync_cmd, repo_path)
        if not result.passed:
            return "pulled; sync failed: " + result.output[-300:].strip()
    return "pulled"


def integrate_branch(
    *,
    repo_path: Path | str,
    worktree_path: Path | str,
    branch: str,
    base_branch: str,
    check_cmd: str,
    resolver=None,
    resolve_attempts: int = 2,
    push: bool = False,
    sync_cmd: str | None = None,
    on_conflict: Callable[[list[str], str], None] | None = None,
) -> IntegrationResult:
    """Serial merge-train step (SPEC §6): rebase onto base -> re-check -> ff-merge.

    Run one branch at a time so each rebase targets a *stationary* base. On a rebase
    conflict, if a ``resolver`` agent is supplied it edits the worktree to a clean
    merge (up to ``resolve_attempts`` tries); otherwise (or if unresolved) returns
    ``merged=False``. Post-rebase, a red check also returns ``merged=False``.

    A merge that lands is never undone by what happens after it. ``sync_cmd`` (e.g.
    ``npm install``) runs in ``repo_path`` once the merge succeeds — worktrees share
    directories like ``node_modules`` with the repo via a symlink (SPEC §6), but a
    slice's own dependency install can silently replace that symlink with a real,
    worktree-local directory (observed: npm does this rather than writing through a
    junction), leaving the repo's copy stale even though the merge is clean. Then
    ``push``, if set, sends the base branch to ``origin``. Both are best-effort: a
    failure in either is folded into ``detail`` rather than raised, because raising
    here would crash the daemon mid-tick and strand the merged commit un-pushed
    with its worktree never torn down (also observed) instead of just retrying on
    the next slice's sync/push, which reconciles against the *current* lockfile/
    branch state regardless of what this slice added.
    """
    prep = prepare_branch(
        worktree_path=worktree_path,
        base_branch=base_branch,
        check_cmd=check_cmd,
        resolver=resolver,
        resolve_attempts=resolve_attempts,
        on_conflict=on_conflict,
    )
    if not prep.ok:
        return IntegrationResult(False, None, prep.detail)

    try:
        _git(repo_path, "merge", "--ff-only", branch)
    except RuntimeError as exc:  # pragma: no cover - defensive; base moved mid-train
        return IntegrationResult(False, None, f"merge failed: {exc}")

    notes: list[str] = []

    if sync_cmd:
        sync_result = run_check(sync_cmd, repo_path)
        if not sync_result.passed:
            notes.append(
                "post-merge sync failed, will retry on a later successful sync: "
                + sync_result.output[-300:].strip()
            )

    if push:
        try:
            _git(repo_path, "push", "origin", base_branch)
        except RuntimeError as exc:
            notes.append(f"push failed, will retry on a later push: {exc}")

    detail = "merged" if not notes else "merged; " + "; ".join(notes)
    return IntegrationResult(True, _git(repo_path, "rev-parse", base_branch), detail)


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
    resume_session: str | None = None,
    result_contract: bool = False,
    resume_prompt: str | None = None,
    install_cmd: str | None = None,
) -> SliceResult:
    """Work -> check -> review -> one commit.

    ``resume_session`` continues a slice the usage limit interrupted: its preserved
    worktree is reattached and the agent session resumed with ``CONTINUE_PROMPT``.
    Raises ``UsageLimitReached`` if the limit hits; the worktree is left in place.
    """
    resuming = resume_session is not None and worktrees.exists(sl.id)
    wt = worktrees.attach(sl.id) if resuming else worktrees.create(sl.id, base_branch)

    attempts = 0
    last_session: str | None = resume_session if resuming else None

    def _blocked(reason: str) -> SliceResult:
        sl.status = "blocked"
        sl.attempts = attempts
        worktrees.preserve(sl.id)
        return SliceResult(sl.id, "blocked", attempts, None, wt.branch, reason, last_session)

    if install_cmd and not resuming:
        installed = run_check(install_cmd, wt.path)
        if not installed.passed:
            return _blocked(f"install failed: {installed.output[-300:]}")

    # Work: iterate until the check is green (or the cap trips).
    green = False
    prompt: str | None = (resume_prompt or CONTINUE_PROMPT) if resuming else None
    agent: AgentResult | None = None
    while attempts < max_attempts:
        attempts += 1
        clear_result(wt.path)
        if prompt is None:
            exec_result = executor.execute(wt.path, sl, resume_session=last_session)
        else:
            exec_result = executor.execute_prompt(wt.path, prompt, last_session)
        last_session = exec_result.session_id or last_session
        if exec_result.usage_limited:
            raise UsageLimitReached(last_session)
        if result_contract:
            agent, error = read_result(wt.path)
            if agent is None:
                fix = executor.execute_prompt(wt.path, repair_prompt(error), last_session)
                last_session = fix.session_id or last_session
                if fix.usage_limited:
                    raise UsageLimitReached(last_session)
                agent, error = read_result(wt.path)
            if agent is None:
                return _blocked(f"agent wrote no valid result.json: {error}")
            if agent.outcome == "question":
                worktrees.preserve(sl.id)
                return SliceResult(
                    sl.id,
                    "question",
                    attempts,
                    None,
                    wt.branch,
                    agent.question,
                    last_session,
                    question=agent.question,
                    decisions=agent.decisions,
                )
            if agent.outcome == "needs_files":
                worktrees.preserve(sl.id)
                return SliceResult(
                    sl.id,
                    "needs_files",
                    attempts,
                    None,
                    wt.branch,
                    "needs " + ", ".join(agent.needs_files),
                    last_session,
                    needs_files=agent.needs_files,
                    decisions=agent.decisions,
                )
        check = run_check(check_cmd, wt.path)
        if check.passed:
            problems = content_problems(wt.path, base_branch)
            if not problems:
                green = True
                break
            prompt = check_failed_prompt("nightshift content gate", "\n".join(problems))
        else:
            prompt = check_failed_prompt(check_cmd, check.output)

    if not green:
        return _blocked(f"check failed after {attempts} attempt(s)")

    # Ship gate: always-on AI review (blocking findings loop back to a fix round).
    if reviewer is not None:
        review = None
        for _ in range(review_rounds):
            try:
                review = reviewer.review(wt.path, sl)
            except UsageLimitReached as exc:
                raise UsageLimitReached(last_session, reset_session=exc.reset_session) from exc
            if not review.blocking:
                break
            from .review import fix_prompt

            res = executor.execute_prompt(wt.path, fix_prompt(review.blocking), last_session)
            last_session = res.session_id or last_session
            if res.usage_limited:
                raise UsageLimitReached(last_session)
            if not run_check(check_cmd, wt.path).passed:
                return _blocked("review fix broke the check")
        else:
            # Keep the findings: babysit can't see the reviewer's session, so without them
            # the block says nothing about what to fix.
            blocking = review.blocking if review is not None else []
            _write_findings(Path(wt.path), blocking)
            summary = "; ".join(f.message for f in blocking)
            return _blocked(f"blocking review findings unresolved: {summary}"[:500])

    if not resuming and not _has_changes(wt.path):
        note = (exec_result.result_text or "").strip()[:300]
        return _blocked(f"agent made no changes: {note}")

    decisions = agent.decisions if agent is not None else []
    commit_fn = _commit_if_changes if resuming else _commit_all
    commit = commit_fn(wt.path, _commit_message(sl, decisions))
    sl.status = "done"
    sl.attempts = attempts
    return SliceResult(
        sl.id,
        "done",
        attempts,
        commit,
        wt.branch,
        "check passed",
        last_session,
        decisions=decisions,
    )


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
        worktrees=WorktreeManager(
            repo_cfg.path, symlink_dirs=repo_cfg.symlink_dirs, registries=repo_cfg.registries
        ),
        executor=executor if executor is not None else Executor.for_repo(repo_cfg),
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
    """`nsh resume` glue: resume a blocked slice on its worktree, then ship it the way the
    daemon would for this repo (a PR under babysit, the local merge train otherwise)."""
    from .daemon import build_daemon  # the daemon imports this module

    daemon = build_daemon(
        repo, config_path=config_path, executor=executor, runtime_path=runtime_path
    )
    sl = daemon.source.get(slice_id)
    if sl is None:
        raise FileNotFoundError(f"no slice {slice_id!r} in {daemon.repo_cfg.path}")
    assert daemon.runtime is not None  # build_daemon always wires one
    resumed = resume_slice(
        sl,
        check_cmd=daemon.repo_cfg.check,
        worktrees=daemon.worktrees,
        executor=daemon.executor,
        session_id=daemon.runtime.get(slice_id).get("session_id"),
    )
    if resumed.status != "done":
        daemon.source.set_blocked(slice_id, resumed.detail)
        daemon.runtime.record(slice_id, session_id=resumed.session_id, branch=resumed.branch)
        return resumed
    return daemon.finish(resumed)


def _write_findings(worktree: Path, findings) -> None:
    """Write unresolved blocking findings to .nightshift/review-findings.md in the worktree."""
    out = worktree / ".nightshift" / "review-findings.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Unresolved blocking review findings", ""] + [f"- {f.message}" for f in findings]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
