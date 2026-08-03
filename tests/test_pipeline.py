"""End-to-end tests for run_slice — real worktree + real check + real commit.

The *agent* is faked (a runner that writes files into the worktree), so no live
Claude is needed, but everything else is real git.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nightshift.executor import Executor, RunOutput
from nightshift.pipeline import run_slice
from nightshift.slice import Slice
from nightshift.worktree import WorktreeManager

# Passes only if the agent created impl.txt in the worktree (same quoting pattern
# as test_check.py, verified to work on this Windows setup).
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
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


def make_slice() -> Slice:
    return Slice.parse(
        "---\nid: slice-001\ntitle: Implement the thing\nstatus: ready\n---\n"
        "## Goal\nDo the thing.\n"
    )


class Agent:
    """Fake headless agent: writes impl.txt into the worktree on attempt >= succeed_on."""

    def __init__(self, succeed_on: int = 1):
        self.calls = 0
        self.succeed_on = succeed_on

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        self.calls += 1
        if self.calls >= self.succeed_on:
            Path(cwd, "impl.txt").write_text("done", encoding="utf-8")
        return RunOutput(
            0, json.dumps({"result": f"attempt {self.calls}", "session_id": f"s{self.calls}"}), ""
        )


def _run(repo, tmp_path, agent, **kw):
    return run_slice(
        make_slice(),
        repo_path=repo,
        check_cmd=CHECK,
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        executor=Executor(runner=agent),
        **kw,
    )


def test_success_makes_one_clean_commit_and_marks_done(repo, tmp_path) -> None:
    result = _run(repo, tmp_path, Agent(succeed_on=1))
    assert result.status == "done"
    assert result.attempts == 1
    assert result.commit is not None
    # Exactly ONE commit beyond main, and it contains the agent's file.
    count = _git(repo, "rev-list", "--count", "nightshift/slice-001", "^main")
    assert count == "1"
    files = _git(repo, "show", "--name-only", "--format=", "nightshift/slice-001")
    assert "impl.txt" in files


def test_retries_then_succeeds(repo, tmp_path) -> None:
    result = _run(repo, tmp_path, Agent(succeed_on=2), max_attempts=3)
    assert result.status == "done"
    assert result.attempts == 2


def test_blocks_after_max_attempts_and_preserves_worktree(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    result = run_slice(
        make_slice(),
        repo_path=repo,
        check_cmd=CHECK,
        worktrees=wts,
        executor=Executor(runner=Agent(succeed_on=99)),
        max_attempts=2,
    )
    assert result.status == "blocked"
    assert result.attempts == 2
    assert result.commit is None
    assert wts.exists("slice-001")  # preserved for inspection
