"""Tests for the babysit helpers: nsh pr checkout | merge | comment."""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightshift import prtools
from nightshift.check import CheckResult
from nightshift.cli import app
from nightshift.prtools import PrError

runner = CliRunner()


class Calls:
    def __init__(self, replies=None, fail_on=None):
        self.calls = []
        self.replies = replies or {}
        self.fail_on = fail_on

    def __call__(self, *args):
        self.calls.append(args)
        if self.fail_on and args[: len(self.fail_on)] == self.fail_on:
            raise RuntimeError(f"{' '.join(args)} failed: boom")
        for prefix, reply in self.replies.items():
            if args[: len(prefix)] == prefix:
                return reply
        return ""


GH_VIEW = {("pr", "view", "7", "--json", "headRefName"): json.dumps({"headRefName": "feat/x"})}


def _ok(cmd, cwd):
    return CheckResult(True, "", 0)


# --- checkout ----------------------------------------------------------------------


def test_checkout_adds_a_worktree_installs_and_returns_the_path(tmp_path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    gh, git, ran = Calls(GH_VIEW), Calls(), []

    path = prtools.checkout(
        7,
        repo=repo,
        install="npm ci",
        gh=gh,
        git=git,
        run=lambda c, d: ran.append((c, d)) or _ok(c, d),
    )

    assert path == tmp_path / "proj-pr-7"
    assert git.calls == [
        ("fetch", "origin", "feat/x"),
        ("worktree", "add", str(path), "origin/feat/x"),
    ]
    assert ran == [("npm ci", path)]


def test_checkout_without_install_skips_it(tmp_path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    prtools.checkout(7, repo=repo, install=None, gh=Calls(GH_VIEW), git=Calls(), run=None)


def test_checkout_fails_if_the_worktree_exists(tmp_path) -> None:
    repo = tmp_path / "proj"
    (tmp_path / "proj-pr-7").mkdir(parents=True)
    git = Calls()
    with pytest.raises(PrError, match="already exists"):
        prtools.checkout(7, repo=repo, install=None, gh=Calls(GH_VIEW), git=git)
    assert git.calls == []


def test_checkout_reports_a_failed_install(tmp_path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()

    def bad(cmd, cwd):
        return CheckResult(False, "x" * 500 + "ERESOLVE", 1)

    with pytest.raises(PrError, match="install failed in .*ERESOLVE"):
        prtools.checkout(7, repo=repo, install="npm ci", gh=Calls(GH_VIEW), git=Calls(), run=bad)


def test_checkout_lets_gh_failures_through(tmp_path) -> None:
    gh = Calls(fail_on=("pr", "view"))
    with pytest.raises(RuntimeError, match="boom"):
        prtools.checkout(7, repo=tmp_path / "proj", install=None, gh=gh, git=Calls())


# --- merge -------------------------------------------------------------------------


class FakeMerger:
    def __init__(self, error=None):
        self.merged = []
        self.error = error

    def merge(self, branch):
        if self.error:
            raise RuntimeError(self.error)
        self.merged.append(branch)


class FakeTrees:
    def __init__(self, present):
        self.present = present
        self.torn = []

    def exists(self, sid):
        return self.present

    def teardown(self, sid):
        self.torn.append(sid)


def test_merge_by_number_then_removes_the_pr_worktree(tmp_path) -> None:
    pr, trees = FakeMerger(), FakeTrees(True)
    removed = prtools.merge(7, repo=tmp_path / "proj", pr=pr, worktrees=trees)
    assert pr.merged == ["7"] and trees.torn == ["proj-pr-7"] and removed


def test_merge_without_a_worktree_removes_nothing(tmp_path) -> None:
    pr, trees = FakeMerger(), FakeTrees(False)
    assert not prtools.merge(7, repo=tmp_path / "proj", pr=pr, worktrees=trees)
    assert trees.torn == []


def test_merge_failure_keeps_the_worktree(tmp_path) -> None:
    trees = FakeTrees(True)
    with pytest.raises(RuntimeError, match="not mergeable: CONFLICTING"):
        prtools.merge(
            7,
            repo=tmp_path / "proj",
            pr=FakeMerger("not mergeable: CONFLICTING"),
            worktrees=trees,
        )
    assert trees.torn == []


# --- comment -----------------------------------------------------------------------


def test_comment_posts_the_verified_text() -> None:
    gh = Calls()
    prtools.comment(7, "login works on staging", gh=gh)
    assert gh.calls == [("pr", "comment", "7", "--body", "✅ Verified:\n\nlogin works on staging")]


def test_comment_needs_something_that_was_verified() -> None:
    gh = Calls()
    with pytest.raises(PrError, match="what you verified"):
        prtools.comment(7, "  ", gh=gh)
    assert gh.calls == []


# --- the CLI wrappers (wiring) -----------------------------------------------------


class FakeGitHubPR(Calls):
    """Stands in for GitHubPR(repo): .gh / .git runners plus merge()."""

    instances: list = []

    def __init__(self, repo):
        super().__init__()
        self.repo = repo
        self.gh = Calls(GH_VIEW)
        self.git = Calls()
        self.merger = FakeMerger()
        FakeGitHubPR.instances.append(self)

    def merge(self, branch):
        self.merger.merge(branch)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    FakeGitHubPR.instances = []
    monkeypatch.setattr(prtools, "GitHubPR", FakeGitHubPR)
    repo = tmp_path / "proj"
    repo.mkdir()
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        f"repos:\n  proj:\n    path: {repo.as_posix()}\n    check: x\n    install: npm ci\n",
        encoding="utf-8",
    )
    return repo, cfg


def _invoke(*args):
    return runner.invoke(app, ["pr", *map(str, args)])


def test_cli_checkout_prints_the_path(wired, monkeypatch) -> None:
    repo, cfg = wired
    ran = []
    monkeypatch.setattr(prtools, "run_check", lambda c, d: ran.append(c) or _ok(c, d))
    result = _invoke("checkout", 7, "--repo", repo, "--config", cfg)
    assert result.exit_code == 0
    assert result.stdout.strip() == str(repo.parent / "proj-pr-7")
    assert ran == ["npm ci"]


def test_cli_checkout_in_an_unregistered_repo_fails_fast(tmp_path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("repos: {}\n", encoding="utf-8")
    result = _invoke("checkout", 7, "--repo", tmp_path, "--config", cfg)
    assert result.exit_code == 1
    assert "not registered" in result.output


def test_cli_checkout_fails_with_a_clear_message(wired) -> None:
    repo, cfg = wired
    (repo.parent / "proj-pr-7").mkdir()
    result = _invoke("checkout", 7, "--repo", repo, "--config", cfg)
    assert result.exit_code == 1 and "already exists" in result.output


def test_cli_checkout_reports_runner_failures(wired, monkeypatch) -> None:
    repo, cfg = wired

    class Down(FakeGitHubPR):
        def __init__(self, repo):
            super().__init__(repo)
            self.gh = Calls(fail_on=("pr", "view"))

    monkeypatch.setattr(prtools, "GitHubPR", Down)
    result = _invoke("checkout", 7, "--repo", repo, "--config", cfg)
    assert result.exit_code == 1 and "boom" in result.output


def test_cli_merge_merges_and_removes_the_worktree(wired) -> None:
    repo, _cfg = wired
    result = _invoke("merge", 7, "--repo", repo)
    assert result.exit_code == 0 and "merged #7" in result.stdout
    assert FakeGitHubPR.instances[0].merger.merged == ["7"]


def test_cli_merge_removes_an_existing_pr_worktree(wired, monkeypatch) -> None:
    repo, _cfg = wired
    trees = FakeTrees(True)
    monkeypatch.setattr(prtools, "WorktreeManager", lambda *a, **k: trees)
    result = _invoke("merge", 7, "--repo", repo)
    assert result.exit_code == 0 and "removed proj-pr-7" in result.stdout
    assert trees.torn == ["proj-pr-7"]


def test_cli_merge_failure_exits_nonzero(wired, monkeypatch) -> None:
    repo, _cfg = wired

    class Conflicting(FakeGitHubPR):
        def merge(self, branch):
            raise RuntimeError("not mergeable: CONFLICTING")

    monkeypatch.setattr(prtools, "GitHubPR", Conflicting)
    result = _invoke("merge", 7, "--repo", repo)
    assert result.exit_code == 1 and "not mergeable: CONFLICTING" in result.output


def test_cli_comment_posts(wired) -> None:
    repo, _cfg = wired
    result = _invoke("comment", 7, "clicked through checkout", "--repo", repo)
    assert result.exit_code == 0
    assert FakeGitHubPR.instances[0].gh.calls == [
        ("pr", "comment", "7", "--body", "✅ Verified:\n\nclicked through checkout")
    ]


def test_cli_comment_without_text_fails(wired) -> None:
    repo, _cfg = wired
    result = _invoke("comment", 7, " ", "--repo", repo)
    assert result.exit_code == 1 and "what you verified" in result.output


# --- real git: checkout then teardown through the real worktree manager ---------------


def test_checkout_and_merge_cleanup_with_real_git(tmp_path) -> None:
    def git(cwd, *a):
        return subprocess.run(
            ["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True
        ).stdout

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "proj"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.email", "t@e.local")
    git(repo, "config", "user.name", "T")
    Path(repo, "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "origin", "main")
    git(repo, "push", "origin", "main:feat/x")

    path = prtools.checkout(
        7, repo=repo, install=None, gh=Calls(GH_VIEW), git=lambda *a: git(repo, *a)
    )
    assert (path / "a.txt").exists()

    trees = prtools.WorktreeManager(repo, worktrees_root=repo.parent)
    assert prtools.merge(7, repo=repo, pr=FakeMerger(), worktrees=trees)
    assert not path.exists()
