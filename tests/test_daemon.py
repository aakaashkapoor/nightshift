"""Tests for the sequential daemon loop (tick + run_forever + CLI wiring)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nightshift.config import RepoConfig
from nightshift.daemon import Daemon, run_daemon_cli
from nightshift.executor import Executor, RunOutput
from nightshift.source import LocalMdSource
from nightshift.worktree import WorktreeManager

# Check passes only if the agent wrote impl.txt into the worktree.
CHECK = (
    f'"{sys.executable}" -c '
    f'"import os,sys; sys.exit(0 if os.path.exists(\'impl.txt\') else 1)"'
)


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(r)], check=True, capture_output=True, text=True
    )
    _git(r, "config", "user.email", "t@e.local")
    _git(r, "config", "user.name", "T")
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    (r / ".slices").mkdir()
    return r


def _write_slice(repo, sid, status="ready", deps=None, hints=None) -> None:
    deps_line = f"depends_on: {deps}\n" if deps is not None else ""
    hints_block = f"\n## Scope hints\n{hints}\n" if hints else ""
    (repo / ".slices" / f"{sid}.md").write_text(
        f"---\nid: {sid}\ntitle: {sid}\nstatus: {status}\n{deps_line}---\n"
        f"## Goal\ndo it\n{hints_block}",
        encoding="utf-8",
    )


class Agent:
    def __init__(self, write_file: bool = True):
        self.write_file = write_file

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        if self.write_file:
            Path(cwd, "impl.txt").write_text("x", encoding="utf-8")
        return RunOutput(0, json.dumps({"result": "ok", "session_id": "s"}), "")


def _daemon(repo, tmp_path, agent, *, check=CHECK, max_attempts=3, max_parallel=5) -> Daemon:
    return Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="demo", path=repo, check=check, base_branch="main"),
        executor=Executor(runner=agent),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        max_attempts=max_attempts,
        max_parallel=max_parallel,
    )


def test_tick_runs_ready_slice_and_marks_done(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    d = _daemon(repo, tmp_path, Agent())

    results = d.tick()

    assert [r.status for r in results] == ["done"]
    assert LocalMdSource(repo).get("slice-001").status == "done"


def test_tick_runs_only_runnable(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002", deps="[slice-001]")
    d = _daemon(repo, tmp_path, Agent())

    results = d.tick()

    assert [r.slice_id for r in results] == ["slice-001"]  # 002 waits on 001
    src = LocalMdSource(repo)
    assert src.get("slice-001").status == "done"
    assert src.get("slice-002").status == "ready"


def test_tick_marks_blocked_when_check_never_passes(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    d = _daemon(repo, tmp_path, Agent(write_file=False), max_attempts=1)

    results = d.tick()

    assert results[0].status == "blocked"
    assert LocalMdSource(repo).get("slice-001").status == "blocked"


def test_tick_runs_independent_slices_in_parallel(repo, tmp_path) -> None:
    for sid in ("slice-001", "slice-002", "slice-003"):
        _write_slice(repo, sid)
    d = _daemon(repo, tmp_path, Agent(), max_parallel=5)

    results = d.tick()

    assert {r.slice_id for r in results} == {"slice-001", "slice-002", "slice-003"}
    assert all(r.status == "done" for r in results)


def test_max_parallel_caps_the_batch(repo, tmp_path) -> None:
    for sid in ("slice-001", "slice-002", "slice-003"):
        _write_slice(repo, sid)
    d = _daemon(repo, tmp_path, Agent(), max_parallel=2)

    results = d.tick()

    assert len(results) == 2  # third waits for the next tick
    src = LocalMdSource(repo)
    done = [s.id for s in src.list_all() if s.status == "done"]
    waiting = [s.id for s in src.list_all() if s.status == "ready"]
    assert len(done) == 2 and len(waiting) == 1


def test_overlapping_scope_hints_serialize(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001", hints="shared.py")
    _write_slice(repo, "slice-002", hints="shared.py")
    d = _daemon(repo, tmp_path, Agent(), max_parallel=5)

    results = d.tick()

    assert len(results) == 1  # the collider waits
    src = LocalMdSource(repo)
    assert sorted(s.status for s in src.list_all()) == ["done", "ready"]


def test_non_overlapping_scope_hints_run_together(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001", hints="a.py")
    _write_slice(repo, "slice-002", hints="b.py")
    d = _daemon(repo, tmp_path, Agent(), max_parallel=5)

    results = d.tick()

    assert len(results) == 2
    assert all(r.status == "done" for r in results)


def test_run_forever_ticks_n_times_then_stops(repo, tmp_path) -> None:
    d = _daemon(repo, tmp_path, Agent())
    calls = []
    d.tick = lambda: calls.append(1) or []  # type: ignore[method-assign]

    d.run_forever(max_ticks=3, interval=0, sleep=lambda _s: None)

    assert len(calls) == 3


def test_run_daemon_cli_once(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'repos:\n  demo:\n    path: "{repo.as_posix()}"\n    check: "echo ok"\n',
        encoding="utf-8",
    )
    results = run_daemon_cli(
        repo, config_path=cfg, once=True, executor=Executor(runner=Agent())
    )
    assert results[0].status == "done"
