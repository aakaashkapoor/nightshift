"""The Nightshift daemon loop (SPEC §2, §8).

v0.2 skeleton: each *tick* scans the source, builds the dependency DAG, and runs
every currently-runnable slice, writing status back into the source. Sequential for
now (parallelism = 2.4, the serial merge-train = 2.5). ``run_forever`` polls on an
interval.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from pathlib import Path

from .check import run_check
from .config import Config, RepoConfig
from .conflicts import ConflictLog
from .control import Control
from .events import EventLog
from .executor import Executor
from .limits import UsageLimitReached, reset_time, resume_at
from .lint import lint
from .notifier import NullNotifier, build_notifier
from .paths import repo_home
from .pipeline import (
    SliceResult,
    _git,
    _git_ok,
    integrate_branch,
    prepare_branch,
    run_slice,
    sync_base,
)
from .pr import BABYSIT_LABEL, REVIEW_LABEL, GitHubPR, open_pr_for_slice
from .prompts import FILES_PROMPT, REQUEUE_PROMPT
from .resolver import Resolver
from .review import AgentReviewer
from .runtime import Runtime
from .scheduler import Dag, overlaps
from .slice import Slice
from .source import Source, build_source
from .state import StateStore
from .worktree import WorktreeManager

IN_PROGRESS = "in-progress"
DONE = "done"
BLOCKED = "blocked"
IN_REVIEW = "in-review"
LIMITED = "limited"
QUESTION = "question"
NEEDS_FILES = "needs_files"
REQUEUED = "requeued"

log = logging.getLogger("nightshift")
_LOG_FMT = logging.Formatter("%(asctime)s  nightshift  %(message)s", "%H:%M:%S")


def configure_logging(log_file: Path | str | None = None) -> None:
    """Send nightshift logs to stdout (and optionally a file)."""
    log.setLevel(logging.INFO)
    for handler in log.handlers:
        handler.close()
    log.handlers.clear()
    stream = logging.StreamHandler()
    stream.setFormatter(_LOG_FMT)
    log.addHandler(stream)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(_LOG_FMT)
        log.addHandler(file_handler)


class Daemon:
    def __init__(
        self,
        *,
        source: Source,
        repo_cfg: RepoConfig,
        executor: Executor,
        worktrees: WorktreeManager | None = None,
        max_attempts: int = 3,
        max_parallel: int | None = None,
        resolver=None,
        resolve_attempts: int = 2,
        notifier=None,
        runtime=None,
        reviewer=None,
        pr=None,
        events: EventLog | None = None,
        state: StateStore | None = None,
        conflicts: ConflictLog | None = None,
        clock: Callable[[], float] = time.time,
        reset_lookup: Callable[[str | None], float | None] = reset_time,
        control: Control | None = None,
        reload: Callable[[], RepoConfig] | None = None,
        exit_event: str = "daemon_exit",
        lint_issues: bool = False,
        result_contract: bool = False,
    ):
        self.source = source
        self.repo_cfg = repo_cfg
        self.executor = executor
        self.worktrees = worktrees or WorktreeManager(
            repo_cfg.path, symlink_dirs=repo_cfg.symlink_dirs, registries=repo_cfg.registries
        )
        self.max_attempts = max_attempts
        self.max_parallel = max_parallel or repo_cfg.max_parallel
        self.resolver = resolver
        self.resolve_attempts = resolve_attempts
        self.notifier = notifier or NullNotifier()
        self.runtime = runtime
        self.reviewer = reviewer
        self.pr = pr
        self.events = events
        self.state = state
        self.conflicts = conflicts
        self.clock = clock
        self.reset_lookup = reset_lookup
        self.paused_until: float | None = None
        self._reset_sessions: dict[str, str | None] = {}
        self.control = control
        self.reload = reload
        self.exit_event = exit_event
        self.lint_issues = lint_issues
        self.result_contract = result_contract
        self._idle: str | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._running: dict[str, Future[SliceResult]] = {}  # slice id -> work, in start order
        self._held: dict[str, set[str]] = {}  # slice id -> its touches, while running
        self._recovered = False
        self._green: str | None = None  # last base commit whose check passed
        self._red: str | None = None  # last base commit whose check failed (already reported)
        self._waiting: dict[str, str] = {}  # last logged why-not-started map
        self._unknown_deps: set[tuple[str, str]] = set()  # (slice, dep) already warned about
        self._rebase_tried: dict[str, str] = {}  # in-review slice id -> base commit last tried
        self._mode = "running"  # running | paused | stopping, mirrored into state.json

    def _emit(self, kind: str, slice_id: str | None = None, **data) -> None:
        if self.events is not None:
            self.events.emit(kind, slice_id, **data)

    def _phase(self, slice_id: str, phase: str, **fields) -> None:
        if self.state is not None:
            self.state.set_slice(slice_id, phase, **fields)

    def _drop(self, slice_id: str) -> None:
        if self.state is not None:
            self.state.drop_slice(slice_id)

    def _conflict_reporter(self, slice_id: str) -> Callable[[list[str], str], None]:
        def report(files: list[str], how: str) -> None:
            self._emit("conflict", slice_id, files=files, how=how)
            if self.conflicts is not None:
                self.conflicts.record(files, how)
                top = sorted(self.conflicts.counts().items(), key=lambda kv: (-kv[1], kv[0]))[:5]
                self._daemon_state(conflicts=dict(top))

        return report

    def _daemon_state(self, **fields) -> None:
        if self.state is not None:
            self.state.set_daemon(**fields)

    def _run_one(self, sl: Slice) -> SliceResult:
        entry = self.runtime.get(sl.id) if self.runtime is not None else {}
        resume = entry.get("session_id") if entry.get("interrupted") else None
        executor = self.executor.with_model(self.repo_cfg.model_for(sl.size()))
        try:
            return run_slice(
                sl,
                repo_path=self.repo_cfg.path,
                check_cmd=self.repo_cfg.check,
                worktrees=self.worktrees,
                executor=executor,
                base_branch=self.repo_cfg.base_branch,
                max_attempts=self.max_attempts,
                reviewer=self.reviewer,
                resume_session=resume,
                resume_prompt=entry.get("resume_prompt"),
                result_contract=self.result_contract,
                install_cmd=self.repo_cfg.install,
            )
        except UsageLimitReached as exc:
            self._reset_sessions[sl.id] = exc.reset_session
            return SliceResult(
                sl.id,
                LIMITED,
                0,
                None,
                self.worktrees.branch_for(sl.id),
                "usage limit",
                exc.session_id or resume,
            )
        except Exception as exc:  # a git/exec error must not kill the tick
            return SliceResult(
                sl.id, "blocked", 0, None, self.worktrees.branch_for(sl.id), f"error: {exc}"
            )

    def _block(self, work: SliceResult, reason: str) -> SliceResult:
        """Escalation: preserve worktree, note the reason in the issue, notify."""
        self.worktrees.preserve(work.slice_id)
        self.source.set_blocked(work.slice_id, reason)
        self.notifier.notify("blocked", f"{work.slice_id}: {reason}")
        self._emit("blocked", work.slice_id, reason=reason)
        self._phase(work.slice_id, BLOCKED, detail=reason)
        return SliceResult(
            work.slice_id,
            BLOCKED,
            work.attempts,
            work.commit,
            work.branch,
            reason,
            work.session_id,
        )

    def _limited(self, work: SliceResult, reset_session: str | None = None) -> SliceResult:
        """Usage limit: keep the worktree, put the slice back in the queue, pause."""
        self.worktrees.preserve(work.slice_id)
        self.source.set_status(work.slice_id, "ready")
        self._phase(work.slice_id, "queued", detail="usage limit")
        self._pause(
            reset_session or self._reset_sessions.pop(work.slice_id, None) or work.session_id
        )
        return SliceResult(
            work.slice_id,
            LIMITED,
            work.attempts,
            work.commit,
            work.branch,
            "usage limit",
            work.session_id,
        )

    def _pause(self, session_id: str | None) -> None:
        until = resume_at(self.reset_lookup(session_id), self.clock())
        if self.paused_until is not None:
            self.paused_until = max(self.paused_until, until)
            return
        self.paused_until = until
        log.warning(
            "usage limit reached; pausing until %s",
            time.strftime("%H:%M", time.localtime(until)),
        )
        self._emit("paused_limit", until=until)
        self._daemon_state(status="paused_limit", paused_until=until)

    def _integrate(self, work: SliceResult) -> SliceResult:
        """Serial merge-train step for one committed branch (SPEC §6)."""
        if work.status == LIMITED:
            return self._limited(work)
        if work.status == QUESTION:
            return self._ask(work)
        if work.status == NEEDS_FILES:
            return self._request_files(work)
        if work.status == DONE:
            requeued = self._enforce_touches(work)
            if requeued is not None:
                return requeued
        try:
            return self._ship(work)
        except UsageLimitReached as exc:
            _git_ok(self.worktrees.path_for(work.slice_id), "rebase", "--abort")
            return self._limited(work, reset_session=exc.reset_session)

    def _ask(self, work: SliceResult) -> SliceResult:
        """The agent asked the owner something: park the issue until `nsh answer`."""
        self.source.set_needs_detail(
            work.slice_id,
            f"Question from the agent:\n\n{work.question}\n\n"
            f'Answer with: nsh answer {work.slice_id} "..."',
        )
        if self.runtime is not None:
            self.runtime.record(
                work.slice_id,
                session_id=work.session_id,
                branch=work.branch,
                interrupted=False,
                awaiting_answer=True,
            )
        self._emit("question", work.slice_id, question=work.question)
        self._phase(work.slice_id, QUESTION, detail=work.question)
        return work

    def _add_touches(self, slice_id: str, paths: list[str]) -> None:
        """Append to '## Touches', first carrying over legacy '## Scope hints' paths."""
        sl = self.source.get(slice_id)
        if sl is not None and sl.section("Touches") is None:
            for path in sorted(sl.touches()):
                self.source.append_to_section(slice_id, "Touches", f"- {path}")
        for path in paths:
            self.source.append_to_section(slice_id, "Touches", f"- {path}")

    def _request_files(self, work: SliceResult) -> SliceResult:
        """The agent needs more files: add them to Touches and requeue to resume."""
        self._add_touches(work.slice_id, work.needs_files)
        self.source.set_status(work.slice_id, "ready")
        if self.runtime is not None:
            self.runtime.record(
                work.slice_id,
                session_id=work.session_id,
                branch=work.branch,
                interrupted=True,
                resume_prompt=FILES_PROMPT.format(files=", ".join(work.needs_files)),
            )
        self._emit("needs_files", work.slice_id, files=work.needs_files)
        self._phase(work.slice_id, "queued", detail=work.detail)
        return work

    def _outside_touches(self, work: SliceResult, touches: set[str]) -> list[str]:
        """Existing files the slice modified that its Touches doesn't cover."""
        out = _git(
            self.worktrees.path_for(work.slice_id),
            "diff",
            "--name-only",
            "--diff-filter=MDR",
            f"{self.repo_cfg.base_branch}...HEAD",
        )
        return [f for f in out.splitlines() if f and not overlaps({f}, touches)]

    def _enforce_touches(self, work: SliceResult) -> SliceResult | None:
        """Strict file list: None to ship; a requeued result when another slice owns a file."""
        sl = self.source.get(work.slice_id)
        touches = sl.touches() if sl is not None else set()
        if not touches:
            return None
        outside = self._outside_touches(work, touches)
        if not outside:
            return None
        self._add_touches(work.slice_id, outside)
        holder = next(
            (h for h, held in self._held.items() if any(overlaps({f}, held) for f in outside)),
            None,
        )
        files = ", ".join(outside)
        if holder is None:
            log.info("%s also modified %s; added to Touches", work.slice_id, files)
            return None
        self.worktrees.preserve(work.slice_id)
        self.source.set_status(work.slice_id, "ready")
        if self.runtime is not None:
            self.runtime.record(
                work.slice_id,
                session_id=work.session_id,
                branch=work.branch,
                interrupted=True,
                resume_prompt=REQUEUE_PROMPT.format(files=files),
            )
        self._emit(REQUEUED, work.slice_id, files=outside, holder=holder)
        self._phase(work.slice_id, "queued", detail=f"{files} is held by {holder}")
        return SliceResult(
            work.slice_id,
            REQUEUED,
            work.attempts,
            work.commit,
            work.branch,
            f"{files} is held by {holder}",
            work.session_id,
        )

    def _ship(self, work: SliceResult) -> SliceResult:
        if work.status != DONE:  # Work itself failed -> nothing to integrate
            return self._block(work, work.detail)
        if self.repo_cfg.babysit:
            return self._ship_pr(work)
        result = integrate_branch(
            repo_path=self.repo_cfg.path,
            worktree_path=self.worktrees.path_for(work.slice_id),
            branch=work.branch,
            base_branch=self.repo_cfg.base_branch,
            check_cmd=self.repo_cfg.check,
            resolver=self.resolver,
            resolve_attempts=self.resolve_attempts,
            push=self.repo_cfg.push,
            sync_cmd=self.repo_cfg.sync,
            on_conflict=self._conflict_reporter(work.slice_id),
        )
        if result.merged:
            self._green = result.commit  # that commit was checked before it merged
            self.worktrees.teardown(work.slice_id)
            self.source.set_status(work.slice_id, DONE)
            self._emit("merged", work.slice_id, commit=result.commit)
            self._drop(work.slice_id)
            return SliceResult(
                work.slice_id,
                DONE,
                work.attempts,
                result.commit,
                work.branch,
                "merged",
                work.session_id,
            )
        return self._block(work, result.detail)

    def _ship_pr(self, work: SliceResult) -> SliceResult:
        """PR Ship (SPEC §9): rebase + re-check, open a PR, merge it unless the issue wants review."""
        prep = prepare_branch(
            worktree_path=self.worktrees.path_for(work.slice_id),
            base_branch=self.repo_cfg.base_branch,
            check_cmd=self.repo_cfg.check,
            resolver=self.resolver,
            resolve_attempts=self.resolve_attempts,
            on_conflict=self._conflict_reporter(work.slice_id),
        )
        if not prep.ok:
            return self._block(work, prep.detail)
        sl = self.source.get(work.slice_id)
        try:
            url = open_pr_for_slice(
                self.pr,
                sl,
                branch=work.branch,
                base=self.repo_cfg.base_branch,
                automerge=False,
                labels=(BABYSIT_LABEL,),
                decisions=work.decisions,
            )
        except Exception as exc:  # gh/network failure must not kill the tick
            return self._block(work, f"PR failed: {exc}")
        # The branch lives on origin now. Tear down before merging so `gh pr merge
        # --delete-branch` can delete the local branch too.
        assert sl is not None  # open_pr_for_slice read it above
        self.worktrees.teardown(work.slice_id)
        if REVIEW_LABEL in sl.labels:
            return self._await_review(work, url)
        try:
            self.pr.merge(work.branch)
        except Exception as exc:  # not mergeable, or gh failed: a human decides
            return self._await_review(work, url, reason=f"auto-merge failed: {exc}")
        self.source.set_status(work.slice_id, DONE)
        self.pr.unlabel(url, BABYSIT_LABEL)
        self._emit("merged", work.slice_id, pr=url)
        self._drop(work.slice_id)
        return SliceResult(
            work.slice_id, DONE, work.attempts, work.commit, work.branch, "merged", work.session_id
        )

    def _await_review(self, work: SliceResult, url: str, reason: str = "") -> SliceResult:
        """Leave the PR open for babysit; ``_watch_reviews`` settles it once it merges."""
        self.source.set_status(work.slice_id, IN_REVIEW)
        self.notifier.notify("in-review", f"{work.slice_id}: {url}")
        extra = {"reason": reason} if reason else {}
        self._emit("review_ready", work.slice_id, pr=url, **extra)
        self._phase(work.slice_id, IN_REVIEW, pr=url)
        return SliceResult(
            work.slice_id, IN_REVIEW, work.attempts, work.commit, work.branch, url, work.session_id
        )

    def _watch_reviews(self, slices: list[Slice]) -> None:
        """Pull merged work, then settle in-review slices whose PR merged or closed."""
        cfg = self.repo_cfg
        log.info("base: %s", sync_base(cfg.path, cfg.base_branch, cfg.sync))
        phases = self.state.snapshot()["slices"] if self.state is not None else {}
        for sl in slices:
            if sl.status == DONE and sl.id in phases:
                self._settle_done(sl, phases[sl.id])
                continue
            if sl.status != IN_REVIEW:
                continue
            try:
                state, mergeable = self.pr.view(self.worktrees.branch_for(sl.id))
            except Exception as exc:
                log.warning("%s: PR state unknown (%s)", sl.id, exc)
                continue
            if state == "MERGED":
                self.source.set_status(sl.id, DONE)
                self.pr.unlabel(self.worktrees.branch_for(sl.id), BABYSIT_LABEL)
                sl.status = DONE
                self._emit("merged", sl.id)
                self._drop(sl.id)
            elif state == "CLOSED":
                reason = "PR closed without merging (babysit)"
                self.source.set_blocked(sl.id, reason)
                sl.status = BLOCKED
                self._emit("blocked", sl.id, reason=reason)
                self._phase(sl.id, BLOCKED, detail=reason)
            elif mergeable == "CONFLICTING":
                self._rebase_pr(sl)

    def _settle_done(self, sl: Slice, entry: dict) -> None:
        """A done slice still listed in state.json: its issue was closed by a PR's
        "Closes #N" on merge, or by hand. Report the merge, clear labels, drop it."""
        if entry.get("pr"):
            branch = self.worktrees.branch_for(sl.id)
            try:
                state, _ = self.pr.view(branch)
            except Exception as exc:  # try again next poll
                log.warning("%s: PR state unknown (%s)", sl.id, exc)
                return
            if state == "MERGED":
                self.pr.unlabel(branch, BABYSIT_LABEL)
                self._emit("merged", sl.id, pr=entry["pr"])
        self.source.set_status(sl.id, DONE)  # strips leftover status labels
        self._drop(sl.id)

    def _rebase_pr(self, sl: Slice) -> None:
        """Main moved under an open PR: rebase the PR branch and force-push it.

        Tried once per base commit, whether it worked or not, so a PR that can't be
        rebased is reported once and left for babysit until main moves again.
        """
        cfg = self.repo_cfg
        branch = self.worktrees.branch_for(sl.id)
        try:
            base = _git(cfg.path, "rev-parse", cfg.base_branch)
            if self._rebase_tried.get(sl.id) == base:
                return
            self._rebase_tried[sl.id] = base
            detail = self._rebase_and_push(sl.id, branch)
        except Exception as exc:  # git/fs trouble must not stop the poll
            detail = str(exc)
        if detail is None:
            log.info("%s: PR rebased onto %s and pushed", sl.id, cfg.base_branch)
            self._emit("pr_rebased", sl.id, branch=branch)
            return
        reason = f"PR conflicts with {cfg.base_branch} and could not be rebased: {detail}"
        log.warning("%s: %s", sl.id, reason)
        self._emit("blocked", sl.id, reason=reason)

    def _rebase_and_push(self, slice_id: str, branch: str) -> str | None:
        """Run the rebase ladder on a fresh checkout of the PR branch; None on success."""
        cfg = self.repo_cfg
        try:
            wt = self.worktrees.checkout_remote(slice_id)
            if cfg.install:
                installed = run_check(cfg.install, wt.path)
                if not installed.passed:
                    return f"install failed: {installed.output[-300:].strip()}"
            prep = prepare_branch(
                worktree_path=wt.path,
                base_branch=cfg.base_branch,
                check_cmd=cfg.check,
                resolver=self.resolver,
                resolve_attempts=self.resolve_attempts,
                on_conflict=self._conflict_reporter(slice_id),
            )
            if not prep.ok:
                return prep.detail
            _git(wt.path, "push", "--force-with-lease", "origin", f"HEAD:{branch}")
            return None
        finally:
            if self.worktrees.exists(slice_id):
                self.worktrees.teardown(slice_id)

    def _note_idle(self, slices: list[Slice]) -> None:
        open_ = {s.id: s.status for s in slices if s.status != DONE}
        if not open_:
            kind: str | None = "all_done"
        elif any(st in (IN_PROGRESS, IN_REVIEW) for st in open_.values()):
            kind = None
        else:
            kind = "stalled"
        if kind is not None and kind != self._idle:
            if kind == "stalled":
                log.warning("stalled: nothing runnable; open slices: %s", open_)
                self._emit(kind, waiting=open_)
            else:
                self._emit(kind)
        self._idle = kind

    def tick(self) -> list[SliceResult]:
        """One synchronous pass (``--once``, tests): start work, wait for all of it, ship it."""
        self._start_ready()
        return self._settle(wait=True)

    def poll(self) -> list[SliceResult]:
        """Continuous mode: ship whatever finished, then fill the free slots."""
        results = self._settle(wait=False)
        self._start_ready()
        return results

    def _executor_pool(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self.max_parallel)
        return self._pool

    def _set_mode(self, mode: str) -> None:
        if mode != self._mode:
            self._mode = mode
            log.info("daemon %s", mode)
            self._daemon_state(status=mode)

    def _apply_control(self) -> bool:
        """Read the control file; True when no new work may start."""
        if self.control is not None and self.control.stop:
            self._set_mode("stopping")
            return True
        if self.control is not None and self.control.paused:
            self._set_mode("paused")
            return True
        self._set_mode("running")
        return False

    def _start_ready(self) -> None:
        """Fill free slots with the most important runnable, non-overlapping slices."""
        slices = self.source.list_all()
        if not slices:
            log.info("idle — no slices")
            self._apply_control()  # a stop request must work on an empty queue too
            return
        self._reload_config()
        if self.repo_cfg.babysit:
            self._watch_reviews(slices)
        self._recover(slices)
        if self.paused_until is not None:
            if self.clock() < self.paused_until:
                log.info(
                    "paused (usage limit) until %s",
                    time.strftime("%H:%M", time.localtime(self.paused_until)),
                )
                return
            self.paused_until = None
            self._emit("resumed")
            self._daemon_state(status="running", paused_until=None)
            log.info("usage limit reset; resuming")
        if self._apply_control():
            return
        if self.repo_cfg.main_gate and not self._main_green():
            return
        if self.runtime is not None:
            self.runtime.reconcile(known_slice_ids={s.id for s in slices}, worktrees=self.worktrees)
        dag = Dag.build(slices)
        self._warn_unknown_deps(dag.unknown_deps())
        weight = dag.dependant_counts()
        order = {s.id: i for i, s in enumerate(slices)}
        runnable = sorted(
            (s for s in dag.runnable() if s.id not in self._running),
            key=lambda s: (-weight[s.id], order[s.id]),
        )
        if self.lint_issues:
            runnable = self._lint_runnable(runnable)
        free = self.max_parallel - len(self._running)
        waiting: dict[str, str] = {}
        started: list[Slice] = []
        for sl in runnable:
            touches = sl.touches()
            holder = next((h for h, t in self._held.items() if overlaps(touches, t)), None)
            if holder is not None:
                waiting[sl.id] = f"touches overlap with {holder}"
            elif len(started) >= free:
                waiting[sl.id] = "no free slot"
            else:
                started.append(sl)
                self._held[sl.id] = touches
        for s in slices:
            if s.status == "ready":
                reason = dag.waiting_reason(s.id)
                if reason:
                    waiting[s.id] = reason
        self._log_waiting(waiting)
        if not started:
            if not self._running:
                log.info("idle — %d slice(s), none runnable", len(slices))
                self._note_idle(slices)
            return
        self._idle = None
        log.info("running %d slice(s): %s", len(started), ", ".join(s.id for s in started))
        for sl in started:
            try:  # all-or-nothing: a failed start must not leave a stale lock
                self.source.set_status(sl.id, IN_PROGRESS)
                self._emit("slice_started", sl.id, weight=weight[sl.id])
                self._phase(sl.id, "working")
                log.info("starting %s (blocks %d)", sl.id, weight[sl.id])
                self._running[sl.id] = self._executor_pool().submit(self._run_one, sl)
            except Exception as e:
                self._held.pop(sl.id, None)
                self._drop(sl.id)
                log.warning("could not start %s (stays ready): %s", sl.id, e)
                self._emit("start_failed", sl.id, error=str(e))

    def _lint_runnable(self, runnable: list[Slice]) -> list[Slice]:
        """Park issues the linter rejects as needs-detail; return the rest."""
        clean = []
        for sl in runnable:
            problems = lint(sl)
            if problems:
                text = "\n".join(f"- {p}" for p in problems)
                self.source.set_needs_detail(sl.id, text)
                self._emit("needs_detail", sl.id, problems=problems)
                self._phase(sl.id, "needs-detail", detail="; ".join(problems))
                log.warning("%s needs detail: %s", sl.id, "; ".join(problems))
            else:
                clean.append(sl)
        return clean

    def _settle(self, *, wait: bool) -> list[SliceResult]:
        """Ship finished work serially, in start order (the merge train)."""
        if wait and self._running:
            futures_wait(list(self._running.values()))
        done = [sid for sid, fut in self._running.items() if fut.done()]
        results = []
        for sid in done:
            work = self._running.pop(sid).result()
            self._held.pop(sid, None)
            results.append(self._integrate(work))
        self._log_results(results)
        self._record_runtime(results)
        return results

    def _log_results(self, results: list[SliceResult]) -> None:
        for r in results:
            if r.status == DONE:
                log.info("%s done (%s)", r.slice_id, (r.commit or "")[:8])
            elif r.status == IN_REVIEW:
                log.info("%s in review: %s", r.slice_id, r.detail)
            elif r.status == LIMITED:
                log.warning("%s paused on the usage limit", r.slice_id)
            elif r.status == QUESTION:
                log.warning("%s has a question: %s", r.slice_id, r.detail)
            elif r.status == REQUEUED:
                log.info("%s requeued: %s", r.slice_id, r.detail)
            elif r.status == NEEDS_FILES:
                log.info("%s needs more files; requeued (%s)", r.slice_id, r.detail)
            else:
                log.warning("%s BLOCKED — %s", r.slice_id, r.detail)

    def _record_runtime(self, results: list[SliceResult]) -> None:
        if self.runtime is None:
            return
        for r in results:
            if r.status in (QUESTION, NEEDS_FILES, REQUEUED):
                continue  # recorded in _integrate, with the prompt to resume with
            if r.status in (DONE, IN_REVIEW):
                self.runtime.forget(r.slice_id)
            else:
                self.runtime.record(
                    r.slice_id,
                    session_id=r.session_id,
                    branch=r.branch,
                    interrupted=r.status == LIMITED,
                )
                self.runtime.clear(r.slice_id, "resume_prompt")

    def _warn_unknown_deps(self, unknown: set[tuple[str, str]]) -> None:
        """Warn once per (slice, dependency) the source does not return, until it changes."""
        for sid, dep in sorted(unknown - self._unknown_deps):
            log.warning(
                "%s depends on %s, which is not ready (on hold or not a Nightshift issue); "
                "%s waits",
                sid,
                dep,
                sid,
            )
        self._unknown_deps = unknown

    def _log_waiting(self, waiting: dict[str, str]) -> None:
        if waiting == self._waiting:
            return
        for sid, reason in waiting.items():
            log.info("%s: %s", sid, reason)
            self._phase(sid, "waiting", detail=reason)
        self._waiting = waiting

    def _recover(self, slices: list[Slice]) -> None:
        """Requeue slices a crashed or restarted daemon left in progress (once per life)."""
        if self._recovered:
            return
        self._recovered = True
        for sl in slices:
            if sl.status == IN_PROGRESS and sl.id not in self._running:
                self.source.set_status(sl.id, "ready")
                sl.status = "ready"
                entry = self.runtime.get(sl.id) if self.runtime is not None else {}
                has_tree = self.worktrees.exists(sl.id)
                if entry.get("session_id") and has_tree:
                    self.runtime.record(sl.id, interrupted=True)
                elif has_tree:
                    self.worktrees.teardown(sl.id)  # nothing to resume: start fresh
                log.warning("%s was in progress when the daemon stopped; requeued", sl.id)
                self._emit("requeued", sl.id)

    def _main_green(self) -> bool:
        """Run the check on the base branch once per new base commit (field note #15)."""
        cfg = self.repo_cfg
        try:
            head = _git(cfg.path, "rev-parse", cfg.base_branch)
        except RuntimeError as exc:
            log.warning("main gate: can't read %s (%s)", cfg.base_branch, exc)
            return True  # don't stall the queue on a git hiccup
        if head == self._green:
            return True
        if head == self._red:
            return False
        try:
            gate = self.worktrees.gate_worktree(head)
        except RuntimeError as exc:
            log.warning("main gate: can't prepare the gate worktree (%s)", exc)
            return True  # a gate infrastructure problem must not stall the queue
        result = run_check(cfg.install, gate) if cfg.install else None
        if result is None or result.passed:
            result = run_check(cfg.check, gate)
        if result.passed:
            if self._red is not None:
                self._emit("main_green", commit=head)
                log.info("%s is green again", cfg.base_branch)
            self._green, self._red = head, None
            self._daemon_state(main="green")
            return True
        self._red = head
        tail = result.output[-500:].strip()
        log.warning(
            "%s is red at %s; not starting slices until it's fixed", cfg.base_branch, head[:8]
        )
        self._emit("main_red", commit=head, output=tail)
        self._daemon_state(main="red")
        return False

    def _reload_config(self) -> None:
        if self.reload is None:
            return
        try:
            fresh = self.reload()
        except Exception as exc:
            log.warning("config reload failed, keeping the old one: %s", exc)
            return
        if fresh != self.repo_cfg:
            log.info("config reloaded")
            self.repo_cfg = fresh
            self.executor.model = fresh.model
            self.executor.effort = fresh.effort

    def _idle_wait(self, interval: float, sleep: Callable[[float], None]) -> None:
        """Sleep until the next poll, waking early when a running slice finishes."""
        if self._running:
            futures_wait(
                list(self._running.values()), timeout=interval, return_when=FIRST_COMPLETED
            )
        else:
            sleep(interval)

    def run_forever(
        self,
        *,
        interval: float = 30,
        max_ticks: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._daemon_state(status="running", pid=os.getpid())
        reason = "stopped"
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                try:
                    self.poll()
                except Exception:
                    log.exception("poll failed; continuing")
                ticks += 1
                if self._mode == "stopping" and not self._running:
                    reason = "stop requested"
                    assert self.control is not None  # only a control file can request a stop
                    self.control.set(stop=False)
                    break
                if max_ticks is not None and ticks >= max_ticks:
                    break
                self._idle_wait(interval, sleep)
        except BaseException as exc:
            reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            log.info("daemon exiting (%s)", reason)
            self._daemon_state(status="stopped")
            self._emit(self.exit_event, reason=reason)
            if self._pool is not None:
                self._pool.shutdown(wait=True)


def run_daemon_cli(
    repo,
    *,
    config_path=None,
    once: bool = False,
    interval: float = 30,
    executor: Executor | None = None,
    home_root: Path | None = None,
    child: bool = False,
) -> list[SliceResult]:
    """`nsh daemon` glue: build a Daemon from config and tick once or run forever."""
    cfg = Config.load(config_path)
    repo_cfg = cfg.repo(str(repo))
    ex = executor if executor is not None else Executor.for_repo(repo_cfg)
    log.info("claude: model %s, effort %s", ex.model or "CLI default", ex.effort or "CLI default")
    roles = ("S", "M", "L", "review", "resolve")
    log.info(
        "models: %s", ", ".join(f"{r}={repo_cfg.model_for(r) or 'CLI default'}" for r in roles)
    )
    home = repo_home(repo_cfg.name, home_root)
    daemon = Daemon(
        source=build_source(repo_cfg),
        repo_cfg=repo_cfg,
        executor=ex,
        resolver=Resolver(ex.with_model(repo_cfg.model_for("resolve")), escalate=ex),
        reviewer=AgentReviewer(
            ex.with_model(repo_cfg.model_for("review")), base_branch=repo_cfg.base_branch
        ),
        notifier=build_notifier(cfg.defaults.get("notifier")),
        pr=GitHubPR(repo_cfg.path) if repo_cfg.babysit else None,
        runtime=Runtime(home / "runtime.json"),
        events=EventLog(home / "events.jsonl"),
        state=StateStore(home / "state.json"),
        conflicts=ConflictLog(home / "conflicts.json"),
        control=Control(home / "control.json"),
        reload=lambda: Config.load(config_path).repo(str(repo)),
        exit_event="child_exit" if child else "daemon_exit",
        lint_issues=True,
        result_contract=True,
    )
    if once:
        return daemon.tick()
    daemon.run_forever(interval=interval)  # pragma: no cover - blocks forever
    return []  # pragma: no cover
