"""The worker pool: critical path, polling, recovery, main gate, controls, reload."""

import json
import logging
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from nightshift.config import RepoConfig
from nightshift.control import Control
from nightshift.daemon import Daemon
from nightshift.events import EventLog
from nightshift.executor import Executor, RunOutput
from nightshift.pipeline import CONTINUE_PROMPT
from nightshift.runtime import Runtime
from nightshift.source import LocalMdSource
from nightshift.state import StateStore
from nightshift.worktree import WorktreeManager


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(r)], check=True, capture_output=True)
    _git(r, "config", "user.email", "t@e.local")
    _git(r, "config", "user.name", "T")
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    (r / ".slices").mkdir()
    return r


def _write_slice(repo, sid, status="ready", deps=None, touches=None) -> None:
    deps_line = f"depends_on: {deps}\n" if deps is not None else ""
    touches_block = ""
    if touches:
        touches_block = "\n## Touches\n" + "".join(f"- {t}\n" for t in touches)
    (repo / ".slices" / f"{sid}.md").write_text(
        f"---\nid: {sid}\ntitle: {sid}\nstatus: {status}\n{deps_line}---\n"
        f"## Goal\ndo it\n{touches_block}",
        encoding="utf-8",
    )


def _ok(session="s") -> RunOutput:
    return RunOutput(0, json.dumps({"result": "ok", "session_id": session}), "")


class Agent:
    """Writes a file unique to each slice's worktree; records every prompt."""

    def __init__(self):
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        name = Path(cwd).name
        self.prompts.append((name, stdin))
        Path(cwd, f"{name}.txt").write_text("x", encoding="utf-8")
        return _ok()


