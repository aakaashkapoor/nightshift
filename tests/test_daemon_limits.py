"""Usage-limit pause and resume: pipeline and daemon."""

import json
import subprocess
from pathlib import Path

import pytest

from nightshift.executor import Executor, RunOutput
from nightshift.limits import UsageLimitReached
from nightshift.pipeline import CONTINUE_PROMPT, run_slice
from nightshift.slice import Slice
from nightshift.worktree import WorktreeManager

LIMIT = RunOutput(
    0,
    json.dumps(
        {
            "is_error": True,
            "result": "You've hit your session limit · resets 2:30pm",
            "session_id": "lim",
        }
    ),
    "",
)


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
    (r / ".slices").mkdir()
    return r


def _slice(sid="slice-001") -> Slice:
    return Slice.parse(f"---\nid: {sid}\ntitle: {sid}\nstatus: ready\n---\n## Goal\ndo it\n")


class Script:
    """Fake agent: returns queued outputs; writes a file on non-limit calls."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.prompts: list[str] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        self.prompts.append(stdin)
        self.argvs.append(argv)
        out = self.outputs.pop(0)
        if out is not LIMIT:
            Path(cwd, "work.txt").write_text("x", encoding="utf-8")
        return out


def _ok(session="s1") -> RunOutput:
    return RunOutput(0, json.dumps({"result": "ok", "session_id": session}), "")


def test_run_slice_raises_on_usage_limit(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    with pytest.raises(UsageLimitReached) as info:
        run_slice(
            _slice(),
            repo_path=repo,
            check_cmd="echo ok",
            worktrees=wts,
            executor=Executor(runner=Script(LIMIT)),
        )
    assert info.value.session_id == "lim"
    assert wts.exists("slice-001")  # worktree kept for the resume


def test_run_slice_resumes_with_continue_prompt(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    with pytest.raises(UsageLimitReached):
        run_slice(
            _slice(),
            repo_path=repo,
            check_cmd="echo ok",
            worktrees=wts,
            executor=Executor(runner=Script(LIMIT)),
        )
    agent = Script(_ok("s2"))
    result = run_slice(
        _slice(),
        repo_path=repo,
        check_cmd="echo ok",
        worktrees=wts,
        executor=Executor(runner=agent),
        resume_session="lim",
    )
    assert result.status == "done"
    assert agent.prompts == [CONTINUE_PROMPT]
    assert "--resume" in agent.argvs[0] and "lim" in agent.argvs[0]
    assert result.session_id == "s2"


def test_resume_without_worktree_starts_fresh(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    agent = Script(_ok())
    result = run_slice(
        _slice(),
        repo_path=repo,
        check_cmd="echo ok",
        worktrees=wts,
        executor=Executor(runner=agent),
        resume_session="gone",
    )
    assert result.status == "done"
    assert agent.prompts[0] != CONTINUE_PROMPT
    assert "--resume" not in agent.argvs[0]


def test_resume_after_commit_does_not_fail_on_empty_commit(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    first = run_slice(
        _slice(),
        repo_path=repo,
        check_cmd="echo ok",
        worktrees=wts,
        executor=Executor(runner=Script(_ok())),
    )
    assert first.status == "done"

    class NoEdit:
        def __call__(self, argv, cwd, stdin) -> RunOutput:
            return _ok("s3")

    again = run_slice(
        _slice(),
        repo_path=repo,
        check_cmd="echo ok",
        worktrees=wts,
        executor=Executor(runner=NoEdit()),
        resume_session="s1",
    )
    assert again.status == "done"
    assert again.commit == first.commit


def test_limit_during_review_fix_raises(repo, tmp_path) -> None:
    from nightshift.review import Finding, ReviewResult

    class OneBlocking:
        def review(self, path, sl):
            return ReviewResult([Finding("blocking", "bug")])

    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    with pytest.raises(UsageLimitReached):
        run_slice(
            _slice(),
            repo_path=repo,
            check_cmd="echo ok",
            worktrees=wts,
            executor=Executor(runner=Script(_ok(), LIMIT)),
            reviewer=OneBlocking(),
        )
