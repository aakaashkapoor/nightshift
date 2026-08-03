"""Tests for the AI review step (parsing + the run_slice review gate)."""

import json
import subprocess
from pathlib import Path

import pytest

from nightshift.executor import Executor, RunOutput
from nightshift.pipeline import run_slice
from nightshift.review import Finding, ReviewResult, parse_findings
from nightshift.slice import Slice
from nightshift.worktree import WorktreeManager


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
    return r


class Agent:
    def __call__(self, argv, cwd, stdin) -> RunOutput:
        Path(cwd, "impl.txt").write_text("x", encoding="utf-8")
        return RunOutput(0, json.dumps({"result": "ok", "session_id": "s"}), "")


def _slice() -> Slice:
    return Slice.parse("---\nid: slice-001\ntitle: t\nstatus: ready\n---\n## Goal\ng\n")


def _run(repo, tmp_path, reviewer):
    return run_slice(
        _slice(),
        repo_path=repo,
        check_cmd="echo ok",
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        executor=Executor(runner=Agent()),
        reviewer=reviewer,
    )


# --- parsing ------------------------------------------------------------------


def test_parse_findings_valid() -> None:
    rr = parse_findings(json.dumps({"findings": [{"severity": "blocking", "message": "m"}]}))
    assert len(rr.blocking) == 1 and rr.blocking[0].message == "m"


def test_parse_findings_empty_and_malformed() -> None:
    assert parse_findings('{"findings": []}').findings == []
    assert parse_findings("not json at all").findings == []


# --- the review gate in run_slice ---------------------------------------------


class FlakyReviewer:
    """Blocking on the first look, clean after the fix round."""

    def __init__(self):
        self.calls = 0

    def review(self, wt, sl) -> ReviewResult:
        self.calls += 1
        if self.calls == 1:
            return ReviewResult([Finding("blocking", "fix me")])
        return ReviewResult([])


class AlwaysBlock:
    def review(self, wt, sl) -> ReviewResult:
        return ReviewResult([Finding("blocking", "nope")])


class AdvisoryOnly:
    def review(self, wt, sl) -> ReviewResult:
        return ReviewResult([Finding("advisory", "nit")])


def test_blocking_finding_forces_fix_then_commits(repo, tmp_path) -> None:
    reviewer = FlakyReviewer()
    result = _run(repo, tmp_path, reviewer)
    assert result.status == "done"
    assert reviewer.calls == 2  # reviewed, fixed, reviewed-clean


def test_unresolved_blocking_findings_block(repo, tmp_path) -> None:
    result = _run(repo, tmp_path, AlwaysBlock())
    assert result.status == "blocked"
    assert "review" in result.detail


def test_advisory_findings_do_not_block(repo, tmp_path) -> None:
    result = _run(repo, tmp_path, AdvisoryOnly())
    assert result.status == "done"
