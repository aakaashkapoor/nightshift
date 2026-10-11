"""Tests for the deterministic content gate: UTF-8 and conflict markers."""

import json
import subprocess
from pathlib import Path

import pytest

from nightshift.executor import Executor, RunOutput
from nightshift.gates import content_problems
from nightshift.pipeline import run_slice
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
    subprocess.run(["git", "init", "-b", "main", str(r)], check=True, capture_output=True)
    _git(r, "config", "user.email", "t@e.local")
    _git(r, "config", "user.name", "T")
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    # A marker already on main is not this change's problem.
    (r / "old.txt").write_bytes(b"<<<<<<< HEAD\nold\n=======\nold\n>>>>>>> x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


@pytest.fixture
def wt(repo, tmp_path):
    return WorktreeManager(repo, worktrees_root=tmp_path / "wts").create("slice-001").path


def test_cp1252_file_is_flagged_with_its_offset(wt) -> None:
    (wt / "x.txt").write_bytes(b"caf\xe9\n")
    assert content_problems(wt, "main") == ["x.txt: not UTF-8 (byte 0xE9 at offset 3)"]


def test_conflict_markers_are_flagged_with_their_line(wt) -> None:
    (wt / "x.txt").write_bytes(b"a\r\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> branch\n")
    assert content_problems(wt, "main") == [
        "x.txt:2: conflict marker",
        "x.txt:4: conflict marker",
        "x.txt:6: conflict marker",
    ]


def test_setext_heading_underline_is_not_a_marker(wt) -> None:
    (wt / "doc.md").write_bytes(b"Title\n=======\ntext\n")
    assert content_problems(wt, "main") == []


def test_bare_separator_counts_once_the_file_opens_a_conflict(wt) -> None:
    (wt / "doc.md").write_bytes(b"Title\n=======\n<<<<<<< HEAD\n")
    assert content_problems(wt, "main") == [
        "doc.md:2: conflict marker",
        "doc.md:3: conflict marker",
    ]


def test_a_non_utf8_file_with_markers_reports_both(wt) -> None:
    (wt / "x.txt").write_bytes(b"\xff\n<<<<<<< HEAD\n")
    assert content_problems(wt, "main") == [
        "x.txt: not UTF-8 (byte 0xFF at offset 0)",
        "x.txt:2: conflict marker",
    ]


def test_binary_and_clean_utf8_files_pass(wt) -> None:
    (wt / "img.bin").write_bytes(b"\x89PNG\x00\xff<<<<<<< not text")
    (wt / "ok.txt").write_text("café ✅\n= not a marker\n<<<<<< short\n", encoding="utf-8")
    assert content_problems(wt, "main") == []


def test_only_this_changes_files_are_checked(wt) -> None:
    (wt / "README.md").write_text("hi again\n", encoding="utf-8")
    assert content_problems(wt, "main") == []  # old.txt's markers are on main already


def test_committed_changes_count_too(wt) -> None:
    (wt / "x.txt").write_bytes(b"<<<<<<< HEAD\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "work")
    assert content_problems(wt, "main") == ["x.txt:1: conflict marker"]


def test_deleted_files_and_the_agents_own_dir_are_ignored(wt) -> None:
    (wt / "old.txt").unlink()
    (wt / ".nightshift").mkdir()
    (wt / ".nightshift" / "result.json").write_bytes(b"\xff")
    assert content_problems(wt, "main") == []


def test_a_path_that_is_not_a_file_is_skipped(wt, monkeypatch) -> None:
    from nightshift import gates

    (wt / "sub").mkdir()  # e.g. a submodule: git lists it, but it is a directory here
    monkeypatch.setattr(gates, "_changed_files", lambda root, base: ["sub"])
    assert content_problems(wt, "main") == []


# --- inside run_slice ---------------------------------------------------------------


def _slice() -> Slice:
    return Slice.parse(
        "---\nid: slice-001\ntitle: Implement the thing\nstatus: ready\n---\n## Goal\nGo.\n"
    )


class Writer:
    """Agent that writes the given bytes per attempt and records its prompts."""

    def __init__(self, *versions: bytes):
        self.versions = list(versions)
        self.prompts: list[str] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        self.prompts.append(stdin)
        self.argvs.append(argv)
        n = min(len(self.prompts), len(self.versions)) - 1
        Path(cwd, "out.txt").write_bytes(self.versions[n])
        return RunOutput(0, json.dumps({"result": "ok", "session_id": "s1"}), "")


def _run(repo, tmp_path, agent, **kw):
    return run_slice(
        _slice(),
        repo_path=repo,
        check_cmd="echo ok",
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        executor=Executor(runner=agent),
        **kw,
    )


def test_gate_failure_resumes_the_session_with_the_problems(repo, tmp_path) -> None:
    agent = Writer(b"caf\xe9\n", "café\n".encode())
    result = _run(repo, tmp_path, agent)

    assert result.status == "done" and result.attempts == 2
    assert "nightshift content gate" in agent.prompts[1]
    assert "out.txt: not UTF-8 (byte 0xE9 at offset 3)" in agent.prompts[1]
    assert "--resume" in agent.argvs[1]


def test_gate_failures_count_as_attempts_and_block(repo, tmp_path) -> None:
    agent = Writer(b"<<<<<<< HEAD\n")
    result = _run(repo, tmp_path, agent, max_attempts=2)
    assert result.status == "blocked" and result.attempts == 2
    assert "out.txt:1: conflict marker" in agent.prompts[1]


def test_clean_work_passes_the_gate_untouched(repo, tmp_path) -> None:
    agent = Writer(b"plain ascii\n")
    result = _run(repo, tmp_path, agent)
    assert result.status == "done" and result.attempts == 1
