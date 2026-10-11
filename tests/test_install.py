"""Per-worktree dependency install: detection, config, `nsh init`, and run_slice."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightshift.cli import app
from nightshift.config import Config, RepoConfig, register_repo
from nightshift.daemon import Daemon
from nightshift.detect import detect_install, detect_sync
from nightshift.events import EventLog
from nightshift.executor import Executor, RunOutput
from nightshift.pipeline import run_slice
from nightshift.slice import Slice
from nightshift.source import LocalMdSource
from nightshift.state import StateStore
from nightshift.worktree import WorktreeManager

runner = CliRunner()


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
    return r


# --- detection ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lockfile", "command"),
    [
        ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
        ("package-lock.json", "npm ci"),
        ("yarn.lock", "yarn install --frozen-lockfile"),
        ("uv.lock", "uv sync --frozen"),
    ],
)
def test_detect_install_table(tmp_path, lockfile, command) -> None:
    (tmp_path / lockfile).write_text("", encoding="utf-8")
    assert detect_install(tmp_path) == command
    assert detect_sync(tmp_path) == command


def test_detect_install_none(tmp_path) -> None:
    assert detect_install(tmp_path) is None
    assert detect_sync(tmp_path) is None


def test_detect_install_prefers_pnpm_over_npm(tmp_path) -> None:
    (tmp_path / "package-lock.json").write_text("", encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert detect_install(tmp_path) == "pnpm install --frozen-lockfile"


# --- config ---------------------------------------------------------------------


def test_register_repo_writes_install(tmp_path) -> None:
    cfg = tmp_path / "c.yaml"
    register_repo(cfg, name="a", path=tmp_path, check="x", install="npm ci")
    assert Config.load(cfg).repo("a").install == "npm ci"
    register_repo(cfg, name="b", path=tmp_path, check="x")
    assert Config.load(cfg).repo("b").install is None


# --- nsh init -------------------------------------------------------------------


def test_init_fills_install_and_sync(tmp_path) -> None:
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    cfg = tmp_path / "c.yaml"
    result = runner.invoke(
        app, ["init", "--repo", str(tmp_path), "--config", str(cfg), "--name", "x", "--check", "t"]
    )
    assert result.exit_code == 0
    repo_cfg = Config.load(cfg).repo("x")
    assert repo_cfg.install == "npm ci" and repo_cfg.sync == "npm ci"
    assert "install: npm ci" in result.stdout and "sync:    npm ci" in result.stdout


def test_init_explicit_install_and_sync_win(tmp_path) -> None:
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    cfg = tmp_path / "c.yaml"
    args = ["init", "--repo", str(tmp_path), "--config", str(cfg), "--name", "x", "--check", "t"]
    result = runner.invoke(app, [*args, "--install", "make deps", "--sync", "make sync"])
    assert result.exit_code == 0
    repo_cfg = Config.load(cfg).repo("x")
    assert (repo_cfg.install, repo_cfg.sync) == ("make deps", "make sync")


def test_init_without_lockfile_has_no_install(tmp_path) -> None:
    cfg = tmp_path / "c.yaml"
    result = runner.invoke(
        app, ["init", "--repo", str(tmp_path), "--config", str(cfg), "--name", "x", "--check", "t"]
    )
    assert result.exit_code == 0
    repo_cfg = Config.load(cfg).repo("x")
    assert repo_cfg.install is None and repo_cfg.sync is None
    assert "install:" not in result.stdout


# --- run_slice ------------------------------------------------------------------


def _marker_cmd(tmp_path, *, fail=False) -> tuple[str, Path]:
    marker = tmp_path / "installed.txt"
    script = tmp_path / "inst.py"
    script.write_text(
        f"import sys\nopen({str(marker)!r}, 'a').write('x')\n"
        + ("print('boom: missing dep')\nsys.exit(1)\n" if fail else ""),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"', marker


def _slice() -> Slice:
    return Slice.parse("---\nid: slice-001\ntitle: t\nstatus: ready\n---\n## Goal\ndo it\n")


class Agent:
    def __init__(self):
        self.calls = 0

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        self.calls += 1
        Path(cwd, "impl.txt").write_text("x", encoding="utf-8")
        return RunOutput(0, json.dumps({"result": "ok", "session_id": "s"}), "")


def _run(repo, tmp_path, wts, agent, **kw):
    return run_slice(
        _slice(),
        repo_path=repo,
        check_cmd="echo ok",
        worktrees=wts,
        executor=Executor(runner=agent),
        **kw,
    )


def test_install_runs_once_on_create(repo, tmp_path) -> None:
    cmd, marker = _marker_cmd(tmp_path)
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    result = _run(repo, tmp_path, wts, Agent(), install_cmd=cmd)
    assert result.status == "done"
    assert marker.read_text(encoding="utf-8") == "x"


def test_install_skipped_on_resume(repo, tmp_path) -> None:
    cmd, marker = _marker_cmd(tmp_path)
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    wts.create("slice-001")
    result = _run(repo, tmp_path, wts, Agent(), install_cmd=cmd, resume_session="sess")
    assert result.status == "done"
    assert not marker.exists()


def test_no_install_command_is_fine(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    assert _run(repo, tmp_path, wts, Agent()).status == "done"


def test_install_failure_blocks_with_output_tail(repo, tmp_path) -> None:
    cmd, _ = _marker_cmd(tmp_path, fail=True)
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    agent = Agent()
    result = _run(repo, tmp_path, wts, agent, install_cmd=cmd)
    assert result.status == "blocked"
    assert result.detail.startswith("install failed: ")
    assert "boom: missing dep" in result.detail
    assert agent.calls == 0
    assert wts.exists("slice-001")  # preserved for inspection


# --- daemon ---------------------------------------------------------------------


def test_daemon_passes_install_to_run_slice(repo, tmp_path) -> None:
    cmd, marker = _marker_cmd(tmp_path)
    (repo / ".slices").mkdir()
    (repo / ".slices" / "slice-001.md").write_text(
        "---\nid: slice-001\ntitle: t\nstatus: ready\n---\n## Goal\ndo it\n", encoding="utf-8"
    )
    d = Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(
            name="demo", path=repo, check="echo ok", base_branch="main", install=cmd
        ),
        executor=Executor(runner=Agent()),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        events=EventLog(tmp_path / "home" / "events.jsonl"),
        state=StateStore(tmp_path / "home" / "state.json"),
    )
    assert [r.status for r in d.tick()] == ["done"]
    assert marker.read_text(encoding="utf-8") == "x"
