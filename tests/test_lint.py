"""Issue sections, size, the linter, and needs-detail parking (v2 design, Step 1)."""

import json
import subprocess
from pathlib import Path

import pytest

from nightshift.config import RepoConfig
from nightshift.daemon import Daemon
from nightshift.events import EventLog
from nightshift.executor import Executor, RunOutput
from nightshift.github import GitHubIssuesSource
from nightshift.lint import lint
from nightshift.slice import Slice
from nightshift.source import LocalMdSource
from nightshift.worktree import WorktreeManager


def _slice(body: str, status: str = "ready") -> Slice:
    return Slice.parse(f"---\nid: s-1\ntitle: T\nstatus: {status}\n---\n{body}")


GOOD = "## Goal\nDo it.\n\n## Acceptance\n- works\n"


# --- Slice.section / Slice.size --------------------------------------------------


def test_section_present_stops_at_next_heading_and_ignores_case() -> None:
    sl = _slice("## GOAL\nline one\nline two\n\n## Other\nnot this\n")
    assert sl.section("goal") == "line one\nline two"


def test_section_absent_is_none_and_empty_is_blank() -> None:
    sl = _slice("## Goal\n\n## Acceptance\nx\n")
    assert sl.section("Missing") is None
    assert sl.section("Goal") == ""


def test_section_runs_to_end_of_body() -> None:
    assert _slice("## Goal\nlast\n").section("Goal") == "last"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("S", "S"), ("M", "M"), ("L", "L"), ("l  (big)", "L"), ("s", "S")],
)
def test_size_reads_the_first_token(text: str, expected: str) -> None:
    assert _slice(f"## Size\n{text}\n").size() == expected


@pytest.mark.parametrize("body", ["## Goal\nx\n", "## Size\n\n", "## Size\nhuge\n"])
def test_size_defaults_to_m(body: str) -> None:
    assert _slice(body).size() == "M"


# --- lint -----------------------------------------------------------------------


def test_lint_clean_issue() -> None:
    assert lint(_slice(GOOD)) == []


def test_lint_accepts_acceptance_criteria_alias() -> None:
    assert lint(_slice("## Goal\nx\n\n## Acceptance criteria\n- y\n")) == []


def test_lint_missing_goal_and_acceptance() -> None:
    assert lint(_slice("just words\n")) == ["missing '## Goal'", "missing '## Acceptance'"]


def test_lint_empty_goal_counts_as_missing() -> None:
    assert lint(_slice("## Goal\n\n## Acceptance\n- y\n")) == ["missing '## Goal'"]


def test_lint_bad_size() -> None:
    assert lint(_slice(GOOD + "\n## Size\nhuge\n")) == ["'## Size' must be S, M or L (got 'huge')"]


def test_lint_size_l_needs_subtasks() -> None:
    assert lint(_slice(GOOD + "\n## Size\nL\n")) == ["size L needs '## Subtasks'"]
    assert lint(_slice(GOOD + "\n## Size\nL\n\n## Subtasks\n1. a\n")) == []


# --- sources --------------------------------------------------------------------


def test_local_md_set_needs_detail(tmp_path) -> None:
    d = tmp_path / ".slices"
    d.mkdir()
    (d / "s-1.md").write_text("---\nid: s-1\ntitle: T\nstatus: ready\n---\n## Goal\nx\n", "utf-8")
    src = LocalMdSource(tmp_path)
    src.set_needs_detail("s-1", "- missing '## Acceptance'")
    sl = src.get("s-1")
    assert sl is not None
    assert sl.status == "needs-detail"
    assert sl.section("Needs detail") == "- missing '## Acceptance'"
    src.set_needs_detail("s-1", "again")  # a later message replaces the old one
    sl = src.get("s-1")
    assert sl is not None
    assert sl.body.count("## Needs detail") == 1
    assert sl.section("Needs detail") == "again"
    assert sl.section("Goal") == "x"


def test_local_md_needs_detail_replacement_keeps_later_sections(tmp_path) -> None:
    d = tmp_path / ".slices"
    d.mkdir()
    (d / "s-1.md").write_text(
        "---\nid: s-1\ntitle: T\nstatus: ready\n---\n"
        "## Needs detail\nold question\n\n## Notes\nkeep me\n",
        "utf-8",
    )
    src = LocalMdSource(tmp_path)
    src.set_needs_detail("s-1", "new question")
    sl = src.get("s-1")
    assert sl is not None
    assert sl.section("Needs detail") == "new question"
    assert sl.section("Notes") == "keep me"


def test_local_md_set_needs_detail_unknown_slice(tmp_path) -> None:
    with pytest.raises(KeyError):
        LocalMdSource(tmp_path).set_needs_detail("nope", "x")


class FakeGh:
    def __init__(self, issues=()):
        self.issues = list(issues)
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        return json.dumps(self.issues) if args[:2] == ("issue", "list") else ""


def test_github_set_needs_detail_labels_and_comments() -> None:
    gh = FakeGh()
    GitHubIssuesSource(".", gh=gh).set_needs_detail("issue-7", "- missing '## Goal'")
    edit, comment = [c for c in gh.calls if c[:2] != ("label", "create")]
    assert edit[:5] == ("issue", "edit", "7", "--add-label", "nightshift:needs-detail")
    removed = edit[6].split(",")
    assert "nightshift:ready" in removed and "nightshift:needs-detail" not in removed
    assert comment == (
        "issue",
        "comment",
        "7",
        "--body",
        "❓ Needs detail:\n\n- missing '## Goal'",
    )


def test_github_maps_needs_detail_label() -> None:
    issue = {
        "number": 3,
        "title": "t",
        "body": "",
        "labels": [{"name": "nightshift:needs-detail"}],
        "state": "OPEN",
    }
    sl = GitHubIssuesSource(".", gh=FakeGh([issue])).get("issue-3")
    assert sl is not None and sl.status == "needs-detail"


# --- daemon ---------------------------------------------------------------------


def _git(cwd, *args) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


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


def _write(repo, sid, body) -> None:
    (repo / ".slices" / f"{sid}.md").write_text(
        f"---\nid: {sid}\ntitle: {sid}\nstatus: ready\n---\n{body}", encoding="utf-8"
    )


def _daemon(repo, tmp_path, **kw) -> Daemon:
    def agent(argv, cwd, stdin) -> RunOutput:
        (Path(cwd) / "out.txt").write_text("x", encoding="utf-8")
        return RunOutput(0, json.dumps({"result": "ok", "session_id": "s"}), "")

    return Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="demo", path=repo, check="echo ok", base_branch="main"),
        executor=Executor(runner=agent),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        events=EventLog(tmp_path / "events.jsonl"),
        **kw,
    )


def test_daemon_lints_when_enabled(repo, tmp_path) -> None:
    _write(repo, "bad", "## Goal\nonly a goal\n")
    _write(repo, "good", GOOD)
    d = _daemon(repo, tmp_path, lint_issues=True)
    results = d.tick()
    assert [(r.slice_id, r.status) for r in results] == [("good", "done")]
    bad = LocalMdSource(repo).get("bad")
    assert bad is not None and bad.status == "needs-detail"
    events, _ = EventLog(tmp_path / "events.jsonl").read_from(0)
    flagged = [e for e in events if e["kind"] == "needs_detail"]
    assert [(e["slice"], e["problems"]) for e in flagged] == [("bad", ["missing '## Acceptance'"])]


def test_daemon_does_not_lint_by_default(repo, tmp_path) -> None:
    _write(repo, "bare", "## Goal\nonly a goal\n")
    results = _daemon(repo, tmp_path).tick()
    assert [r.status for r in results] == ["done"]
