"""Tests for resuming a blocked slice (resume_slice + run_resume_cli)."""

import json
import subprocess
import sys

import pytest

from nightshift.config import RepoConfig
from nightshift.daemon import Daemon
from nightshift.executor import Executor, RunOutput
from nightshift.paths import repo_home
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


# --- resume on a PR-shipping (babysit) repo ships the way the daemon does ------


class FakePR:
    def __init__(self, fail=False):
        self.fail = fail
        self.opened = []
        self.merged = []
        self.unlabeled = []

    def open(self, *, branch, base, title, body, labels=()):
        if self.fail:
            raise RuntimeError("gh down")
        self.opened.append((branch, labels))
        return f"https://gh/pr/{branch}"

    def merge(self, branch):
        self.merged.append(branch)

    def unlabel(self, ref, label):
        self.unlabeled.append((ref, label))


class LabelledSource(LocalMdSource):
    def __init__(self, repo, labels):
        super().__init__(repo)
        self.labels = labels

    def get(self, slice_id):
        sl = super().get(slice_id)
        if sl is not None:
            sl.labels = set(self.labels)
        return sl


def _blocked_babysit_slice(repo, tmp_path, monkeypatch, labels, pr):
    """A github-issues (babysit) repo whose slice the daemon blocked; resume wired to fakes."""
    import nightshift.daemon as d

    (repo / ".slices" / "slice-001.md").write_text(SLICE_TEXT, encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "repos": {
                    "demo": {
                        "path": str(repo),
                        "check": CHECK,
                        "base_branch": "main",
                        "source": "github-issues",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    Daemon(
        source=LabelledSource(repo, labels),
        repo_cfg=RepoConfig(name="demo", path=repo, check=CHECK, babysit=True),
        executor=Executor(runner=FailAgent()),
        worktrees=WorktreeManager(repo),
        pr=pr,
        max_attempts=1,
    ).tick()
    assert LocalMdSource(repo).get("slice-001").status == "blocked"
    monkeypatch.setattr(d, "build_source", lambda rc: LabelledSource(repo, labels))
    monkeypatch.setattr(d, "GitHubPR", lambda path: pr)
    return cfg


def _resume(repo, cfg, tmp_path):
    return run_resume_cli(
        "slice-001",
        repo=repo,
        config_path=cfg,
        executor=Executor(runner=OkAgent()),
        runtime_path=tmp_path / "runtime.json",
    )


def test_resume_review_labelled_slice_opens_a_pr_and_waits(repo, tmp_path, monkeypatch) -> None:
    pr = FakePR()
    cfg = _blocked_babysit_slice(repo, tmp_path, monkeypatch, ["review", "visual"], pr)
    main_before = _git(repo, "rev-parse", "main")

    result = _resume(repo, cfg, tmp_path)

    assert result.status == "in-review"
    assert result.detail == "https://gh/pr/nightshift/slice-001"
    assert pr.opened == [("nightshift/slice-001", ("nightshift:babysit",))]
    assert pr.merged == []
    assert _git(repo, "rev-parse", "main") == main_before  # never merged into local main
    assert LocalMdSource(repo).get("slice-001").status == "in-review"
    log = (repo_home("demo") / "events.jsonl").read_text(encoding="utf-8")
    assert '"review_ready"' in log


def test_resume_unreviewed_slice_merges_its_pr(repo, tmp_path, monkeypatch) -> None:
    pr = FakePR()
    cfg = _blocked_babysit_slice(repo, tmp_path, monkeypatch, [], pr)
    main_before = _git(repo, "rev-parse", "main")

    result = _resume(repo, cfg, tmp_path)

    assert result.status == "done"
    assert pr.opened == [("nightshift/slice-001", ("nightshift:babysit",))]
    assert pr.merged == ["nightshift/slice-001"]  # merged on GitHub, not locally
    assert _git(repo, "rev-parse", "main") == main_before
    assert LocalMdSource(repo).get("slice-001").status == "done"


def test_resume_keeps_the_branch_when_the_pr_cannot_open(repo, tmp_path, monkeypatch) -> None:
    pr = FakePR(fail=True)
    cfg = _blocked_babysit_slice(repo, tmp_path, monkeypatch, ["review"], pr)

    result = _resume(repo, cfg, tmp_path)

    assert result.status == "blocked"
    assert "PR failed" in result.detail
    assert _git(repo, "branch", "--list", "nightshift/slice-001")  # not deleted
    assert WorktreeManager(repo).exists("slice-001")
    assert LocalMdSource(repo).get("slice-001").status == "blocked"
    assert Runtime(tmp_path / "runtime.json").get("slice-001")["branch"] == "nightshift/slice-001"
