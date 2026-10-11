"""Tests for the AI review step (parsing + the run_slice review gate)."""

import json
import subprocess
from pathlib import Path

import pytest

from nightshift.executor import Executor, RunOutput
from nightshift.limits import UsageLimitReached
from nightshift.pipeline import run_slice
from nightshift.review import AgentReviewer, Finding, ReviewResult, parse_findings
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


# --- the reviewer sees the diff, the issue and the plan -----------------------


class Recorder:
    def __init__(self, findings=()):
        self.prompts: list[str] = []
        self.findings = list(findings)

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        self.prompts.append(stdin)
        text = json.dumps({"findings": self.findings})
        return RunOutput(0, json.dumps({"result": text, "session_id": "r"}), "")


def _review_slice() -> Slice:
    return Slice.parse(
        "---\nid: slice-001\ntitle: Add greeter\nstatus: ready\n---\n"
        "## Goal\nGreet people politely.\n\n## Acceptance\n- says hello\n"
    )


def _worktree(repo, tmp_path, base="main"):
    return WorktreeManager(repo, worktrees_root=tmp_path / "wts").create("slice-001", base).path


def test_reviewer_prompt_has_issue_plan_and_diff_of_modified_and_new_files(repo, tmp_path) -> None:
    path = _worktree(repo, tmp_path)
    Path(path, "README.md").write_text("hi\nmodified line\n", encoding="utf-8")
    Path(path, "greeter.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    Path(path, ".nightshift").mkdir()
    Path(path, ".nightshift", "plan.md").write_text("PLAN: build greeter", encoding="utf-8")
    runner = Recorder()
    AgentReviewer(Executor(runner=runner)).review(path, _review_slice())
    prompt = runner.prompts[0]
    assert "Add greeter" in prompt and "Greet people politely." in prompt
    assert "- says hello" in prompt
    assert "PLAN: build greeter" in prompt
    assert "+modified line" in prompt  # tracked file
    assert "+++ b/greeter.py" in prompt and "+    return 'hello'" in prompt  # new file
    assert "b/.nightshift" not in prompt
    assert "path:line" in prompt


def test_reviewer_without_a_plan_says_none(repo, tmp_path) -> None:
    path = _worktree(repo, tmp_path)
    Path(path, "a.txt").write_text("a", encoding="utf-8")
    runner = Recorder()
    AgentReviewer(Executor(runner=runner)).review(path, _review_slice())
    assert "# Plan\n\n(none)\n" in runner.prompts[0]


def test_reviewer_diff_includes_already_committed_work(repo, tmp_path) -> None:
    path = _worktree(repo, tmp_path)
    Path(path, "first.txt").write_text("committed earlier", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "wip")
    Path(path, "second.txt").write_text("still loose", encoding="utf-8")
    runner = Recorder()
    AgentReviewer(Executor(runner=runner)).review(path, _review_slice())
    assert "committed earlier" in runner.prompts[0] and "still loose" in runner.prompts[0]


def test_reviewer_truncates_a_huge_diff(repo, tmp_path) -> None:
    path = _worktree(repo, tmp_path)
    Path(path, "big.txt").write_text("x" * 5000, encoding="utf-8")
    runner = Recorder()
    AgentReviewer(Executor(runner=runner), max_diff=200).review(path, _review_slice())
    prompt = runner.prompts[0]
    assert prompt.endswith("\n[diff truncated]\n")
    assert "x" * 300 not in prompt


def test_reviewer_uses_the_given_base_branch(repo, tmp_path) -> None:
    _git(repo, "branch", "trunk")
    path = _worktree(repo, tmp_path, base="trunk")
    Path(path, "t.txt").write_text("on trunk", encoding="utf-8")
    runner = Recorder()
    AgentReviewer(Executor(runner=runner), base_branch="trunk").review(path, _review_slice())
    assert "on trunk" in runner.prompts[0]


def test_blocking_without_a_location_is_downgraded_to_advisory(repo, tmp_path) -> None:
    path = _worktree(repo, tmp_path)
    Path(path, "a.txt").write_text("a", encoding="utf-8")
    win = "bad windows path src\\greeter.py:3"
    runner = Recorder(
        [
            {"severity": "blocking", "message": "wrong result at src/greeter.py:12"},
            {"severity": "blocking", "message": win},
            {"severity": "blocking", "message": "this feels off"},
            {"severity": "blocking", "message": "see greeter.py without a line"},
            {"severity": "advisory", "message": "nit"},
        ]
    )
    result = AgentReviewer(Executor(runner=runner)).review(path, _review_slice())
    assert [f.message for f in result.blocking] == ["wrong result at src/greeter.py:12", win]
    assert [f.message for f in result.advisory] == [
        "this feels off",
        "see greeter.py without a line",
        "nit",
    ]


def test_reviewer_raises_on_usage_limit_from_the_agent(repo, tmp_path) -> None:
    path = _worktree(repo, tmp_path)
    msg = "You've hit your session limit · resets 2:30pm"

    def limited(argv, cwd, stdin) -> RunOutput:
        return RunOutput(1, json.dumps({"result": msg, "is_error": True, "session_id": "l"}), "")

    with pytest.raises(UsageLimitReached):
        AgentReviewer(Executor(runner=limited)).review(path, _review_slice())


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
