"""Tests for resuming a blocked slice (resume_slice + run_resume_cli)."""

import json
import subprocess
import sys

import pytest

from nightshift.config import RepoConfig
from nightshift.daemon import Daemon
from nightshift.executor import Executor, RunOutput
from nightshift.pipeline import resume_slice, run_resume_cli, run_slice
from nightshift.runtime import Runtime
from nightshift.slice import Slice
from nightshift.source import LocalMdSource
from nightshift.worktree import WorktreeManager

CHECK = f'"{sys.executable}" -c "import os,sys; sys.exit(0 if os.path.exists(\'impl.txt\') else 1)"'

SLICE_TEXT = "---\nid: slice-001\ntitle: t\nstatus: ready\n---\n## Goal\ng\n"


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class FailAgent:
    def __call__(self, argv, cwd, stdin) -> RunOutput:
        return RunOutput(0, json.dumps({"result": "stuck", "session_id": "sess_1"}), "")


class OkAgent:
    def __call__(self, argv, cwd, stdin) -> RunOutput:
        from pathlib import Path

        Path(cwd, "impl.txt").write_text("done", encoding="utf-8")
        return RunOutput(0, json.dumps({"result": "ok", "session_id": "sess_2"}), "")


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


def test_resume_completes_a_blocked_slice(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    sl = Slice.parse(SLICE_TEXT)

    blocked = run_slice(
        sl,
        repo_path=repo,
        check_cmd=CHECK,
        worktrees=wts,
        executor=Executor(runner=FailAgent()),
        max_attempts=1,
    )
    assert blocked.status == "blocked"
    assert wts.exists("slice-001")  # worktree preserved

    resumed = resume_slice(
        sl,
        check_cmd=CHECK,
        worktrees=wts,
        executor=Executor(runner=OkAgent()),
        max_attempts=1,
    )
    assert resumed.status == "done"
    assert resumed.commit is not None


def test_resume_cli_end_to_end(repo, tmp_path) -> None:
    (repo / ".slices" / "slice-001.md").write_text(SLICE_TEXT, encoding="utf-8")
    cfg = tmp_path / "config.json"  # JSON is valid YAML -> no quoting headaches
    cfg.write_text(
        json.dumps({"repos": {"demo": {"path": str(repo), "check": CHECK, "base_branch": "main"}}}),
        encoding="utf-8",
    )
    runtime_path = tmp_path / "runtime.json"

    # 1) daemon blocks it (agent never produces impl.txt).
    Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="demo", path=repo, check=CHECK, base_branch="main"),
        executor=Executor(runner=FailAgent()),
        worktrees=WorktreeManager(repo),
        runtime=Runtime(runtime_path),
        max_attempts=1,
    ).tick()
    assert LocalMdSource(repo).get("slice-001").status == "blocked"
    assert Runtime(runtime_path).get("slice-001").get("session_id") == "sess_1"

    # 2) resume with a working agent -> done + merged + runtime forgotten.
    result = run_resume_cli(
        "slice-001",
        repo=repo,
        config_path=cfg,
        executor=Executor(runner=OkAgent()),
        runtime_path=runtime_path,
    )
    assert result.status == "done"
    assert LocalMdSource(repo).get("slice-001").status == "done"
    assert "impl.txt" in _git(repo, "ls-tree", "--name-only", "main").split()
    assert Runtime(runtime_path).get("slice-001") == {}  # forgotten after merge
