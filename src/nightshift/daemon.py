"""The Nightshift daemon loop (SPEC §2, §8).

v0.2 skeleton: each *tick* scans the source, builds the dependency DAG, and runs
every currently-runnable slice, writing status back into the source. Sequential for
now (parallelism = 2.4, the serial merge-train = 2.5). ``run_forever`` polls on an
interval.
"""

from __future__ import annotations

import time
from typing import Callable

from .config import Config, RepoConfig
from .executor import Executor
from .pipeline import SliceResult, run_slice
from .scheduler import Dag
from .source import LocalMdSource
from .worktree import WorktreeManager

IN_PROGRESS = "in-progress"


class Daemon:
    def __init__(
        self,
        *,
        source: LocalMdSource,
        repo_cfg: RepoConfig,
        executor: Executor,
        worktrees: WorktreeManager | None = None,
        max_attempts: int = 3,
    ):
        self.source = source
        self.repo_cfg = repo_cfg
        self.executor = executor
        self.worktrees = worktrees or WorktreeManager(repo_cfg.path)
        self.max_attempts = max_attempts

    def tick(self) -> list[SliceResult]:
        """One pass: run every runnable slice, persist status."""
        slices = self.source.list_all()
        if not slices:
            return []
        dag = Dag.build(slices)
        results: list[SliceResult] = []
        for sl in dag.runnable():
            self.source.set_status(sl.id, IN_PROGRESS)
            result = run_slice(
                sl,
                repo_path=self.repo_cfg.path,
                check_cmd=self.repo_cfg.check,
                worktrees=self.worktrees,
                executor=self.executor,
                base_branch=self.repo_cfg.base_branch,
                max_attempts=self.max_attempts,
            )
            self.source.set_status(sl.id, result.status)
            results.append(result)
        return results

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
    daemon = Daemon(
        source=LocalMdSource(repo_cfg.path),
        repo_cfg=repo_cfg,
        executor=executor if executor is not None else Executor(),
    )
    if once:
        return daemon.tick()
    daemon.run_forever(interval=interval)
    return []
