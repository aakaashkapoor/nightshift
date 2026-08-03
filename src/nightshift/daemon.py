"""The Nightshift daemon loop (SPEC §2, §8).

v0.2 skeleton: each *tick* scans the source, builds the dependency DAG, and runs
every currently-runnable slice, writing status back into the source. Sequential for
now (parallelism = 2.4, the serial merge-train = 2.5). ``run_forever`` polls on an
interval.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .config import Config, RepoConfig
from .executor import Executor
from .notifier import NullNotifier
from .pipeline import SliceResult, integrate_branch, run_slice
from .resolver import Resolver
from .scheduler import Dag
from .slice import Slice
from .source import LocalMdSource
from .worktree import WorktreeManager

IN_PROGRESS = "in-progress"
DONE = "done"
BLOCKED = "blocked"


class Daemon:
    def __init__(
        self,
        *,
        source: LocalMdSource,
        repo_cfg: RepoConfig,
        executor: Executor,
        worktrees: WorktreeManager | None = None,
        max_attempts: int = 3,
        max_parallel: int | None = None,
        resolver=None,
        resolve_attempts: int = 2,
        notifier=None,
    ):
        self.source = source
        self.repo_cfg = repo_cfg
        self.executor = executor
        self.worktrees = worktrees or WorktreeManager(repo_cfg.path)
        self.max_attempts = max_attempts
        self.max_parallel = max_parallel or repo_cfg.max_parallel
        self.resolver = resolver
        self.resolve_attempts = resolve_attempts
        self.notifier = notifier or NullNotifier()

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
        try:
            return run_slice(
                sl,
                repo_path=self.repo_cfg.path,
                check_cmd=self.repo_cfg.check,
                worktrees=self.worktrees,
                executor=self.executor,
                base_branch=self.repo_cfg.base_branch,
                max_attempts=self.max_attempts,
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
        return SliceResult(
            work.slice_id, BLOCKED, work.attempts, work.commit, work.branch, reason
        )

    def _integrate(self, work: SliceResult) -> SliceResult:
        """Serial merge-train step for one committed branch (SPEC §6)."""
        if work.status != DONE:  # Work itself failed -> nothing to integrate
            return self._block(work, work.detail)
        result = integrate_branch(
            repo_path=self.repo_cfg.path,
            worktree_path=self.worktrees.path_for(work.slice_id),
            branch=work.branch,
            base_branch=self.repo_cfg.base_branch,
            check_cmd=self.repo_cfg.check,
            resolver=self.resolver,
            resolve_attempts=self.resolve_attempts,
        )
        if result.merged:
            self.worktrees.teardown(work.slice_id)
            self.source.set_status(work.slice_id, DONE)
            return SliceResult(
                work.slice_id, DONE, work.attempts, result.commit, work.branch, "merged"
            )
        return self._block(work, result.detail)

    def tick(self) -> list[SliceResult]:
        """One pass: parallel Work on a non-overlapping batch, then a serial merge-train."""
        slices = self.source.list_all()
        if not slices:
            return []
        batch = self._select_batch(Dag.build(slices).runnable())
        if not batch:
            return []
        for sl in batch:
            self.source.set_status(sl.id, IN_PROGRESS)
        # Work: parallel, each in its own worktree.
        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            work_results = list(pool.map(self._run_one, batch))
        # Ship: serial merge-train — one branch integrates at a time.
        return [self._integrate(work) for work in work_results]

    def run_forever(
        self,
        *,
        interval: float = 30,
        max_ticks: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            self.tick()
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            sleep(interval)


def run_daemon_cli(
    repo,
    *,
    config_path=None,
    once: bool = False,
    interval: float = 30,
    executor: Executor | None = None,
) -> list[SliceResult]:
    """`nsh daemon` glue: build a Daemon from config and tick once or run forever."""
    cfg = Config.load(config_path)
    repo_cfg = cfg.repo(str(repo))
    ex = executor if executor is not None else Executor()
    daemon = Daemon(
        source=LocalMdSource(repo_cfg.path),
        repo_cfg=repo_cfg,
        executor=ex,
        resolver=Resolver(ex),
    )
    if once:
        return daemon.tick()
    daemon.run_forever(interval=interval)
    return []
