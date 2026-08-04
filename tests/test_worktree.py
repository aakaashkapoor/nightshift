"""Tests for the worktree manager — exercises real `git worktree`."""

import subprocess

import pytest

from nightshift.worktree import WorktreeManager


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo with one commit on `main`."""
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(r)], check=True, capture_output=True, text=True
    )
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("hello\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


@pytest.fixture
def wm(repo, tmp_path):
    return WorktreeManager(repo, worktrees_root=tmp_path / "wts")


def test_create_makes_worktree_and_branch(wm, repo) -> None:
    wt = wm.create("slice-001")
    assert wt.path.exists()
    assert (wt.path / "README.md").exists()  # base content checked out
    assert wt.branch == "nightshift/slice-001"
    branches = _git(repo, "branch", "--list", "nightshift/slice-001")
    assert "nightshift/slice-001" in branches


def test_worktrees_are_isolated(wm) -> None:
    a = wm.create("slice-001")
    b = wm.create("slice-002")
    (a.path / "a.txt").write_text("A", encoding="utf-8")
    assert not (b.path / "a.txt").exists()


def test_teardown_removes_worktree_and_branch(wm, repo) -> None:
    wm.create("slice-001")
    wm.teardown("slice-001")
    assert not wm.path_for("slice-001").exists()
    branches = _git(repo, "branch", "--list", "nightshift/slice-001")
    assert branches.strip() == ""


def test_preserve_keeps_worktree(wm) -> None:
    wt = wm.create("slice-001")
    wm.preserve("slice-001")
    assert wt.path.exists()
    assert wm.exists("slice-001")


def test_list_slices(wm) -> None:
    wm.create("slice-001")
    wm.create("slice-002")
    assert set(wm.list_slices()) == {"slice-001", "slice-002"}


def test_create_symlinks_shared_dirs(repo, tmp_path) -> None:
    # A gitignored shared dir (e.g. node_modules) in the main checkout.
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "marker.txt").write_text("dep", encoding="utf-8")
    wm = WorktreeManager(repo, worktrees_root=tmp_path / "wts", symlink_dirs=["node_modules"])

    wt = wm.create("slice-001")

    # The worktree sees the shared dir through the link (so `npm run check` works).
    assert (wt.path / "node_modules" / "marker.txt").read_text(encoding="utf-8") == "dep"


def test_teardown_removes_link_without_deleting_target(repo, tmp_path) -> None:
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "marker.txt").write_text("dep", encoding="utf-8")
    wm = WorktreeManager(repo, worktrees_root=tmp_path / "wts", symlink_dirs=["node_modules"])
    wm.create("slice-001")

    wm.teardown("slice-001")  # must not raise, and must NOT delete the real dir

    assert not wm.exists("slice-001")
    assert (repo / "node_modules" / "marker.txt").exists()  # target intact


def test_create_skips_missing_symlink_dir(repo, tmp_path) -> None:
    wm = WorktreeManager(repo, worktrees_root=tmp_path / "wts", symlink_dirs=["nope"])
    wt = wm.create("slice-001")  # must not raise
    assert not (wt.path / "nope").exists()
    wm.teardown("slice-001")  # teardown with a configured-but-absent link must be fine
    assert not wm.exists("slice-001")


def test_create_from_moved_main_uses_tip(wm, repo) -> None:
    # A second commit lands on main; a new worktree should branch from the new tip.
    (repo / "second.txt").write_text("2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "second")
    wt = wm.create("slice-003", base_branch="main")
    assert (wt.path / "second.txt").exists()
