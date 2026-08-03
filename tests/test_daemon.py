"""Tests for the daemon: parallel Work + serial merge-train + CLI wiring."""

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


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _main_files(repo) -> set[str]:
    return set(_git(repo, "ls-tree", "--name-only", "main").split())


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


def _ok(session="s") -> RunOutput:
    return RunOutput(0, json.dumps({"result": "ok", "session_id": session}), "")


class Agent:
    """Fake agent: writes a file unique to each slice's worktree (no cross-conflicts)."""

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        Path(cwd, f"{Path(cwd).name}.txt").write_text("x", encoding="utf-8")
        return _ok()


class DepAgent:
    """slice-001 creates parent.txt; everyone else creates their own file."""

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        name = Path(cwd).name
        if name == "slice-001":
            Path(cwd, "parent.txt").write_text("p", encoding="utf-8")
        else:
            Path(cwd, f"{name}.txt").write_text("x", encoding="utf-8")
        return _ok()


class ConflictAgent:
    """Every slice appends a distinct line to the SAME file -> rebase conflict."""

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        readme = Path(cwd, "README.md")
        readme.write_text(
            readme.read_text(encoding="utf-8") + f"\nline-{Path(cwd).name}\n",
            encoding="utf-8",
        )
        return _ok()


class StripResolver:
    """Fake resolver: drops conflict-marker lines, keeping both sides' content."""

    def __call__(self, worktree_path) -> None:
        readme = Path(worktree_path, "README.md")
        kept = [
            line
            for line in readme.read_text(encoding="utf-8").splitlines()
            if not line.startswith(("<<<<<<<", "=======", ">>>>>>>"))
        ]
        readme.write_text("\n".join(kept) + "\n", encoding="utf-8")


class SpyNotifier:
    def __init__(self):
        self.events = []

    def notify(self, event, message) -> None:
        self.events.append((event, message))


class NoOpResolver:
    """A resolver that never actually resolves -> exercises the escalation cap."""

    def __call__(self, worktree_path) -> None:
        return None


def _daemon(
    repo,
    tmp_path,
    agent,
    *,
    check="echo ok",
    max_attempts=3,
    max_parallel=5,
    resolver=None,
    notifier=None,
) -> Daemon:
    return Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="demo", path=repo, check=check, base_branch="main"),
        executor=Executor(runner=agent),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        max_attempts=max_attempts,
        max_parallel=max_parallel,
        resolver=resolver,
        notifier=notifier,
    )


# --- scheduling ---------------------------------------------------------------

