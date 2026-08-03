"""Tests for the `nsh run` wiring (run_slice_cli) — config + slice lookup + run.

Uses a fake agent (writes a file) and a trivial always-pass check, so the wiring
is exercised end-to-end without a live Claude.
"""

import json
import subprocess
from pathlib import Path

import pytest

from nightshift.executor import Executor, RunOutput
from nightshift.pipeline import run_slice_cli

SLICE_TEXT = (
    "---\nid: slice-001\ntitle: Wire it up\nstatus: ready\n---\n## Goal\nWire it.\n"
)


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class Agent:
    def __call__(self, argv, cwd, stdin) -> RunOutput:
        Path(cwd, "impl.txt").write_text("done", encoding="utf-8")
        return RunOutput(0, json.dumps({"result": "ok", "session_id": "s1"}), "")


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


@pytest.fixture
def config_file(tmp_path, repo):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'repos:\n  demo:\n    path: "{repo.as_posix()}"\n    check: "echo ok"\n',
        encoding="utf-8",
    )
    return cfg


def test_run_cli_resolves_slice_by_id(repo, config_file) -> None:
    slices = repo / ".slices"
    slices.mkdir()
    (slices / "slice-001.md").write_text(SLICE_TEXT, encoding="utf-8")

    result = run_slice_cli(
        "slice-001", repo=repo, config_path=config_file, executor=Executor(runner=Agent())
    )
    assert result.status == "done"
    assert result.commit is not None


def test_run_cli_accepts_slice_path(repo, config_file, tmp_path) -> None:
    slice_path = tmp_path / "loose-slice.md"
    slice_path.write_text(SLICE_TEXT, encoding="utf-8")

    result = run_slice_cli(
        str(slice_path), repo=repo, config_path=config_file, executor=Executor(runner=Agent())
    )
    assert result.status == "done"


def test_run_cli_unknown_slice_raises(repo, config_file) -> None:
    with pytest.raises(FileNotFoundError):
        run_slice_cli(
            "nope", repo=repo, config_path=config_file, executor=Executor(runner=Agent())
        )