class GateAgent(Agent):
    """slice-001 blocks until ``release`` is set; every other slice finishes at once."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        if Path(cwd).name == "slice-001":
            assert self.release.wait(timeout=30), "test never released the gate"
        return super().__call__(argv, cwd, stdin)


def _daemon(repo, tmp_path, agent, *, max_parallel=5, check="echo ok", **kw) -> Daemon:
    return Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="demo", path=repo, check=check, base_branch="main"),
        executor=Executor(runner=agent),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        max_parallel=max_parallel,
        runtime=Runtime(tmp_path / "home" / "runtime.json"),
        events=EventLog(tmp_path / "home" / "events.jsonl"),
        state=StateStore(tmp_path / "home" / "state.json"),
        **kw,
    )


def _events(tmp_path) -> list[dict]:
    events, _ = EventLog(tmp_path / "home" / "events.jsonl").read_from(0)
    return events


def _kinds(tmp_path) -> list[str]:
    return [e["kind"] for e in _events(tmp_path)]


def _snap(tmp_path) -> dict:
    return StateStore(tmp_path / "home" / "state.json").snapshot()


def test_critical_path_starts_first(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")  # leaf, nothing waits on it
    _write_slice(repo, "slice-002")  # root: two slices wait on it
    _write_slice(repo, "slice-003", deps="[slice-002]")
    _write_slice(repo, "slice-004", deps="[slice-003]")
    d = _daemon(repo, tmp_path, Agent(), max_parallel=1)
    results = d.tick()
    assert [r.slice_id for r in results] == ["slice-002"]
    started = [e for e in _events(tmp_path) if e["kind"] == "slice_started"]
    assert started[0]["weight"] == 2


def test_poll_starts_new_work_without_waiting_for_the_batch(repo, tmp_path) -> None:
    agent = GateAgent()
    d = _daemon(repo, tmp_path, agent)
    try:
        _write_slice(repo, "slice-001")
        assert d.poll() == []
        assert list(d._running) == ["slice-001"]
        _write_slice(repo, "slice-002")
        assert d.poll() == []  # slice-001 is still blocked on the gate
        assert list(d._running) == ["slice-001", "slice-002"]
    finally:
        agent.release.set()
    results = d.tick()  # drains both, shipping in start order
    assert [r.slice_id for r in results] == ["slice-001", "slice-002"]
    assert [r.status for r in results] == ["done", "done"]
    assert d._running == {} and d._held == {}


def test_overlapping_touches_wait_and_say_why(repo, tmp_path, caplog) -> None:
    _write_slice(repo, "slice-001", touches=["src/kit/"])
    _write_slice(repo, "slice-002", touches=["src/kit/Card.tsx"])
    d = _daemon(repo, tmp_path, Agent())
    with caplog.at_level(logging.INFO, logger="nightshift"):
        results = d.tick()
    assert [r.slice_id for r in results] == ["slice-001"]
    assert "slice-002: touches overlap with slice-001" in caplog.text
    entry = _snap(tmp_path)["slices"]["slice-002"]
    assert (entry["phase"], entry["detail"]) == ("waiting", "touches overlap with slice-001")


def test_no_free_slot_is_reported(repo, tmp_path, caplog) -> None:
    for sid in ("slice-001", "slice-002"):
        _write_slice(repo, sid)
    d = _daemon(repo, tmp_path, Agent(), max_parallel=1)
    with caplog.at_level(logging.INFO, logger="nightshift"):
        d.tick()
    assert "slice-002: no free slot" in caplog.text


def test_waiting_on_deps_is_logged_once(repo, tmp_path, caplog) -> None:
    _write_slice(repo, "slice-002", status="blocked")
    _write_slice(repo, "slice-003", deps="[slice-002]")
    d = _daemon(repo, tmp_path, Agent())
    with caplog.at_level(logging.INFO, logger="nightshift"):
        assert d.tick() == []
        assert d.tick() == []
    assert caplog.text.count("slice-003: waiting on slice-002 (blocked)") == 1
    assert _snap(tmp_path)["slices"]["slice-003"]["phase"] == "waiting"


def test_recover_requeues_orphaned_in_progress(repo, tmp_path) -> None:
    agent = Agent()
    d = _daemon(repo, tmp_path, agent)
    _write_slice(repo, "slice-001", status="in-progress")  # has a session + worktree
    _write_slice(repo, "slice-002", status="in-progress")  # worktree, no session
    _write_slice(repo, "slice-003", status="in-progress")  # no worktree at all
    d.worktrees.create("slice-001")
    d.worktrees.create("slice-002")
    assert d.runtime is not None
    d.runtime.record("slice-001", session_id="old", branch="nightshift/slice-001")
    results = d.tick()
    assert {r.slice_id: r.status for r in results} == {
        "slice-001": "done",
        "slice-002": "done",
        "slice-003": "done",
    }
    assert _kinds(tmp_path).count("requeued") == 3
    prompts = dict(agent.prompts)
    assert prompts["slice-001"] == CONTINUE_PROMPT
    assert prompts["slice-002"] != CONTINUE_PROMPT
    assert prompts["slice-003"] != CONTINUE_PROMPT


def test_recover_runs_once_and_works_without_runtime(repo, tmp_path) -> None:
    agent = Agent()
    d = _daemon(repo, tmp_path, agent)
    d.runtime = None
    _write_slice(repo, "slice-001", status="in-progress")
    d.worktrees.create("slice-001")
    assert [r.status for r in d.tick()] == ["done"]
    assert agent.prompts[0][1] != CONTINUE_PROMPT  # no session recorded: fresh start
    _write_slice(repo, "slice-002", status="in-progress")  # a second pass must not requeue it
    assert d.tick() == []
    assert LocalMdSource(repo).get("slice-002").status == "in-progress"


RED_CHECK = f'"{sys.executable}" -c "import pathlib,sys; sys.exit(pathlib.Path(\'RED\').exists())"'


def test_main_gate_red_blocks_then_green_resumes(repo, tmp_path) -> None:
    (repo / "RED").write_text("x", encoding="utf-8")
    _git(repo, "add", "RED")
    _git(repo, "commit", "-m", "break main")
    _write_slice(repo, "slice-001")
    d = _daemon(repo, tmp_path, Agent(), check=RED_CHECK)
    d.repo_cfg.main_gate = True
    assert d.tick() == []
    assert d.tick() == []  # same red commit: no second event
    assert _kinds(tmp_path).count("main_red") == 1
    assert _snap(tmp_path)["daemon"]["main"] == "red"
    assert LocalMdSource(repo).get("slice-001").status == "ready"

    _git(repo, "rm", "RED")
    _git(repo, "commit", "-m", "fix main")
    results = d.tick()
    assert [r.status for r in results] == ["done"]
    assert "main_green" in _kinds(tmp_path)
    assert _snap(tmp_path)["daemon"]["main"] == "green"
    assert d._green == _git(repo, "rev-parse", "main")  # set by the local merge


def test_main_gate_green_checks_once_per_commit(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001", status="blocked")  # nothing runs; just exercise the gate
    d = _daemon(repo, tmp_path, Agent())
    d.repo_cfg.main_gate = True
    d.tick()
    assert d._green == _git(repo, "rev-parse", "main")
    d.repo_cfg.check = "exit 1"  # would fail if it were run again
    d.tick()
    assert d._red is None and "main_red" not in _kinds(tmp_path)


def test_main_gate_survives_git_error(repo, tmp_path, caplog) -> None:
    _write_slice(repo, "slice-001")
    d = _daemon(repo, tmp_path, Agent())
    d.repo_cfg.main_gate = True
    d.repo_cfg.base_branch = "nope"
    with caplog.at_level(logging.WARNING, logger="nightshift"):
        results = d.tick()
    assert "main gate: can't read nope" in caplog.text
    assert [r.status for r in results] == ["blocked"]  # the gate didn't stall the queue


JUNK_CHECK = f"\"{sys.executable}\" -c \"open('junk.pyc','w').write('x')\""


class JunkAgent(Agent):
    """Commits the same file path the check leaves behind as an untracked artifact."""

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        Path(cwd, "junk.pyc").write_text("slice", encoding="utf-8")
        return super().__call__(argv, cwd, stdin)


def test_main_gate_never_dirties_the_users_checkout(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    d = _daemon(repo, tmp_path, JunkAgent(), check=JUNK_CHECK)
    d.repo_cfg.main_gate = True
    assert [r.status for r in d.tick()] == ["done"]
    status = _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    assert [ln for ln in status if ".slices/" not in ln] == []  # the slice board is the user's
    assert _git(repo, "ls-files", "junk.pyc") == "junk.pyc"  # the slice's copy merged in
    assert (tmp_path / "wts" / "_main-gate").is_dir()


def test_main_gate_runs_install_in_gate_worktree(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001", status="blocked")
    d = _daemon(repo, tmp_path, Agent())
    d.repo_cfg.main_gate = True
    d.repo_cfg.install = f"\"{sys.executable}\" -c \"open('installed.txt','w').write('x')\""
    d.repo_cfg.check = f'"{sys.executable}" -c "import pathlib,sys; sys.exit(not pathlib.Path(\'installed.txt\').exists())"'
    d.tick()
    assert d._green == _git(repo, "rev-parse", "main")
    assert not (repo / "installed.txt").exists()


def test_main_gate_install_failure_is_red(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    d = _daemon(repo, tmp_path, Agent())
    d.repo_cfg.main_gate = True
    d.repo_cfg.install = f'"{sys.executable}" -c "print(\'install boom\'); raise SystemExit(3)"'
    assert d.tick() == []
    assert d._red == _git(repo, "rev-parse", "main")
    red = [e for e in _events(tmp_path) if e["kind"] == "main_red"]
    assert len(red) == 1 and "install boom" in str(red[0])


def test_main_gate_survives_gate_worktree_error(repo, tmp_path, caplog) -> None:
    _write_slice(repo, "slice-001")
    d = _daemon(repo, tmp_path, Agent())
    d.repo_cfg.main_gate = True

    def boom(_commit):
        raise RuntimeError("worktree broke")

    d.worktrees.gate_worktree = boom
    with caplog.at_level(logging.WARNING, logger="nightshift"):
        results = d.tick()
    assert "main gate: can't prepare the gate worktree" in caplog.text
    assert [r.status for r in results] == ["done"]


def test_pause_and_stop_controls(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    control = Control(tmp_path / "home" / "control.json")
    d = _daemon(repo, tmp_path, Agent(), control=control)

    control.set(paused=True)
    assert d.tick() == []
    assert _snap(tmp_path)["daemon"]["status"] == "paused"
    assert LocalMdSource(repo).get("slice-001").status == "ready"

    control.set(paused=False)
    assert [r.status for r in d.tick()] == ["done"]
    assert _snap(tmp_path)["daemon"]["status"] == "running"


def _no_sleep(_s) -> None:
    raise AssertionError("must not sleep")


def test_stop_exits_after_one_poll_and_resets_the_flag(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    control = Control(tmp_path / "home" / "control.json")
    control.set(stop=True)
    d = _daemon(repo, tmp_path, Agent(), control=control)
    d.run_forever(max_ticks=5, interval=0, sleep=_no_sleep)
    assert _events(tmp_path)[-1]["reason"] == "stop requested"
    assert control.stop is False
    assert LocalMdSource(repo).get("slice-001").status == "ready"  # nothing was started


def test_stop_works_with_an_empty_queue(repo, tmp_path) -> None:
    control = Control(tmp_path / "home" / "control.json")
    control.set(stop=True)
    d = _daemon(repo, tmp_path, Agent(), control=control)
    d.run_forever(max_ticks=5, interval=0, sleep=_no_sleep)
    assert _events(tmp_path)[-1]["reason"] == "stop requested"


def test_stop_waits_for_running_work_to_finish(repo, tmp_path) -> None:
    agent = GateAgent()
    _write_slice(repo, "slice-001")
    control = Control(tmp_path / "home" / "control.json")
    d = _daemon(repo, tmp_path, agent, control=control)
    d.poll()  # slice-001 is running, held at the gate
    control.set(stop=True)
    timer = threading.Timer(0.2, agent.release.set)
    timer.start()
    try:
        d.run_forever(max_ticks=50, interval=30)
    finally:
        agent.release.set()
        timer.cancel()
    assert LocalMdSource(repo).get("slice-001").status == "done"  # shipped before exiting
    assert _events(tmp_path)[-1]["reason"] == "stop requested"


def test_reload_applies_new_config(repo, tmp_path, caplog) -> None:
    _write_slice(repo, "slice-001", status="blocked")
    box: dict = {}
    d = _daemon(repo, tmp_path, Agent(), reload=lambda: box["next"])
    box["next"] = replace(d.repo_cfg, model="sonnet", effort="high")
    with caplog.at_level(logging.INFO, logger="nightshift"):
        d.tick()
    assert (d.executor.model, d.executor.effort) == ("sonnet", "high")
    assert caplog.text.count("config reloaded") == 1
    d.tick()  # identical config: no second reload message
    assert caplog.text.count("config reloaded") == 1

    def boom():
        raise ValueError("bad yaml")

    d.reload = boom
    with caplog.at_level(logging.WARNING, logger="nightshift"):
        d.tick()
    assert "config reload failed, keeping the old one: bad yaml" in caplog.text
    assert d.executor.model == "sonnet"


def test_run_forever_wakes_when_a_slice_finishes(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    d = _daemon(repo, tmp_path, Agent())
    d.run_forever(max_ticks=2, interval=3600, sleep=_no_sleep)
    assert LocalMdSource(repo).get("slice-001").status == "done"
    assert d._pool is not None and d._running == {}


def test_run_forever_sleeps_when_idle(repo, tmp_path) -> None:
    d = _daemon(repo, tmp_path, Agent())
    slept: list[float] = []
    d.run_forever(max_ticks=2, interval=7, sleep=slept.append)
    assert slept == [7]
    assert d._pool is None


def test_child_exit_event_name(repo, tmp_path) -> None:
    d = _daemon(repo, tmp_path, Agent(), exit_event="child_exit")
    d.run_forever(max_ticks=1, interval=0, sleep=lambda _s: None)
    assert _events(tmp_path)[-1]["kind"] == "child_exit"
    assert "daemon_exit" not in _kinds(tmp_path)
