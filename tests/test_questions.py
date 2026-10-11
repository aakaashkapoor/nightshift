"""Agent questions and file requests flow through the issue; nsh answer resumes the slice."""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightshift.cli import app
from nightshift.config import RepoConfig
from nightshift.daemon import Daemon
from nightshift.events import EventLog
from nightshift.executor import Executor, RunOutput
from nightshift.paths import repo_home
from nightshift.prompts import ANSWER_PROMPT, FILES_PROMPT
from nightshift.runtime import Runtime
from nightshift.source import LocalMdSource
from nightshift.state import StateStore
from nightshift.wait import run_answer_cli
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


def _write_slice(repo, sid="slice-001", touches=None) -> None:
    block = "".join(f"- {t}\n" for t in touches or [])
    block = f"\n## Touches\n{block}" if touches else ""
    (repo / ".slices" / f"{sid}.md").write_text(
        f"---\nid: {sid}\ntitle: {sid}\nstatus: ready\n---\n## Goal\ndo it\n{block}",
        encoding="utf-8",
    )


def _result(cwd, **data) -> None:
    path = Path(cwd, ".nightshift", "result.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _ask(cwd) -> None:
    _result(cwd, outcome="question", question="Which db?")


def _need(cwd) -> None:
    _result(cwd, outcome="needs_files", needs_files=["b.py", "c.py"])


def _finish(cwd) -> None:
    Path(cwd, "impl.txt").write_text("x", encoding="utf-8")
    _result(cwd, outcome="done")


class Scripted:
    def __init__(self, *steps):
        self.steps = steps
        self.prompts: list[str] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        n = len(self.prompts)
        self.prompts.append(stdin)
        self.argvs.append(argv)
        self.steps[min(n, len(self.steps) - 1)](cwd)
        return RunOutput(0, json.dumps({"result": "r", "session_id": "sess"}), "")


def _config(tmp_path, repo) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"repos:\n  demo:\n    path: {repo.as_posix()}\n    check: echo ok\n", encoding="utf-8"
    )
    return cfg


def _daemon(repo, tmp_path, agent, *, runtime=True) -> Daemon:
    home = repo_home("demo", tmp_path / "ns")
    return Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="demo", path=repo, check="echo ok", base_branch="main"),
        executor=Executor(runner=agent),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        runtime=Runtime(home / "runtime.json") if runtime else None,
        events=EventLog(home / "events.jsonl"),
        state=StateStore(home / "state.json"),
        result_contract=True,
    )


def _events(tmp_path) -> list[dict]:
    home = repo_home("demo", tmp_path / "ns")
    return EventLog(home / "events.jsonl").read_from(0)[0]


def _runtime(tmp_path, sid="slice-001") -> dict:
    return Runtime(repo_home("demo", tmp_path / "ns") / "runtime.json").get(sid)


def _phase(tmp_path, sid="slice-001") -> dict:
    snap = StateStore(repo_home("demo", tmp_path / "ns") / "state.json").snapshot()
    return snap["slices"][sid]


def test_question_flow_end_to_end(repo, tmp_path) -> None:
    _write_slice(repo)
    agent = Scripted(_ask, _finish)
    d = _daemon(repo, tmp_path, agent)
    cfg = _config(tmp_path, repo)

    [r] = d.tick()
    assert (r.status, r.question) == ("question", "Which db?")
    sl = LocalMdSource(repo).get("slice-001")
    assert sl.status == "needs-detail"
    assert "Question from the agent:\n\nWhich db?" in sl.body
    assert 'nsh answer slice-001 "..."' in sl.body
    [q] = [e for e in _events(tmp_path) if e["kind"] == "question"]
    assert q["question"] == "Which db?"
    assert _phase(tmp_path)["phase"] == "question"
    assert _phase(tmp_path)["detail"] == "Which db?"
    assert d.worktrees.exists("slice-001")
    assert _runtime(tmp_path) == {
        "session_id": "sess",
        "branch": "nightshift/slice-001",
        "interrupted": False,
        "awaiting_answer": True,
    }
    assert d.tick() == []  # parked until someone answers

    message = run_answer_cli(
        repo, "slice-001", "use sqlite", config_path=cfg, home_root=tmp_path / "ns"
    )
    assert message == "answered slice-001; it will resume on the next poll"
    sl = LocalMdSource(repo).get("slice-001")
    assert sl.status == "ready" and sl.section("Answers") == "- use sqlite"
    assert [e for e in _events(tmp_path) if e["kind"] == "answered"][0]["slice"] == "slice-001"

    [r] = d.tick()
    assert r.status == "done"
    assert agent.prompts[1] == ANSWER_PROMPT.format(answer="use sqlite")
    assert "sess" in agent.argvs[1]  # the same session
    assert _runtime(tmp_path) == {}


