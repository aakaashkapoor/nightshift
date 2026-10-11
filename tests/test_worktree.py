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


def test_create_enables_git_longpaths(repo, tmp_path) -> None:
    WorktreeManager(repo, worktrees_root=tmp_path / "wts").create("slice-001")
    longpaths = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "core.longpaths"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert longpaths == "true"


def test_force_rmtree_deletes_nested_dir(tmp_path) -> None:
    from nightshift.worktree import _force_rmtree

    deep = tmp_path / "d" / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "f.txt").write_text("x", encoding="utf-8")
    _force_rmtree(tmp_path / "d")
    assert not (tmp_path / "d").exists()


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


def _failing_remove(wm, failures):
    """Make the first `failures` `git worktree remove` calls fail (None: always)."""
    real = wm._git
    state = {"removes": 0}

    def fake(*args):
        if args[:2] == ("worktree", "remove"):
            state["removes"] += 1
            if failures is None or state["removes"] <= failures:
                raise RuntimeError("Permission denied")
        return real(*args)

    wm._git = fake
    return state


def test_teardown_kills_holders_then_retries(repo, tmp_path) -> None:
    killed, slept = [], []
    wm = WorktreeManager(
        repo, worktrees_root=tmp_path / "wts", killer=killed.append, sleep=slept.append
    )
    wm.create("slice-001")
    state = _failing_remove(wm, 2)
    wm.teardown("slice-001")
    assert killed == [wm.path_for("slice-001")] * 2
    assert slept == [1, 1]
    assert state["removes"] == 3
    assert not wm.path_for("slice-001").exists()


def test_teardown_no_kill_when_remove_succeeds(repo, tmp_path) -> None:
    killed = []
    wm = WorktreeManager(repo, worktrees_root=tmp_path / "wts", killer=killed.append)
    wm.create("slice-001")
    wm.teardown("slice-001")
    assert killed == []


def test_teardown_falls_back_to_force_rmtree(repo, tmp_path, monkeypatch) -> None:
    killed, slept, forced = [], [], []
    wm = WorktreeManager(
        repo, worktrees_root=tmp_path / "wts", killer=killed.append, sleep=slept.append
    )
    wm.create("slice-001")
    state = _failing_remove(wm, None)
    monkeypatch.setattr("nightshift.worktree._force_rmtree", forced.append)
    wm.teardown("slice-001")
    assert len(killed) == 3 and slept == [1, 1, 1]
    assert state["removes"] == 4  # the first try plus three retries
    assert forced == [wm.path_for("slice-001")]


def _attributes(repo) -> list[str]:
    path = repo / ".git" / "info" / "attributes"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_registry_attribute_lines_written_once(repo, tmp_path) -> None:
    wm = WorktreeManager(
        repo, worktrees_root=tmp_path / "wts", registries=["src/index.ts", "src/routes.ts"]
    )
    wm.create("slice-001")
    wm.create("slice-002")
    assert _attributes(repo) == ["src/index.ts merge=union", "src/routes.ts merge=union"]


def test_registry_lines_append_to_existing_attributes(repo, tmp_path) -> None:
    attrs = repo / ".git" / "info" / "attributes"
    attrs.parent.mkdir(exist_ok=True)
    attrs.write_text("*.png binary", encoding="utf-8")  # no trailing newline
    WorktreeManager(repo, worktrees_root=tmp_path / "wts", registries=["src/index.ts"]).create(
        "slice-001"
    )
    assert _attributes(repo) == ["*.png binary", "src/index.ts merge=union"]


def test_no_registries_writes_no_attributes(wm, repo) -> None:
    wm.create("slice-001")
    assert _attributes(repo) == []


def _diverge(repo, wm) -> subprocess.CompletedProcess:
    """Two branches each append a different export to src/index.ts; rebase one on the other."""
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("export * from './base'\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    wt = wm.create("slice-001")
    index = wt.path / "src" / "index.ts"
    index.write_text(index.read_text(encoding="utf-8") + "export * from './a'\n", encoding="utf-8")
    _git(wt.path, "commit", "-am", "a")
    main_index = repo / "src" / "index.ts"
    main_index.write_text(
        main_index.read_text(encoding="utf-8") + "export * from './b'\n", encoding="utf-8"
    )
    _git(repo, "commit", "-am", "b")
    return subprocess.run(
        ["git", "-C", str(wt.path), "rebase", "main"], capture_output=True, text=True
    )


def test_registry_union_merge_rebases_cleanly(repo, tmp_path) -> None:
    wm = WorktreeManager(repo, worktrees_root=tmp_path / "wts", registries=["src/index.ts"])
    done = _diverge(repo, wm)
    assert done.returncode == 0, done.stdout + done.stderr
    text = (wm.path_for("slice-001") / "src" / "index.ts").read_text(encoding="utf-8")
    assert "./a" in text and "./b" in text and "<<<<" not in text


def test_without_registry_the_same_rebase_conflicts(repo, tmp_path) -> None:
    wm = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    assert _diverge(repo, wm).returncode != 0
