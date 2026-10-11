"""Nightshift rebases its own conflicting PRs when main moves (real git, bare origin)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nightshift.config import RepoConfig
from nightshift.daemon import Daemon
from nightshift.events import EventLog
from nightshift.executor import Executor
from nightshift.source import LocalMdSource
from nightshift.worktree import WorktreeManager

BRANCH = "nightshift/slice-001"


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(cwd, name, text, msg) -> None:
    Path(cwd, name).write_text(text, encoding="utf-8")
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-m", msg)


class StripResolver:
    def __call__(self, worktree_path) -> None:
        readme = Path(worktree_path, "README.md")
        kept = [
            line
            for line in readme.read_text(encoding="utf-8").splitlines()
            if not line.startswith(("<<<<<<<", "=======", ">>>>>>>"))
        ]
        readme.write_text("\n".join(kept) + "\n", encoding="utf-8")


class ViewPR:
    def __init__(self, state="OPEN", mergeable="CONFLICTING"):
        self.value = (state, mergeable)
        self.views = 0

    def view(self, branch):
        self.views += 1
        return self.value


@pytest.fixture
def world(tmp_path):
    """repo with a bare origin; slice-001 is in review with its branch pushed to origin."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@e.local")
    _git(repo, "config", "user.name", "T")
    _commit(repo, "README.md", "base\n", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "main")
    wm = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    wt = wm.create("slice-001")
    _commit(wt.path, "README.md", "base\nbranch\n", "slice work")
    _git(wt.path, "push", "origin", BRANCH)
    wm.teardown("slice-001")
    (repo / ".slices").mkdir()
    (repo / ".slices" / "slice-001.md").write_text(
        "---\nid: slice-001\ntitle: s\nstatus: in-review\n---\n## Goal\nx\n", encoding="utf-8"
    )
    return repo, origin, tmp_path


def _daemon(world, pr, resolver=None, install=None):
    repo, _origin, tmp_path = world
    return Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="r", path=repo, check="echo ok", babysit=True, install=install),
        executor=Executor(runner=lambda *a: None),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        pr=pr,
        resolver=resolver,
        events=EventLog(tmp_path / "events.jsonl"),
    )


def _events(world, kind) -> list[dict]:
    path = world[2] / "events.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["kind"] == kind]


def _main_moves(world) -> None:
    repo = world[0]
    _commit(repo, "README.md", "base\nmain\n", "main moved")
    _git(repo, "push", "origin", "main")


def test_conflicting_pr_is_rebased_and_force_pushed(world) -> None:
    _repo, origin, _ = world
    _main_moves(world)
    d = _daemon(world, ViewPR(), resolver=StripResolver())

    d.tick()

    _git(origin, "merge-base", "--is-ancestor", "main", BRANCH)  # raises unless it is
    content = _git(origin, "show", f"{BRANCH}:README.md")
    assert "branch" in content and "main" in content
    assert [e["slice"] for e in _events(world, "pr_rebased")] == ["slice-001"]
    assert not d.worktrees.exists("slice-001")  # torn down again
    assert d.source.get("slice-001").status == "in-review"


def test_rebase_runs_the_install_command(world) -> None:
    _main_moves(world)
    marker = world[2] / "installed.txt"
    cmd = f"\"{sys.executable}\" -c \"open(r'{marker}','w').write('x')\""
    _daemon(world, ViewPR(), resolver=StripResolver(), install=cmd).tick()
    assert marker.exists()


def test_failed_install_reports_blocked(world) -> None:
    _main_moves(world)
    _daemon(world, ViewPR(), resolver=StripResolver(), install="exit 3").tick()
    reason = _events(world, "blocked")[0]["reason"]
    assert reason.startswith("PR conflicts with main and could not be rebased: install failed")


def test_unresolvable_conflict_blocks_once_per_base_commit(world) -> None:
    repo, origin, _ = world
    before = _git(origin, "rev-parse", BRANCH)
    _main_moves(world)
    d = _daemon(world, ViewPR())  # no resolver

    d.tick()
    d.tick()  # same base commit: no second attempt

    blocked = _events(world, "blocked")
    assert len(blocked) == 1 and blocked[0]["slice"] == "slice-001"
    assert blocked[0]["reason"] == (
        "PR conflicts with main and could not be rebased: rebase conflict"
    )
    assert _git(origin, "rev-parse", BRANCH) == before  # PR untouched
    assert d.source.get("slice-001").status == "in-review"
    assert not d.worktrees.exists("slice-001")

    _commit(repo, "other.txt", "x\n", "main moved again")
    _git(repo, "push", "origin", "main")
    d.tick()  # main moved: try again
    assert len(_events(world, "blocked")) == 2


def test_missing_remote_branch_is_reported_not_raised(world) -> None:
    _git(world[1], "branch", "-D", BRANCH)
    _daemon(world, ViewPR()).tick()
    assert "could not be rebased" in _events(world, "blocked")[0]["reason"]


def test_mergeable_prs_are_left_alone_with_one_gh_call(world) -> None:
    _main_moves(world)
    pr = ViewPR(mergeable="MERGEABLE")
    _daemon(world, pr, resolver=StripResolver()).tick()
    assert pr.views == 1
    assert _events(world, "pr_rebased") == [] and _events(world, "blocked") == []