def test_question_without_runtime(repo, tmp_path) -> None:
    _write_slice(repo)
    d = _daemon(repo, tmp_path, Scripted(_ask), runtime=False)
    assert [r.status for r in d.tick()] == ["question"]


def test_needs_files_flow_end_to_end(repo, tmp_path) -> None:
    _write_slice(repo, touches=["a.py"])
    agent = Scripted(_need, _finish)
    d = _daemon(repo, tmp_path, agent)

    [r] = d.tick()
    assert (r.status, r.needs_files) == ("needs_files", ["b.py", "c.py"])
    sl = LocalMdSource(repo).get("slice-001")
    assert sl.status == "ready"
    assert sl.touches() == {"a.py", "b.py", "c.py"}
    [e] = [e for e in _events(tmp_path) if e["kind"] == "needs_files"]
    assert e["files"] == ["b.py", "c.py"]
    assert _phase(tmp_path)["phase"] == "queued"
    assert _runtime(tmp_path)["resume_prompt"] == FILES_PROMPT.format(files="b.py, c.py")
    assert _runtime(tmp_path)["interrupted"] is True

    [r] = d.tick()
    assert r.status == "done"
    assert agent.prompts[1] == FILES_PROMPT.format(files="b.py, c.py")
    assert _runtime(tmp_path) == {}


def test_needs_files_keeps_legacy_scope_hints(repo, tmp_path) -> None:
    (repo / ".slices" / "slice-001.md").write_text(
        "---\nid: slice-001\ntitle: t\nstatus: ready\n---\n"
        "## Goal\ndo it\n\n## Scope hints\n- a.py\n",
        encoding="utf-8",
    )
    d = _daemon(repo, tmp_path, Scripted(_need))
    d.tick()
    sl = LocalMdSource(repo).get("slice-001")
    assert sl is not None
    assert sl.touches() == {"a.py", "b.py", "c.py"}


def test_needs_files_without_runtime(repo, tmp_path) -> None:
    _write_slice(repo)
    d = _daemon(repo, tmp_path, Scripted(_need), runtime=False)
    assert [r.status for r in d.tick()] == ["needs_files"]


def test_blocked_after_a_resume_prompt_clears_it(repo, tmp_path) -> None:
    _write_slice(repo)
    d = _daemon(repo, tmp_path, Scripted(_need, lambda cwd: None))
    d.tick()
    [r] = d.tick()  # resumed; the agent writes no result.json
    assert r.status == "blocked"
    assert "resume_prompt" not in _runtime(tmp_path)


def test_answer_for_unknown_slice(repo, tmp_path) -> None:
    cfg = _config(tmp_path, repo)
    with pytest.raises(LookupError, match="no slice 'nope'"):
        run_answer_cli(repo, "nope", "x", config_path=cfg, home_root=tmp_path / "ns")


def test_cli_answer(monkeypatch) -> None:
    seen = {}

    def fake(repo, slice_id, text, **kw):
        seen.update(slice=slice_id, text=text)
        return "answered " + slice_id

    monkeypatch.setattr("nightshift.wait.run_answer_cli", fake)
    res = CliRunner().invoke(app, ["answer", "issue-3", "use sqlite"])
    assert res.exit_code == 0 and "answered issue-3" in res.output
    assert seen == {"slice": "issue-3", "text": "use sqlite"}

    def missing(*a, **kw):
        raise LookupError("no slice 'issue-9'")

    monkeypatch.setattr("nightshift.wait.run_answer_cli", missing)
    res = CliRunner().invoke(app, ["answer", "issue-9", "x"])
    assert res.exit_code == 1 and "no slice 'issue-9'" in res.output
