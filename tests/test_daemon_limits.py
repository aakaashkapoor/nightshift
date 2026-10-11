"""Usage-limit pause and resume: pipeline and daemon."""

import json
import subprocess
from pathlib import Path

import pytest

from nightshift.config import RepoConfig
from nightshift.daemon import LIMITED, Daemon
from nightshift.events import EventLog
from nightshift.executor import Executor, RunOutput
from nightshift.limits import FALLBACK_WAIT, RESET_GRACE, UsageLimitReached
from nightshift.pipeline import CONTINUE_PROMPT, run_slice
from nightshift.runtime import Runtime
from nightshift.slice import Slice
from nightshift.source import LocalMdSource
from nightshift.state import StateStore
from nightshift.worktree import WorktreeManager

LIMIT = RunOutput(
    0,
    json.dumps(
        {
            "is_error": True,
            "result": "You've hit your session limit · resets 2:30pm",
            "session_id": "lim",
        }
    ),
    "",
)


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


def _slice(sid="slice-001") -> Slice:
    return Slice.parse(f"---\nid: {sid}\ntitle: {sid}\nstatus: ready\n---\n## Goal\ndo it\n")


class Script:
    """Fake agent: returns queued outputs; writes a file on non-limit calls."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.prompts: list[str] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        self.prompts.append(stdin)
        self.argvs.append(argv)
        out = self.outputs.pop(0)
        if out is not LIMIT:
            Path(cwd, "work.txt").write_text("x", encoding="utf-8")
        return out


def _ok(session="s1") -> RunOutput:
    return RunOutput(0, json.dumps({"result": "ok", "session_id": session}), "")


def test_run_slice_raises_on_usage_limit(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    with pytest.raises(UsageLimitReached) as info:
        run_slice(
            _slice(),
            repo_path=repo,
            check_cmd="echo ok",
            worktrees=wts,
            executor=Executor(runner=Script(LIMIT)),
        )
    assert info.value.session_id == "lim"
    assert wts.exists("slice-001")  # worktree kept for the resume


def test_run_slice_resumes_with_continue_prompt(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    with pytest.raises(UsageLimitReached):
        run_slice(
            _slice(),
            repo_path=repo,
            check_cmd="echo ok",
            worktrees=wts,
            executor=Executor(runner=Script(LIMIT)),
        )
    agent = Script(_ok("s2"))
    result = run_slice(
        _slice(),
        repo_path=repo,
        check_cmd="echo ok",
        worktrees=wts,
        executor=Executor(runner=agent),
        resume_session="lim",
    )
    assert result.status == "done"
    assert agent.prompts == [CONTINUE_PROMPT]
    assert "--resume" in agent.argvs[0] and "lim" in agent.argvs[0]
    assert result.session_id == "s2"


def test_resume_without_worktree_starts_fresh(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    agent = Script(_ok())
    result = run_slice(
        _slice(),
        repo_path=repo,
        check_cmd="echo ok",
        worktrees=wts,
        executor=Executor(runner=agent),
        resume_session="gone",
    )
    assert result.status == "done"
    assert agent.prompts[0] != CONTINUE_PROMPT
    assert "--resume" not in agent.argvs[0]


def test_resume_after_commit_does_not_fail_on_empty_commit(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    first = run_slice(
        _slice(),
        repo_path=repo,
        check_cmd="echo ok",
        worktrees=wts,
        executor=Executor(runner=Script(_ok())),
    )
    assert first.status == "done"

    class NoEdit:
        def __call__(self, argv, cwd, stdin) -> RunOutput:
            return _ok("s3")

    again = run_slice(
        _slice(),
        repo_path=repo,
        check_cmd="echo ok",
        worktrees=wts,
        executor=Executor(runner=NoEdit()),
        resume_session="s1",
    )
    assert again.status == "done"
    assert again.commit == first.commit


def test_limit_during_review_fix_raises(repo, tmp_path) -> None:
    from nightshift.review import Finding, ReviewResult

    class OneBlocking:
        def review(self, path, sl):
            return ReviewResult([Finding("blocking", "bug")])

    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    with pytest.raises(UsageLimitReached):
        run_slice(
            _slice(),
            repo_path=repo,
            check_cmd="echo ok",
            worktrees=wts,
            executor=Executor(runner=Script(_ok(), LIMIT)),
            reviewer=OneBlocking(),
        )


def _write_slice(repo, sid, status="ready", deps=None) -> None:
    deps_line = f"depends_on: {deps}\n" if deps is not None else ""
    (repo / ".slices" / f"{sid}.md").write_text(
        f"---\nid: {sid}\ntitle: {sid}\nstatus: {status}\n{deps_line}---\n## Goal\ndo it\n",
        encoding="utf-8",
    )


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Flip:
    """Hits the limit while `limited` is True, works normally otherwise."""

    def __init__(self):
        self.limited = True
        self.prompts: list[str] = []

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        self.prompts.append(stdin)
        if self.limited:
            return LIMIT
        Path(cwd, f"{Path(cwd).name}.txt").write_text("x", encoding="utf-8")
        return _ok("s-after")


def _daemon(repo, tmp_path, agent, clock, resets=None) -> Daemon:
    return Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="demo", path=repo, check="echo ok", base_branch="main"),
        executor=Executor(runner=agent),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        runtime=Runtime(tmp_path / "home" / "runtime.json"),
        events=EventLog(tmp_path / "home" / "events.jsonl", clock=clock),
        state=StateStore(tmp_path / "home" / "state.json", clock=clock),
        clock=clock,
        reset_lookup=lambda sid: resets,
    )


def _kinds(tmp_path) -> list[str]:
    events, _ = EventLog(tmp_path / "home" / "events.jsonl").read_from(0)
    return [e["kind"] for e in events]


def test_limit_pauses_instead_of_blocking(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    clock = Clock()
    d = _daemon(repo, tmp_path, Flip(), clock, resets=5000.0)
    results = d.tick()
    assert [r.status for r in results] == [LIMITED]
    assert LocalMdSource(repo).get("slice-001").status == "ready"  # not blocked
    assert d.paused_until == 5000.0 + RESET_GRACE
    assert _kinds(tmp_path) == ["slice_started", "paused_limit"]
    snap = StateStore(tmp_path / "home" / "state.json").snapshot()
    assert snap["daemon"]["status"] == "paused_limit"
    assert snap["slices"]["slice-001"]["phase"] == "queued"
    assert Runtime(tmp_path / "home" / "runtime.json").get("slice-001")["interrupted"] is True


def test_paused_tick_does_nothing_then_resumes_session(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    clock = Clock()
    agent = Flip()
    d = _daemon(repo, tmp_path, agent, clock, resets=5000.0)
    d.tick()
    agent.limited = False
    clock.t = 2000.0
    assert d.tick() == []  # still paused
    assert len(agent.prompts) == 1
    clock.t = 5000.0 + RESET_GRACE
    results = d.tick()
    assert [r.status for r in results] == ["done"]
    assert agent.prompts[-1] == CONTINUE_PROMPT
    assert d.paused_until is None
    assert _kinds(tmp_path)[-3:] == ["resumed", "slice_started", "merged"]
    assert StateStore(tmp_path / "home" / "state.json").snapshot()["daemon"]["status"] == "running"
    assert Runtime(tmp_path / "home" / "runtime.json").get("slice-001") == {}


def test_unknown_reset_uses_fallback_and_one_pause_event(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002")
    clock = Clock()
    d = _daemon(repo, tmp_path, Flip(), clock, resets=None)
    d.tick()
    assert d.paused_until == 1000.0 + FALLBACK_WAIT
    assert _kinds(tmp_path).count("paused_limit") == 1


def test_all_done_and_stalled_emitted_once(repo, tmp_path) -> None:
    clock = Clock()
    agent = Flip()
    agent.limited = False
    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002", status="blocked")
    _write_slice(repo, "slice-003", deps="[slice-002]")
    d = _daemon(repo, tmp_path, agent, clock)
    d.tick()  # runs slice-001
    d.tick()  # idle: 002 blocked, 003 waits on it
    d.tick()
    assert _kinds(tmp_path).count("stalled") == 1
    events, _ = EventLog(tmp_path / "home" / "events.jsonl").read_from(0)
    stalled = next(e for e in events if e["kind"] == "stalled")
    assert stalled["waiting"] == {"slice-002": "blocked", "slice-003": "ready"}
    LocalMdSource(repo).set_status("slice-002", "done")
    LocalMdSource(repo).set_status("slice-003", "done")
    d.tick()
    d.tick()
    assert _kinds(tmp_path).count("all_done") == 1


def test_blocked_slice_emits_event_and_state(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    clock = Clock()
    agent = Flip()
    agent.limited = False
    d = _daemon(repo, tmp_path, agent, clock)
    d.repo_cfg.check = "exit 1"
    d.max_attempts = 1
    d.tick()
    assert "blocked" in _kinds(tmp_path)
    snap = StateStore(tmp_path / "home" / "state.json").snapshot()
    assert snap["slices"]["slice-001"]["phase"] == "blocked"
    assert Runtime(tmp_path / "home" / "runtime.json").get("slice-001")["interrupted"] is False


def test_limit_during_conflict_resolution_pauses(repo, tmp_path) -> None:
    class LimitResolver:
        def __call__(self, worktree_path):
            raise UsageLimitReached("res-1")

    class SameFile:
        def __call__(self, argv, cwd, stdin) -> RunOutput:
            readme = Path(cwd, "README.md")
            readme.write_text(
                readme.read_text(encoding="utf-8") + Path(cwd).name + "\n", encoding="utf-8"
            )
            return _ok()

    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002")
    clock = Clock()
    d = _daemon(repo, tmp_path, SameFile(), clock, resets=9000.0)
    d.resolver = LimitResolver()
    results = d.tick()
    assert sorted(r.status for r in results) == ["done", LIMITED]
    limited = next(r for r in results if r.status == LIMITED)
    assert LocalMdSource(repo).get(limited.slice_id).status == "ready"
    assert d.paused_until == 9000.0 + RESET_GRACE
    wt = tmp_path / "wts" / limited.slice_id
    assert "rebase in progress" not in _git(wt, "status")  # the rebase was aborted


def test_run_forever_records_exit_and_survives_tick_errors(repo, tmp_path) -> None:
    clock = Clock()
    d = _daemon(repo, tmp_path, Flip(), clock)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gh down")
        return []

    d.tick = boom  # type: ignore[method-assign]
    d.run_forever(interval=0, max_ticks=2, sleep=lambda s: None)
    assert calls["n"] == 2
    snap = StateStore(tmp_path / "home" / "state.json").snapshot()
    assert snap["daemon"]["status"] == "stopped"
    events, _ = EventLog(tmp_path / "home" / "events.jsonl").read_from(0)
    assert events[-1] == {"ts": 1000.0, "kind": "daemon_exit", "reason": "stopped"}


def test_run_forever_records_interrupt_reason(repo, tmp_path) -> None:
    d = _daemon(repo, tmp_path, Flip(), Clock())

    def interrupt():
        raise KeyboardInterrupt()

    d.tick = interrupt  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        d.run_forever(interval=0, max_ticks=1, sleep=lambda s: None)
    events, _ = EventLog(tmp_path / "home" / "events.jsonl").read_from(0)
    assert events[-1]["reason"] == "KeyboardInterrupt: "


def test_reviewer_limit_resumes_build_session_but_reads_reset_from_reviewer(repo, tmp_path) -> None:
    from nightshift.review import AgentReviewer

    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    with pytest.raises(UsageLimitReached) as info:
        run_slice(
            _slice(),
            repo_path=repo,
            check_cmd="echo ok",
            worktrees=wts,
            executor=Executor(runner=Script(_ok("build-1"))),
            reviewer=AgentReviewer(Executor(runner=Script(LIMIT))),
        )
    assert info.value.session_id == "build-1"
    assert info.value.reset_session == "lim"


def test_resolver_limit_reads_reset_from_resolver_session(repo, tmp_path) -> None:
    class LimitResolver:
        def __call__(self, worktree_path):
            raise UsageLimitReached("build-ignored", reset_session="res-1")

    class SameFile:
        def __call__(self, argv, cwd, stdin) -> RunOutput:
            readme = Path(cwd, "README.md")
            readme.write_text(
                readme.read_text(encoding="utf-8") + Path(cwd).name + "\n", encoding="utf-8"
            )
            return _ok()

    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002")
    looked_up: list[str | None] = []
    d = _daemon(repo, tmp_path, SameFile(), Clock())
    d.reset_lookup = lambda sid: looked_up.append(sid)
    d.resolver = LimitResolver()
    d.tick()
    assert looked_up == ["res-1"]


def test_work_phase_reviewer_limit_looks_up_reviewer_but_records_build_session(
    repo, tmp_path
) -> None:
    from nightshift.review import AgentReviewer

    class Build:
        def __call__(self, argv, cwd, stdin) -> RunOutput:
            Path(cwd, "work.txt").write_text("x", encoding="utf-8")
            return _ok("build-1")

    _write_slice(repo, "slice-001")
    looked_up: list[str | None] = []
    d = _daemon(repo, tmp_path, Build(), Clock())
    d.reset_lookup = lambda sid: looked_up.append(sid)
    d.reviewer = AgentReviewer(Executor(runner=Script(LIMIT)))
    results = d.tick()
    assert [r.status for r in results] == [LIMITED]
    assert looked_up == ["lim"]
    assert Runtime(tmp_path / "home" / "runtime.json").get("slice-001")["session_id"] == "build-1"