def test_tick_runs_ready_slice_and_marks_done(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    results = _daemon(repo, tmp_path, Agent()).tick()
    assert [r.status for r in results] == ["done"]
    assert LocalMdSource(repo).get("slice-001").status == "done"


def test_tick_runs_only_runnable(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002", deps="[slice-001]")
    results = _daemon(repo, tmp_path, Agent()).tick()
    assert [r.slice_id for r in results] == ["slice-001"]
    src = LocalMdSource(repo)
    assert src.get("slice-001").status == "done"
    assert src.get("slice-002").status == "ready"


def test_tick_marks_blocked_when_check_fails(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    results = _daemon(repo, tmp_path, Agent(), check="exit 1", max_attempts=1).tick()
    assert results[0].status == "blocked"
    assert LocalMdSource(repo).get("slice-001").status == "blocked"


def test_tick_runs_independent_slices_in_parallel(repo, tmp_path) -> None:
    for sid in ("slice-001", "slice-002", "slice-003"):
        _write_slice(repo, sid)
    results = _daemon(repo, tmp_path, Agent(), max_parallel=5).tick()
    assert {r.slice_id for r in results} == {"slice-001", "slice-002", "slice-003"}
    assert all(r.status == "done" for r in results)


def test_max_parallel_caps_the_batch(repo, tmp_path) -> None:
    for sid in ("slice-001", "slice-002", "slice-003"):
        _write_slice(repo, sid)
    results = _daemon(repo, tmp_path, Agent(), max_parallel=2).tick()
    assert len(results) == 2
    src = LocalMdSource(repo)
    done = [s.id for s in src.list_all() if s.status == "done"]
    waiting = [s.id for s in src.list_all() if s.status == "ready"]
    assert len(done) == 2 and len(waiting) == 1


def test_overlapping_scope_hints_serialize(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001", hints="shared.py")
    _write_slice(repo, "slice-002", hints="shared.py")
    results = _daemon(repo, tmp_path, Agent(), max_parallel=5).tick()
    assert len(results) == 1
    assert sorted(s.status for s in LocalMdSource(repo).list_all()) == ["done", "ready"]


def test_non_overlapping_scope_hints_run_together(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001", hints="a.py")
    _write_slice(repo, "slice-002", hints="b.py")
    results = _daemon(repo, tmp_path, Agent(), max_parallel=5).tick()
    assert len(results) == 2
    assert all(r.status == "done" for r in results)


# --- merge-train (Ship) -------------------------------------------------------

def test_tick_lands_commit_on_main(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    before = _git(repo, "rev-parse", "main")
    _daemon(repo, tmp_path, Agent()).tick()
    assert _git(repo, "rev-parse", "main") != before
    assert "slice-001.txt" in _main_files(repo)


def test_two_independent_slices_land_two_commits_on_main(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002")
    _daemon(repo, tmp_path, Agent(), max_parallel=5).tick()
    assert _git(repo, "rev-list", "--count", "main") == "3"  # init + 2 merges
    assert {"slice-001.txt", "slice-002.txt"} <= _main_files(repo)


def test_dependent_slice_sees_parent_after_merge(repo, tmp_path) -> None:
    # Check REQUIRES parent.txt -> slice-002 can only pass if it branched from a
    # main that already had slice-001's merge.
    check = (
        f'"{sys.executable}" -c '
        f'"import os,sys; sys.exit(0 if os.path.exists(\'parent.txt\') else 1)"'
    )
    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002", deps="[slice-001]")
    d = _daemon(repo, tmp_path, DepAgent(), check=check)

    d.tick()  # runs slice-001
    assert LocalMdSource(repo).get("slice-001").status == "done"
    assert "parent.txt" in _main_files(repo)

    d.tick()  # now slice-002 is runnable and sees parent.txt
    assert LocalMdSource(repo).get("slice-002").status == "done"
    assert {"parent.txt", "slice-002.txt"} <= _main_files(repo)


def test_rebase_conflict_blocks_the_collider(repo, tmp_path) -> None:
    # Two slices edit the SAME file, no scope hints -> run in parallel, then the
    # second one's rebase conflicts and it blocks (pre-resolver behaviour).
    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002")
    results = _daemon(repo, tmp_path, ConflictAgent(), max_parallel=5).tick()

    by_id = {r.slice_id: r.status for r in results}
    assert by_id["slice-001"] == "done"
    assert by_id["slice-002"] == "blocked"
    readme = _git(repo, "show", "main:README.md")
    assert "line-slice-001" in readme
    assert "line-slice-002" not in readme


def test_resolver_resolves_conflict_and_merges(repo, tmp_path) -> None:
    # Same conflict, but a resolver is supplied -> the collider is resolved & merged.
    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002")
    results = _daemon(
        repo, tmp_path, ConflictAgent(), max_parallel=5, resolver=StripResolver()
    ).tick()

    by_id = {r.slice_id: r.status for r in results}
    assert by_id["slice-001"] == "done"
    assert by_id["slice-002"] == "done"  # no longer blocked
    readme = _git(repo, "show", "main:README.md")
    assert "line-slice-001" in readme and "line-slice-002" in readme


# --- escalation: cap + note + notify -----------------------------------------

def test_blocked_slice_records_reason_and_notifies(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    spy = SpyNotifier()
    _daemon(repo, tmp_path, Agent(), check="exit 1", max_attempts=1, notifier=spy).tick()

    sl = LocalMdSource(repo).get("slice-001")
    assert sl.status == "blocked"
    assert "## Blocked" in sl.body  # reason written into the issue
    assert spy.events and spy.events[0][0] == "blocked"
    assert "slice-001" in spy.events[0][1]


def test_resolver_cap_blocks_when_never_resolved(repo, tmp_path) -> None:
    # Conflict + a resolver that never resolves -> cap trips -> blocked.
    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002")
    results = _daemon(
        repo, tmp_path, ConflictAgent(), max_parallel=5, resolver=NoOpResolver()
    ).tick()

    by_id = {r.slice_id: r.status for r in results}
    assert by_id["slice-001"] == "done"
    assert by_id["slice-002"] == "blocked"
    blocked = next(r for r in results if r.slice_id == "slice-002")
    assert blocked.detail == "unresolved conflict"


# --- loop + CLI ---------------------------------------------------------------

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
