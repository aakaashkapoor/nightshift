"""Tests for the conflict ladder: lockfile regeneration, the resolver, per-file counts."""

import json
import subprocess
import sys

import pytest

from nightshift import conflicts
from nightshift.conflicts import ConflictLog, conflicted_files, regenerate_lockfiles
from nightshift.pipeline import prepare_branch
from nightshift.state import render_status
from nightshift.worktree import WorktreeManager

LOCK = "package-lock.json"
REGEN = f"\"{sys.executable}\" -c \"open('package-lock.json','w').write('[]')\""
BROKEN = f'"{sys.executable}" -c "import sys; sys.exit(1)"'


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(cwd, files: dict[str, str], msg: str) -> None:
    for name, text in files.items():
        (cwd / name).write_text(text, encoding="utf-8")
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-m", msg)


def _setup(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@e.local")
    _git(repo, "config", "user.name", "T")
    _commit(repo, {LOCK: "base\n", "app.txt": "a\n"}, "init")
    wt = WorktreeManager(repo, worktrees_root=tmp_path / "wts").create("slice-001")
    return repo, wt


@pytest.fixture
def fake_lock(monkeypatch):
    monkeypatch.setattr(conflicts, "LOCKFILES", {LOCK: REGEN})


def test_lockfile_conflict_is_regenerated_without_the_resolver(tmp_path, fake_lock) -> None:
    repo, wt = _setup(tmp_path)
    _commit(wt.path, {LOCK: "branch\n"}, "branch lock")
    _commit(wt.path, {LOCK: "branch2\n"}, "branch lock again")  # a second stop
    _commit(repo, {LOCK: "main\n"}, "main lock")
    seen = []

    result = prepare_branch(
        worktree_path=wt.path,
        base_branch="main",
        check_cmd="echo ok",
        on_conflict=lambda files, how: seen.append((files, how)),
    )

    assert result.ok
    assert (wt.path / LOCK).read_text(encoding="utf-8") == "[]"
    assert seen == [([LOCK], "lockfile"), ([LOCK], "lockfile")]  # stopped twice
    assert conflicted_files(wt.path) == []


def test_mixed_conflict_gives_the_resolver_only_the_code_file(tmp_path, fake_lock) -> None:
    repo, wt = _setup(tmp_path)
    _commit(wt.path, {LOCK: "branch\n", "app.txt": "branch\n"}, "branch")
    _commit(repo, {LOCK: "main\n", "app.txt": "main\n"}, "main")
    seen, resolver_saw = [], []

    def resolver(path):
        resolver_saw.append(conflicted_files(path))
        (wt.path / "app.txt").write_text("merged\n", encoding="utf-8")

    result = prepare_branch(
        worktree_path=wt.path,
        base_branch="main",
        check_cmd="echo ok",
        resolver=resolver,
        on_conflict=lambda files, how: seen.append((files, how)),
    )

    assert result.ok
    assert resolver_saw == [["app.txt"]]
    assert seen == [([LOCK], "lockfile"), (["app.txt"], "agent")]
    assert (wt.path / LOCK).read_text(encoding="utf-8") == "[]"


def test_unresolved_code_conflict_is_reported_and_aborted(tmp_path, fake_lock) -> None:
    repo, wt = _setup(tmp_path)
    _commit(wt.path, {"app.txt": "branch\n"}, "branch")
    _commit(repo, {"app.txt": "main\n"}, "main")
    seen = []

    result = prepare_branch(
        worktree_path=wt.path,
        base_branch="main",
        check_cmd="echo ok",
        resolver=lambda p: None,
        on_conflict=lambda files, how: seen.append((files, how)),
    )

    assert not result.ok and result.detail == "unresolved conflict"
    assert seen == [(["app.txt"], "unresolved")]
    assert conflicted_files(wt.path) == []  # rebase aborted


def test_conflict_without_a_resolver_is_still_reported(tmp_path) -> None:
    repo, wt = _setup(tmp_path)
    _commit(wt.path, {"app.txt": "branch\n"}, "branch")
    _commit(repo, {"app.txt": "main\n"}, "main")
    seen = []
    result = prepare_branch(
        worktree_path=wt.path,
        base_branch="main",
        check_cmd="echo ok",
        on_conflict=lambda files, how: seen.append((files, how)),
    )
    assert result.detail == "rebase conflict"
    assert seen == [(["app.txt"], "unresolved")]


def test_failed_regeneration_fixes_nothing(tmp_path, monkeypatch) -> None:
    repo, wt = _setup(tmp_path)
    monkeypatch.setattr(conflicts, "LOCKFILES", {LOCK: BROKEN})
    _commit(wt.path, {LOCK: "branch\n"}, "branch")
    _commit(repo, {LOCK: "main\n"}, "main")
    assert regenerate_lockfiles(wt.path, [LOCK, "other.txt"]) == []


def test_stop_with_no_conflicted_files_gives_up(tmp_path, monkeypatch) -> None:
    repo, wt = _setup(tmp_path)
    _commit(wt.path, {"app.txt": "branch\n"}, "branch")
    _commit(repo, {"app.txt": "main\n"}, "main")
    import nightshift.pipeline as pipeline

    monkeypatch.setattr(pipeline, "conflicted_files", lambda path: [])
    called = []
    result = prepare_branch(
        worktree_path=wt.path,
        base_branch="main",
        check_cmd="echo ok",
        resolver=lambda p: called.append(p),
    )
    assert not result.ok
    assert called == []
    assert conflicted_files(wt.path) == []


def test_conflict_log_counts_persist(tmp_path) -> None:
    log = ConflictLog(tmp_path / "home" / "conflicts.json")
    assert log.counts() == {}
    log.record(["a.ts", "b.ts"], "agent")
    log.record(["a.ts"], "lockfile")
    assert ConflictLog(tmp_path / "home" / "conflicts.json").counts() == {"a.ts": 2, "b.ts": 1}
    assert json.loads((tmp_path / "home" / "conflicts.json").read_text("utf-8"))["a.ts"] == 2


def test_conflict_log_ignores_a_corrupt_file(tmp_path) -> None:
    path = tmp_path / "conflicts.json"
    path.write_text("{nope", encoding="utf-8")
    assert ConflictLog(path).counts() == {}


def test_render_status_lists_top_conflicts() -> None:
    snap = {"daemon": {"status": "running", "conflicts": {"a.ts": 3, "b.ts": 1}}, "slices": {}}
    assert "conflicts: a.ts x3, b.ts x1" in render_status(snap, now=0)
    assert "conflicts:" not in render_status({"daemon": {"status": "running"}, "slices": {}}, 0)


def test_render_status_shows_conflicts_with_slices() -> None:
    snap = {
        "daemon": {"status": "running", "conflicts": {"a.ts": 2}},
        "slices": {"issue-1": {"phase": "working", "since": 0}},
    }
    assert "conflicts: a.ts x2" in render_status(snap, now=5)
