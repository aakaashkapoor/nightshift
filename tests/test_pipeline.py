"""End-to-end tests for run_slice — real worktree + real check + real commit.

The *agent* is faked (a runner that writes files into the worktree), so no live
Claude is needed, but everything else is real git.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nightshift.executor import Executor, RunOutput
from nightshift.pipeline import run_slice
from nightshift.slice import Slice
from nightshift.worktree import WorktreeManager

# Passes only if the agent created impl.txt in the worktree (same quoting pattern
# as test_check.py, verified to work on this Windows setup).
CHECK = f'"{sys.executable}" -c "import os,sys; sys.exit(0 if os.path.exists(\'impl.txt\') else 1)"'


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
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


def make_slice() -> Slice:
    return Slice.parse(
        "---\nid: slice-001\ntitle: Implement the thing\nstatus: ready\n---\n"
        "## Goal\nDo the thing.\n"
    )


class Agent:
    """Fake headless agent: writes impl.txt into the worktree on attempt >= succeed_on."""

    def __init__(self, succeed_on: int = 1):
        self.calls = 0
        self.succeed_on = succeed_on

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        self.calls += 1
        if self.calls >= self.succeed_on:
            Path(cwd, "impl.txt").write_text("done", encoding="utf-8")
        return RunOutput(
            0, json.dumps({"result": f"attempt {self.calls}", "session_id": f"s{self.calls}"}), ""
        )


def _run(repo, tmp_path, agent, **kw):
    check_cmd = kw.pop("check_cmd", CHECK)
    return run_slice(
        make_slice(),
        repo_path=repo,
        check_cmd=check_cmd,
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        executor=Executor(runner=agent),
        **kw,
    )


def test_success_makes_one_clean_commit_and_marks_done(repo, tmp_path) -> None:
    result = _run(repo, tmp_path, Agent(succeed_on=1))
    assert result.status == "done"
    assert result.attempts == 1
    assert result.commit is not None
    # Exactly ONE commit beyond main, and it contains the agent's file.
    count = _git(repo, "rev-list", "--count", "nightshift/slice-001", "^main")
    assert count == "1"
    files = _git(repo, "show", "--name-only", "--format=", "nightshift/slice-001")
    assert "impl.txt" in files


def test_retries_then_succeeds(repo, tmp_path) -> None:
    result = _run(repo, tmp_path, Agent(succeed_on=2), max_attempts=3)
    assert result.status == "done"
    assert result.attempts == 2


def test_blocks_after_max_attempts_and_preserves_worktree(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    result = run_slice(
        make_slice(),
        repo_path=repo,
        check_cmd=CHECK,
        worktrees=wts,
        executor=Executor(runner=Agent(succeed_on=99)),
        max_attempts=2,
    )
    assert result.status == "blocked"
    assert result.attempts == 2
    assert result.commit is None
    assert wts.exists("slice-001")  # preserved for inspection


def test_integrate_pushes_base_when_enabled(tmp_path) -> None:
    from nightshift.pipeline import integrate_branch

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(bare), str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@e.local")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "origin", "main")

    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    wt = wts.create("slice-001")
    (wt.path / "new.txt").write_text("x", encoding="utf-8")
    _git(wt.path, "add", "-A")
    _git(wt.path, "commit", "-m", "add new")

    result = integrate_branch(
        repo_path=repo,
        worktree_path=wt.path,
        branch=wt.branch,
        base_branch="main",
        check_cmd="echo ok",
        push=True,
    )
    assert result.merged
    origin_files = subprocess.run(
        ["git", "-C", str(bare), "ls-tree", "--name-only", "main"],
        capture_output=True,
        text=True,
    ).stdout
    assert "new.txt" in origin_files  # pushed to origin


def _repo_with_worktree(tmp_path):
    """A local repo (no remote) plus one worktree with an unmerged commit."""
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True
    )
    _git(repo, "config", "user.email", "t@e.local")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")

    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    wt = wts.create("slice-001")
    (wt.path / "new.txt").write_text("x", encoding="utf-8")
    _git(wt.path, "add", "-A")
    _git(wt.path, "commit", "-m", "add new")
    return repo, wt


def test_integrate_runs_sync_after_merge(tmp_path) -> None:
    """A worktree's own dependency install can leave the repo's symlinked dir
    (e.g. node_modules) stale even though the merge itself is clean — sync_cmd is
    what catches the repo back up, and it must run against the *repo*, post-merge,
    not the worktree."""
    from nightshift.pipeline import integrate_branch

    repo, wt = _repo_with_worktree(tmp_path)
    marker = repo / "synced.txt"
    sync_cmd = f"\"{sys.executable}\" -c \"open('synced.txt', 'w').write('x')\""

    result = integrate_branch(
        repo_path=repo,
        worktree_path=wt.path,
        branch=wt.branch,
        base_branch="main",
        check_cmd="echo ok",
        sync_cmd=sync_cmd,
    )
    assert result.merged
    assert marker.exists()  # ran in repo_path, after the merge


def test_integrate_survives_sync_failure(tmp_path) -> None:
    """A broken sync command must not undo an already-clean merge or crash the
    daemon — it's noted and left for a later slice's sync to self-heal."""
    from nightshift.pipeline import integrate_branch

    repo, wt = _repo_with_worktree(tmp_path)
    failing_sync = f'"{sys.executable}" -c "import sys; sys.exit(1)"'

    result = integrate_branch(
        repo_path=repo,
        worktree_path=wt.path,
        branch=wt.branch,
        base_branch="main",
        check_cmd="echo ok",
        sync_cmd=failing_sync,
    )
    assert result.merged
    assert "sync failed" in result.detail
    assert _git(repo, "log", "-1", "--format=%s") == "add new"  # merge still landed


def test_integrate_survives_push_failure(tmp_path) -> None:
    """A push failure (no remote here, but the same holds for a network blip or
    a stale pre-push hook) must not raise — that's what stranded a merged-but-
    unpushed commit with its worktree never torn down, observed in practice."""
    from nightshift.pipeline import integrate_branch

    repo, wt = _repo_with_worktree(tmp_path)  # no "origin" remote configured

    result = integrate_branch(
        repo_path=repo,
        worktree_path=wt.path,
        branch=wt.branch,
        base_branch="main",
        check_cmd="echo ok",
        push=True,
    )
    assert result.merged  # local merge still counts
    assert "push failed" in result.detail


def test_prepare_branch_rebases_and_checks_without_merging(tmp_path) -> None:
    from nightshift.pipeline import prepare_branch

    repo, wt = _repo_with_worktree(tmp_path)
    (repo / "other.txt").write_text("o", encoding="utf-8")  # base moves on
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "other")

    result = prepare_branch(worktree_path=wt.path, base_branch="main", check_cmd="echo ok")

    assert result.ok
    assert (wt.path / "other.txt").exists()  # rebased onto the new base
    assert "new.txt" not in _git(repo, "ls-tree", "--name-only", "main")  # not merged


def test_prepare_branch_reports_conflict(tmp_path) -> None:
    from nightshift.pipeline import prepare_branch

    repo, wt = _repo_with_worktree(tmp_path)
    (repo / "new.txt").write_text("clash", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "clash")

    result = prepare_branch(worktree_path=wt.path, base_branch="main", check_cmd="echo ok")

    assert not result.ok
    assert result.detail == "rebase conflict"


def _clone_with_origin(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(bare), str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@e.local")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "origin", "main")
    return bare, repo


def test_sync_base_pulls_merged_work_and_runs_sync(tmp_path) -> None:
    from nightshift.pipeline import sync_base

    bare, repo = _clone_with_origin(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "t@e.local")
    _git(other, "config", "user.name", "T")
    (other / "merged.txt").write_text("m", encoding="utf-8")  # "a PR merged on GitHub"
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "merged pr")
    _git(other, "push", "origin", "main")

    marker = repo / "synced.flag"
    detail = sync_base(repo, "main", sync_cmd=f"\"{sys.executable}\" -c \"open(r'{marker}', 'w')\"")

    assert (repo / "merged.txt").exists()
    assert marker.exists()
    assert detail == "pulled"


def test_sync_base_noop_when_up_to_date(tmp_path) -> None:
    from nightshift.pipeline import sync_base

    _bare, repo = _clone_with_origin(tmp_path)
    assert sync_base(repo, "main", sync_cmd="exit 1") == "up to date"  # sync not run


def test_sync_base_reports_pull_failure(tmp_path) -> None:
    from nightshift.pipeline import sync_base

    repo, _wt = _repo_with_worktree(tmp_path)  # no origin remote
    assert sync_base(repo, "main").startswith("pull failed")


def test_sync_base_survives_non_repo(tmp_path) -> None:
    from nightshift.pipeline import sync_base

    assert sync_base(tmp_path, "main").startswith("pull failed")


def test_sync_base_reports_sync_failure(tmp_path) -> None:
    from nightshift.pipeline import sync_base

    bare, repo = _clone_with_origin(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "t@e.local")
    _git(other, "config", "user.name", "T")
    (other / "m.txt").write_text("m", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "m")
    _git(other, "push", "origin", "main")

    assert sync_base(repo, "main", sync_cmd="exit 1").startswith("pulled; sync failed")


def test_sync_base_pulls_without_sync_cmd(tmp_path) -> None:
    from nightshift.pipeline import sync_base

    bare, repo = _clone_with_origin(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "t@e.local")
    _git(other, "config", "user.name", "T")
    (other / "m.txt").write_text("m", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "m")
    _git(other, "push", "origin", "main")

    assert sync_base(repo, "main") == "pulled"
    assert (repo / "m.txt").exists()


# ---- Task 3: result.json contract, check output on retry, no-change detection ----


def _result(cwd, **data) -> None:
    path = Path(cwd, ".nightshift", "result.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class Scripted:
    """Fake agent: step i runs on call i (the last step repeats)."""

    def __init__(self, *steps):
        self.steps = steps
        self.prompts: list[str] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        n = len(self.prompts)
        self.prompts.append(stdin)
        self.argvs.append(argv)
        self.steps[min(n, len(self.steps) - 1)](cwd, stdin)
        return RunOutput(0, json.dumps({"result": f"call {n}", "session_id": "sess"}), "")


def _done(*, decisions=(), write_file=True):
    def step(cwd, _stdin) -> None:
        if write_file:
            Path(cwd, "impl.txt").write_text("done", encoding="utf-8")
        _result(cwd, outcome="done", decisions=list(decisions))

    return step


def _nothing(_cwd, _stdin) -> None:
    pass


def _contract(repo, tmp_path, agent, **kw):
    return _run(repo, tmp_path, agent, result_contract=True, **kw)


def test_exclude_is_written_once_and_commit_omits_nightshift(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    wts.create("a")
    wts.create("b")
    exclude = repo / ".git" / "info" / "exclude"
    assert exclude.read_text(encoding="utf-8").splitlines().count(".nightshift/") == 1

    result = _contract(repo, tmp_path, Scripted(_done()))
    assert result.status == "done"
    files = _git(repo, "show", "--name-only", "--format=", "nightshift/slice-001")
    assert "impl.txt" in files and ".nightshift" not in files


def test_exclude_appends_to_existing_content(repo, tmp_path) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text("*.log", encoding="utf-8")  # no trailing newline
    WorktreeManager(repo, worktrees_root=tmp_path / "wts").create("a")
    assert exclude.read_text(encoding="utf-8").splitlines() == ["*.log", ".nightshift/"]


def test_exclude_works_from_a_linked_worktree(repo, tmp_path) -> None:
    # From inside a worktree git reports the common dir as an absolute path.
    first = WorktreeManager(repo, worktrees_root=tmp_path / "wts").create("a")
    WorktreeManager(first.path, worktrees_root=tmp_path / "wts2").create("b")
    exclude = repo / ".git" / "info" / "exclude"
    assert exclude.read_text(encoding="utf-8").splitlines().count(".nightshift/") == 1


def test_contract_done_is_green_with_decisions_in_commit(repo, tmp_path) -> None:
    result = _contract(repo, tmp_path, Scripted(_done(decisions=["used sqlite", "no cache"])))
    assert result.status == "done"
    assert result.decisions == ["used sqlite", "no cache"]
    msg = _git(repo, "log", "-1", "--format=%B", "nightshift/slice-001")
    assert "Decisions:\n- used sqlite\n- no cache" in msg


def test_contract_without_decisions_leaves_message_plain(repo, tmp_path) -> None:
    _contract(repo, tmp_path, Scripted(_done()))
    msg = _git(repo, "log", "-1", "--format=%B", "nightshift/slice-001")
    assert "Decisions" not in msg


def test_missing_result_triggers_one_repair_call(repo, tmp_path) -> None:
    def write_impl(cwd, _stdin) -> None:
        Path(cwd, "impl.txt").write_text("x", encoding="utf-8")

    agent = Scripted(write_impl, _done())
    result = _contract(repo, tmp_path, agent)
    assert result.status == "done"
    assert len(agent.prompts) == 2
    assert "missing or invalid: file not found" in agent.prompts[1]
    assert "--resume" in agent.argvs[1]


def test_still_missing_result_is_blocked(repo, tmp_path) -> None:
    agent = Scripted(_nothing)
    result = _contract(repo, tmp_path, agent)
    assert result.status == "blocked"
    assert "agent wrote no valid result.json: file not found" in result.detail
    assert len(agent.prompts) == 2


def test_usage_limit_during_repair_raises(repo, tmp_path) -> None:
    from nightshift.limits import UsageLimitReached

    class Limited:
        def __init__(self) -> None:
            self.n = 0

        def __call__(self, argv, cwd, stdin) -> RunOutput:
            self.n += 1
            if self.n == 1:
                return RunOutput(0, json.dumps({"result": "ok", "session_id": "s"}), "")
            msg = "You've hit your session limit · resets 2:30pm"
            return RunOutput(1, json.dumps({"result": msg, "is_error": True}), "")

    with pytest.raises(UsageLimitReached):
        _contract(repo, tmp_path, Limited())


def test_question_outcome_keeps_worktree(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")

    def ask(cwd, _stdin) -> None:
        _result(cwd, outcome="question", question="Which db?", decisions=["d"])

    result = run_slice(
        make_slice(),
        repo_path=repo,
        check_cmd=CHECK,
        worktrees=wts,
        executor=Executor(runner=Scripted(ask)),
        result_contract=True,
    )
    assert result.status == "question"
    assert result.question == "Which db?" and result.detail == "Which db?"
    assert result.decisions == ["d"] and result.commit is None
    assert wts.exists("slice-001")


def test_needs_files_outcome(repo, tmp_path) -> None:
    def need(cwd, _stdin) -> None:
        _result(cwd, outcome="needs_files", needs_files=["a.py", "b.py"])

    result = _contract(repo, tmp_path, Scripted(need))
    assert result.status == "needs_files"
    assert result.needs_files == ["a.py", "b.py"]
    assert result.detail == "needs a.py, b.py"


def test_red_check_retry_sends_check_output_on_same_session(repo, tmp_path) -> None:
    script = tmp_path / "flaky_check.py"
    marker = tmp_path / "seen"
    script.write_text(
        "import os, sys\n"
        f"m = r'{marker}'\n"
        "seen = os.path.exists(m)\n"
        "open(m, 'w').close()\n"
        "if not seen:\n"
        "    print('BOOM-FROM-CHECK')\n"
        "sys.exit(0 if seen else 1)\n",
        encoding="utf-8",
    )
    agent = Scripted(_done())
    result = _contract(repo, tmp_path, agent, check_cmd=f'"{sys.executable}" "{script}"')
    assert result.status == "done" and result.attempts == 2
    assert "BOOM-FROM-CHECK" in agent.prompts[1]
    assert "check failed" in agent.prompts[1]
    assert "--resume" in agent.argvs[1]


def test_red_check_retry_without_contract_sends_check_output(repo, tmp_path) -> None:
    agent = Agent(succeed_on=2)
    prompts: list[str] = []

    def spy(argv, cwd, stdin):
        prompts.append(stdin)
        return agent(argv, cwd, stdin)

    result = _run(repo, tmp_path, spy)
    assert result.status == "done" and result.attempts == 2
    assert "check failed" in prompts[1] and "Do the thing." not in prompts[1]


def test_no_changes_is_blocked_with_agent_note(repo, tmp_path) -> None:
    agent = Scripted(_done(write_file=False))
    result = _contract(repo, tmp_path, agent, check_cmd="echo ok")
    assert result.status == "blocked"
    assert result.detail.startswith("agent made no changes: call 0")


def test_no_changes_is_blocked_without_contract(repo, tmp_path) -> None:
    result = _run(repo, tmp_path, Scripted(_nothing), check_cmd="echo ok")
    assert result.status == "blocked"
    assert "agent made no changes" in result.detail
