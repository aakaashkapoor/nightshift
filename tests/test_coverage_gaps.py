"""Targeted tests for pure-logic branches, to reach 100% coverage."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nightshift.check import _as_text
from nightshift.config import _interpolate
from nightshift.executor import Executor, RunOutput
from nightshift.github import GitHubIssuesSource
from nightshift.pipeline import _commit_if_changes, _git
from nightshift.resolver import Resolver
from nightshift.review import AgentReviewer
from nightshift.runtime import Runtime
from nightshift.scheduler import Dag
from nightshift.slice import Slice
from nightshift.source import LocalMdSource
from nightshift.worktree import WorktreeManager

# --- small helpers ------------------------------------------------------------


def _gitr(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(r)], check=True, capture_output=True)
    _gitr(r, "config", "user.email", "t@e.local")
    _gitr(r, "config", "user.name", "T")
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _gitr(r, "add", "-A")
    _gitr(r, "commit", "-m", "init")
    return r


class FakeExec:
    def __init__(self, result_text="{}"):
        self.result_text = result_text
        self.calls = []

    def execute_prompt(self, cwd, prompt, resume_session=None):
        self.calls.append((cwd, prompt))
        return SimpleNamespace(session_id="s", result_text=self.result_text)


# --- check --------------------------------------------------------------------


def test_as_text_handles_none_bytes_str() -> None:
    assert _as_text(None) == ""
    assert _as_text(b"abc") == "abc"
    assert _as_text("xyz") == "xyz"


# --- slice --------------------------------------------------------------------


def test_slice_unterminated_frontmatter_raises() -> None:
    with pytest.raises(ValueError, match="not terminated"):
        Slice.parse("---\nid: x\ntitle: t\nstatus: ready\n## Goal\nno closing fence\n")


# --- config -------------------------------------------------------------------


def test_interpolate_list(monkeypatch) -> None:
    monkeypatch.setenv("TOK", "secret")
    assert _interpolate(["${TOK}", "plain"]) == ["secret", "plain"]


# --- executor -----------------------------------------------------------------


def test_parse_non_dict_json_yields_no_session() -> None:
    result = Executor()._parse(RunOutput(0, "[1, 2, 3]", ""))
    assert result.session_id is None
    assert result.result_text == "[1, 2, 3]"


# --- resolver / review agents -------------------------------------------------


def test_resolver_invokes_executor() -> None:
    ex = FakeExec()
    Resolver(ex)("/some/worktree")
    assert ex.calls and ex.calls[0][0] == "/some/worktree"


def test_agent_reviewer_parses_findings() -> None:
    ex = FakeExec(json.dumps({"findings": [{"severity": "blocking", "message": "b"}]}))
    result = AgentReviewer(ex).review("/wt", None)
    assert len(result.blocking) == 1


# --- runtime ------------------------------------------------------------------


def test_runtime_ignores_corrupt_file(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text("{ not valid json", encoding="utf-8")
    rt = Runtime(path)
    assert rt.get("anything") == {}


def test_runtime_forget_missing_is_noop(tmp_path) -> None:
    Runtime(tmp_path / "runtime.json").forget("nope")  # must not raise or write


# --- source -------------------------------------------------------------------


def test_source_get_missing_and_errors(tmp_path) -> None:
    (tmp_path / ".slices").mkdir()
    src = LocalMdSource(tmp_path)
    assert src.get("nope") is None
    with pytest.raises(KeyError):
        src.set_status("nope", "done")
    with pytest.raises(KeyError):
        src.set_blocked("nope", "reason")


def test_source_set_blocked_without_reason(tmp_path) -> None:
    d = tmp_path / ".slices"
    d.mkdir()
    (d / "s.md").write_text(
        "---\nid: s\ntitle: t\nstatus: ready\n---\n## Goal\ng\n", encoding="utf-8"
    )
    src = LocalMdSource(tmp_path)
    src.set_blocked("s", "")  # empty reason -> no "## Blocked" appended
    assert "## Blocked" not in src.get("s").body


def test_source_skips_unparseable_md(tmp_path) -> None:
    d = tmp_path / ".slices"
    d.mkdir()
    (d / "note.md").write_text("just a note, no frontmatter\n", encoding="utf-8")
    assert LocalMdSource(tmp_path).list_all() == []


# --- worktree -----------------------------------------------------------------


def test_worktree_default_root(tmp_path) -> None:
    wm = WorktreeManager(tmp_path / "repo")
    assert wm.root.name == "repo"
    assert ".nightshift-worktrees" in str(wm.root)


def test_worktree_attach_missing_raises(tmp_path) -> None:
    wm = WorktreeManager(tmp_path / "repo", worktrees_root=tmp_path / "wts")
    with pytest.raises(FileNotFoundError):
        wm.attach("ghost")


def test_worktree_list_slices_empty_when_no_root(tmp_path) -> None:
    assert (
        WorktreeManager(tmp_path / "repo", worktrees_root=tmp_path / "missing").list_slices() == []
    )


# --- scheduler ----------------------------------------------------------------


def test_topo_order_of_a_chain() -> None:
    def S(sid, deps=None):
        return Slice(id=sid, title=sid, status="ready", depends_on=list(deps or []))

    dag = Dag.build([S("c", ["b"]), S("b", ["a"]), S("a")])
    order = dag.topo_order()
    assert order.index("a") < order.index("b") < order.index("c")


# --- github -------------------------------------------------------------------


def test_github_get_and_blocked_without_reason() -> None:
    issues = [
        {
            "number": 1,
            "title": "T",
            "body": "## Goal\ng",
            "labels": [{"name": "nightshift:ready"}],
            "state": "OPEN",
        },
    ]
    calls = []

    def gh(*args):
        calls.append(args)
        return json.dumps(issues) if args[:2] == ("issue", "list") else ""

    src = GitHubIssuesSource(".", gh=gh)
    assert src.get("issue-1").title == "T"
    assert src.get("issue-99") is None

    src.set_blocked("issue-1", "")  # no reason -> label edit only, no comment
    assert not any(c[0:2] == ("issue", "comment") for c in calls)


# --- pipeline -----------------------------------------------------------------


def test_git_raises_on_failure(tmp_path) -> None:
    with pytest.raises(RuntimeError):
        _git(tmp_path, "rev-parse", "HEAD")  # not a git repo


def test_commit_if_changes_noop_when_clean(tmp_path) -> None:
    repo = _repo(tmp_path)
    head = _gitr(repo, "rev-parse", "HEAD").stdout.strip()
    assert _commit_if_changes(repo, "nothing to do") == head  # no new commit


import sys  # noqa: E402

from nightshift.config import RepoConfig  # noqa: E402
from nightshift.daemon import Daemon  # noqa: E402
from nightshift.pipeline import run_resume_cli, run_slice  # noqa: E402
from nightshift.review import Finding, ReviewResult  # noqa: E402

_CHECK_IMPL = (
    f'"{sys.executable}" -c "import os,sys; sys.exit(0 if os.path.exists(\'impl.txt\') else 1)"'
)


def _ok(sid="s"):
    return RunOutput(0, json.dumps({"result": "ok", "session_id": sid}), "")


def test_review_fix_that_breaks_check_blocks(tmp_path) -> None:
    repo = _repo(tmp_path)

    class BreakAgent:
        def __call__(self, argv, cwd, stdin):
            impl = Path(cwd, "impl.txt")
            if "Code review found" in stdin:  # the fix round deletes the work
                if impl.exists():
                    impl.unlink()
            else:
                impl.write_text("x", encoding="utf-8")
            return _ok()

    class AlwaysBlock:
        def review(self, wt, sl):
            return ReviewResult([Finding("blocking", "x")])

    sl = Slice.parse("---\nid: s\ntitle: t\nstatus: ready\n---\n## Goal\ng\n")
    result = run_slice(
        sl,
        repo_path=repo,
        check_cmd=_CHECK_IMPL,
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        executor=Executor(runner=BreakAgent()),
        reviewer=AlwaysBlock(),
    )
    assert result.status == "blocked"
    assert "review fix broke" in result.detail


def test_daemon_run_one_catches_exceptions(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / ".slices").mkdir()
    (repo / ".slices" / "s.md").write_text(
        "---\nid: s\ntitle: t\nstatus: ready\n---\n## Goal\ng\n", encoding="utf-8"
    )

    class BoomAgent:
        def __call__(self, argv, cwd, stdin):
            raise RuntimeError("boom")

    results = Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="d", path=repo, check="echo ok", base_branch="main"),
        executor=Executor(runner=BoomAgent()),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
    ).tick()
    assert results[0].status == "blocked"
    assert "error:" in results[0].detail


def test_resume_cli_stays_blocked(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / ".slices").mkdir()
    (repo / ".slices" / "s.md").write_text(
        "---\nid: s\ntitle: t\nstatus: ready\n---\n## Goal\ng\n", encoding="utf-8"
    )
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {"repos": {"d": {"path": str(repo), "check": _CHECK_IMPL, "base_branch": "main"}}}
        ),
        encoding="utf-8",
    )

    class FailAgent:
        def __call__(self, argv, cwd, stdin):
            return _ok("s1")

    Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="d", path=repo, check=_CHECK_IMPL, base_branch="main"),
        executor=Executor(runner=FailAgent()),
        worktrees=WorktreeManager(repo),
        max_attempts=1,
    ).tick()

    result = run_resume_cli(
        "s",
        repo=repo,
        config_path=cfg,
        executor=Executor(runner=FailAgent()),
        runtime_path=tmp_path / "rt.json",
    )
    assert result.status == "blocked"


def test_review_result_advisory_property() -> None:
    rr = ReviewResult([Finding("advisory", "nit"), Finding("blocking", "bug")])
    assert [f.message for f in rr.advisory] == ["nit"]


def test_topo_order_diamond_hits_multi_parent_branch() -> None:
    def S(sid, deps=None):
        return Slice(id=sid, title=sid, status="ready", depends_on=list(deps or []))

    dag = Dag.build([S("a"), S("b", ["a"]), S("c", ["a"]), S("d", ["b", "c"])])
    order = dag.topo_order()
    assert order.index("d") == 3


def test_github_parse_depends_skips_empty_tokens() -> None:
    issues = [
        {
            "number": 5,
            "title": "T",
            "body": "## Goal\ng\n\nDepends-on: #1, , #2",
            "labels": [{"name": "nightshift:ready"}],
            "state": "OPEN",
        },
    ]
    src = GitHubIssuesSource(".", gh=lambda *a: json.dumps(issues))
    assert src.get("issue-5").depends_on == ["issue-1", "issue-2"]


def test_github_skips_issue_with_only_non_nightshift_labels() -> None:
    # A non-matching label exercises the loop's continue branch; no status -> skipped.
    issues = [
        {"number": 7, "title": "T", "body": "b", "labels": [{"name": "bug"}], "state": "OPEN"}
    ]
    src = GitHubIssuesSource(".", gh=lambda *a: json.dumps(issues))
    assert src.list_all() == []


def test_worktree_git_failure_raises(tmp_path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    wm = WorktreeManager(not_a_repo, worktrees_root=tmp_path / "wts")
    with pytest.raises(RuntimeError):
        wm.create("x")


def _daemon_over(repo, tmp_path, agent, runtime=None):
    return Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="d", path=repo, check="echo ok", base_branch="main"),
        executor=Executor(runner=agent),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        runtime=runtime,
    )


def test_tick_empty_source_returns_nothing(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / ".slices").mkdir()
    assert _daemon_over(repo, tmp_path, _AgentWrite()).tick() == []


def test_tick_no_runnable_returns_nothing(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / ".slices").mkdir()
    (repo / ".slices" / "s.md").write_text(
        "---\nid: s\ntitle: t\nstatus: done\n---\n## Goal\ng\n", encoding="utf-8"
    )
    assert _daemon_over(repo, tmp_path, _AgentWrite()).tick() == []


def test_tick_runtime_forget_on_done(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / ".slices").mkdir()
    (repo / ".slices" / "s.md").write_text(
        "---\nid: s\ntitle: t\nstatus: ready\n---\n## Goal\ng\n", encoding="utf-8"
    )
    rt = Runtime(tmp_path / "rt.json")
    rt.record("s", session_id="old")
    _daemon_over(repo, tmp_path, _AgentWrite(), runtime=rt).tick()
    assert rt.get("s") == {}  # forgotten after merge


def test_run_forever_zero_ticks(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / ".slices").mkdir()
    calls = []
    d = _daemon_over(repo, tmp_path, _AgentWrite())
    d.tick = lambda: calls.append(1) or []
    d.run_forever(max_ticks=0, sleep=lambda _s: None)
    assert calls == []


def test_run_resume_cli_missing_slice_raises(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / ".slices").mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {"repos": {"d": {"path": str(repo), "check": "echo ok", "base_branch": "main"}}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError):
        run_resume_cli("ghost", repo=repo, config_path=cfg, runtime_path=tmp_path / "rt.json")


class _AgentWrite:
    def __call__(self, argv, cwd, stdin):
        Path(cwd, f"{Path(cwd).name}.txt").write_text("x", encoding="utf-8")
        return _ok()
