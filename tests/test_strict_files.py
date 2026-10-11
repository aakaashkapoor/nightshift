"""Strict file list: edits outside Touches are added, or wait for the slice that owns them."""

import json
import logging
import subprocess
from pathlib import Path

import pytest

from nightshift.config import RepoConfig
from nightshift.daemon import Daemon
from nightshift.events import EventLog
from nightshift.executor import Executor, RunOutput
from nightshift.prompts import REQUEUE_PROMPT
from nightshift.runtime import Runtime
from nightshift.source import LocalMdSource
from nightshift.state import StateStore
from nightshift.worktree import WorktreeManager


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
    (r / "other.md").write_text("other\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    (r / ".slices").mkdir()
    return r


def _write_slice(repo, touches=None, sid="slice-001") -> None:
    block = ""
    if touches:
        block = "\n## Touches\n" + "".join(f"- {t}\n" for t in touches)
    (repo / ".slices" / f"{sid}.md").write_text(
        f"---\nid: {sid}\ntitle: {sid}\nstatus: ready\n---\n## Goal\ndo it\n{block}",
        encoding="utf-8",
    )


class EditsReadme:
    """Modifies the existing README.md (and adds a new file)."""

    def __init__(self, on_call=None):
        self.prompts: list[str] = []
        self.on_call = on_call

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        self.prompts.append(stdin)
        if self.on_call is not None:
            self.on_call()
        Path(cwd, "README.md").write_text(f"edited {len(self.prompts)}\n", encoding="utf-8")
        Path(cwd, "new.txt").write_text("new", encoding="utf-8")
        return RunOutput(0, json.dumps({"result": "ok", "session_id": "s"}), "")


class HidingSource(LocalMdSource):
    """`get` returns None on its first call (the issue vanished)."""

    hide_once = False

    def get(self, slice_id):
        if self.hide_once:
            self.hide_once = False
            return None
        return super().get(slice_id)


def _daemon(repo, tmp_path, agent, *, source=None, runtime=True) -> Daemon:
    return Daemon(
        source=source or LocalMdSource(repo),
        repo_cfg=RepoConfig(name="demo", path=repo, check="echo ok", base_branch="main"),
        executor=Executor(runner=agent),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        runtime=Runtime(tmp_path / "home" / "runtime.json") if runtime else None,
        events=EventLog(tmp_path / "home" / "events.jsonl"),
        state=StateStore(tmp_path / "home" / "state.json"),
    )


def _events(tmp_path) -> list[dict]:
    return EventLog(tmp_path / "home" / "events.jsonl").read_from(0)[0]


def test_no_touches_means_no_check(repo, tmp_path) -> None:
    _write_slice(repo)
    [r] = _daemon(repo, tmp_path, EditsReadme()).tick()
    assert r.status == "done"
    assert LocalMdSource(repo).get("slice-001").touches() == set()


def test_edits_inside_touches_ship(repo, tmp_path) -> None:
    _write_slice(repo, touches=["README.md"])
    [r] = _daemon(repo, tmp_path, EditsReadme()).tick()
    assert r.status == "done"
    assert LocalMdSource(repo).get("slice-001").touches() == {"README.md"}


def test_outside_but_free_is_added_to_touches_and_ships(repo, tmp_path, caplog) -> None:
    _write_slice(repo, touches=["other.md"])
    d = _daemon(repo, tmp_path, EditsReadme())
    with caplog.at_level(logging.INFO, logger="nightshift"):
        [r] = d.tick()
    assert r.status == "done"  # the slice's own hold was released before the check
    assert LocalMdSource(repo).get("slice-001").touches() == {"other.md", "README.md"}
    assert "slice-001 also modified README.md; added to Touches" in caplog.text
    assert (repo / "README.md").read_text(encoding="utf-8") == "edited 1\n"


def test_new_files_outside_touches_are_fine(repo, tmp_path) -> None:
    _write_slice(repo, touches=["README.md"])
    [r] = _daemon(repo, tmp_path, EditsReadme()).tick()
    assert r.status == "done" and (repo / "new.txt").exists()


def test_outside_and_held_requeues_until_the_holder_lands(repo, tmp_path) -> None:
    _write_slice(repo, touches=["other.md"])
    agent = EditsReadme()
    d = _daemon(repo, tmp_path, agent)
    d._held["slice-002"] = {"README.md"}  # another running slice owns README.md

    [r] = d.tick()
    assert r.status == "requeued"
    assert r.slice_id == "slice-001"
    sl = LocalMdSource(repo).get("slice-001")
    assert sl.status == "ready" and sl.touches() == {"other.md", "README.md"}
    [e] = [e for e in _events(tmp_path) if e["kind"] == "requeued"]
    assert e["files"] == ["README.md"] and e["holder"] == "slice-002"
    entry = Runtime(tmp_path / "home" / "runtime.json").get("slice-001")
    assert entry["interrupted"] is True and entry["session_id"] == "s"
    assert entry["resume_prompt"] == REQUEUE_PROMPT.format(files="README.md")
    snap = StateStore(tmp_path / "home" / "state.json").snapshot()
    assert snap["slices"]["slice-001"]["phase"] == "queued"
    assert (repo / "README.md").read_text(encoding="utf-8") == "hi\n"  # nothing shipped
    assert d.worktrees.exists("slice-001")

    d._held.clear()  # the holder has landed
    [r] = d.tick()
    assert r.status == "done"
    assert agent.prompts[1] == REQUEUE_PROMPT.format(files="README.md")
    assert Runtime(tmp_path / "home" / "runtime.json").get("slice-001") == {}


def test_requeue_without_runtime(repo, tmp_path) -> None:
    _write_slice(repo, touches=["other.md"])
    d = _daemon(repo, tmp_path, EditsReadme(), runtime=False)
    d._held["slice-002"] = {"README.md"}
    assert [r.status for r in d.tick()] == ["requeued"]


def test_vanished_issue_ships_unchecked(repo, tmp_path) -> None:
    _write_slice(repo, touches=["other.md"])
    src = HidingSource(repo)

    def vanish() -> None:
        src.hide_once = True  # the next read, after the agent ran, finds nothing

    d = _daemon(repo, tmp_path, EditsReadme(vanish), source=src)
    assert [r.status for r in d.tick()] == ["done"]
