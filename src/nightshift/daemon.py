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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config, RepoConfig
from .events import EventLog
from .executor import Executor
from .limits import UsageLimitReached, reset_time, resume_at
from .notifier import NullNotifier, build_notifier
from .paths import repo_home
from .pipeline import (
    SliceResult,
    _git_ok,
    integrate_branch,
    prepare_branch,
    run_slice,
    sync_base,
)
from .pr import BABYSIT_LABEL, GitHubPR, open_pr_for_slice
from .resolver import Resolver
from .review import AgentReviewer
from .runtime import Runtime
from .scheduler import Dag
from .slice import Slice
from .source import Source, build_source
from .state import StateStore
from .worktree import WorktreeManager

IN_PROGRESS = "in-progress"
DONE = "done"
BLOCKED = "blocked"
IN_REVIEW = "in-review"
LIMITED = "limited"

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
        clock: Callable[[], float] = time.time,
        reset_lookup: Callable[[str | None], float | None] = reset_time,
    ):
        self.source = source
        self.repo_cfg = repo_cfg
        self.executor = executor
        self.worktrees = worktrees or WorktreeManager(
            repo_cfg.path, symlink_dirs=repo_cfg.symlink_dirs
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
        self.clock = clock
        self.reset_lookup = reset_lookup
        self.paused_until: float | None = None
        self._reset_sessions: dict[str, str | None] = {}
        self._idle: str | None = None

    def _emit(self, kind: str, slice_id: str | None = None, **data) -> None:
        if self.events is not None:
            self.events.emit(kind, slice_id, **data)

    def _phase(self, slice_id: str, phase: str, **fields) -> None:
        if self.state is not None:
            self.state.set_slice(slice_id, phase, **fields)

    def _drop(self, slice_id: str) -> None:
        if self.state is not None:
            self.state.drop_slice(slice_id)

    def _daemon_state(self, **fields) -> None:
        if self.state is not None:
            self.state.set_daemon(**fields)

    def _select_batch(self, runnable: list[Slice]) -> list[Slice]:
        """Up to max_parallel mutually non-overlapping slices.

        Two slices overlap if their scope hints intersect — those are serialized
        (the collider waits for a later tick). Slices with no hints never overlap.
        """
        selected: list[Slice] = []
        claimed: set[str] = set()
        for sl in runnable:
            if len(selected) >= self.max_parallel:
                break
            hints = sl.scope_hints()
            if hints & claimed:
                continue  # collides with an already-selected slice -> serialize
            selected.append(sl)
            claimed |= hints
        return selected

    def _run_one(self, sl: Slice) -> SliceResult:
        entry = self.runtime.get(sl.id) if self.runtime is not None else {}
        resume = entry.get("session_id") if entry.get("interrupted") else None
        try:
            return run_slice(
                sl,
                repo_path=self.repo_cfg.path,
                check_cmd=self.repo_cfg.check,
                worktrees=self.worktrees,
                executor=self.executor,
                base_branch=self.repo_cfg.base_branch,
                max_attempts=self.max_attempts,
                reviewer=self.reviewer,
                resume_session=resume,
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
        try:
            return self._ship(work)
        except UsageLimitReached as exc:
            _git_ok(self.worktrees.path_for(work.slice_id), "rebase", "--abort")
            return self._limited(work, reset_session=exc.reset_session)

    def _ship(self, work: SliceResult) -> SliceResult:
        if work.status != DONE:  # Work itself failed -> nothing to integrate
            return self._block(work, work.detail)
        if self.repo_cfg.babysit:
            return self._open_for_babysit(work)
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
        )
        if result.merged:
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

    def _open_for_babysit(self, work: SliceResult) -> SliceResult:
        """Babysit Ship (SPEC §9): rebase + re-check, open a labelled PR, stop."""
        prep = prepare_branch(
            worktree_path=self.worktrees.path_for(work.slice_id),
            base_branch=self.repo_cfg.base_branch,
            check_cmd=self.repo_cfg.check,
            resolver=self.resolver,
            resolve_attempts=self.resolve_attempts,
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
            )
        except Exception as exc:  # gh/network failure must not kill the tick
            return self._block(work, f"PR failed: {exc}")
        self.worktrees.teardown(work.slice_id)  # the branch lives on origin now
        self.source.set_status(work.slice_id, IN_REVIEW)
        self.notifier.notify("in-review", f"{work.slice_id}: {url}")
        self._emit("review_ready", work.slice_id, pr=url)
        self._phase(work.slice_id, IN_REVIEW, pr=url)
        return SliceResult(
            work.slice_id, IN_REVIEW, work.attempts, work.commit, work.branch, url, work.session_id
        )

    def _watch_reviews(self, slices: list[Slice]) -> None:
        """Pull merged work, then settle in-review slices whose PR merged or closed."""
        cfg = self.repo_cfg
        log.info("base: %s", sync_base(cfg.path, cfg.base_branch, cfg.sync))
        for sl in slices:
            if sl.status != IN_REVIEW:
                continue
            try:
                state = self.pr.state(self.worktrees.branch_for(sl.id))
            except Exception as exc:
                log.warning("%s: PR state unknown (%s)", sl.id, exc)
                continue
            if state == "MERGED":
                self.source.set_status(sl.id, DONE)
                sl.status = DONE
                self._emit("merged", sl.id)
                self._drop(sl.id)
            elif state == "CLOSED":
                reason = "PR closed without merging (babysit)"
                self.source.set_blocked(sl.id, reason)
                sl.status = BLOCKED
                self._emit("blocked", sl.id, reason=reason)
                self._phase(sl.id, BLOCKED, detail=reason)

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
        """One pass: parallel Work on a non-overlapping batch, then a serial merge-train."""
        slices = self.source.list_all()
        if not slices:
            log.info("idle — no slices")
            return []
        if self.repo_cfg.babysit:
            self._watch_reviews(slices)
        if self.paused_until is not None:
            if self.clock() < self.paused_until:
                log.info(
                    "paused (usage limit) until %s",
                    time.strftime("%H:%M", time.localtime(self.paused_until)),
                )
                return []
            self.paused_until = None
            self._emit("resumed")
            self._daemon_state(status="running", paused_until=None)
            log.info("usage limit reset; resuming")
        if self.runtime is not None:
            self.runtime.reconcile(known_slice_ids={s.id for s in slices}, worktrees=self.worktrees)
        batch = self._select_batch(Dag.build(slices).runnable())
        if not batch:
            log.info("idle — %d slice(s), none runnable", len(slices))
            self._note_idle(slices)
            return []
        self._idle = None
        log.info("running %d slice(s): %s", len(batch), ", ".join(s.id for s in batch))
        for sl in batch:
            self.source.set_status(sl.id, IN_PROGRESS)
            self._emit("slice_started", sl.id)
            self._phase(sl.id, "working")
        # Work: parallel, each in its own worktree.
        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            work_results = list(pool.map(self._run_one, batch))
        # Ship: serial merge-train — one branch integrates at a time.
        results = [self._integrate(work) for work in work_results]
        for r in results:
            if r.status == DONE:
                log.info("%s done (%s)", r.slice_id, (r.commit or "")[:8])
            elif r.status == IN_REVIEW:
                log.info("%s in review: %s", r.slice_id, r.detail)
            elif r.status == LIMITED:
                log.warning("%s paused on the usage limit", r.slice_id)
            else:
                log.warning("%s BLOCKED — %s", r.slice_id, r.detail)
        if self.runtime is not None:
            for r in results:
                if r.status in (DONE, IN_REVIEW):
                    self.runtime.forget(r.slice_id)
                else:
                    self.runtime.record(
                        r.slice_id,
                        session_id=r.session_id,
                        branch=r.branch,
                        interrupted=r.status == LIMITED,
                    )
        return results

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
                    self.tick()
                except Exception:
                    log.exception("tick failed; continuing")
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                sleep(interval)
        except BaseException as exc:
            reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            log.info("daemon exiting (%s)", reason)
            self._daemon_state(status="stopped")
            self._emit("daemon_exit", reason=reason)


def run_daemon_cli(
    repo,
    *,
    config_path=None,
    once: bool = False,
    interval: float = 30,
    executor: Executor | None = None,
    home_root: Path | None = None,
) -> list[SliceResult]:
    """`nsh daemon` glue: build a Daemon from config and tick once or run forever."""
    cfg = Config.load(config_path)
    repo_cfg = cfg.repo(str(repo))
    ex = executor if executor is not None else Executor.for_repo(repo_cfg)
    log.info("claude: model %s, effort %s", ex.model or "CLI default", ex.effort or "CLI default")
    home = repo_home(repo_cfg.name, home_root)
    daemon = Daemon(
        source=build_source(repo_cfg),
        repo_cfg=repo_cfg,
        executor=ex,
        resolver=Resolver(ex),
        reviewer=AgentReviewer(ex),
        notifier=build_notifier(cfg.defaults.get("notifier")),
        pr=GitHubPR(repo_cfg.path) if repo_cfg.babysit else None,
        runtime=Runtime(home / "runtime.json"),
        events=EventLog(home / "events.jsonl"),
        state=StateStore(home / "state.json"),
    )
    if once:
        return daemon.tick()
    daemon.run_forever(interval=interval)  # pragma: no cover - blocks forever
    return []  # pragma: no cover
