"""Tests for the PR flow (injected git + gh runners)."""

from nightshift.pr import GitHubPR, build_pr_body, issue_number_for, open_pr_for_slice
from nightshift.slice import Slice


def _slice(sid) -> Slice:
    return Slice(id=sid, title="Add login", status="ready", body="## Goal\nlogin\n")


class FakeRunner:
    def __init__(self, ret=""):
        self.ret = ret
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.ret


def test_issue_number_for() -> None:
    assert issue_number_for("issue-42") == "42"
    assert issue_number_for("slice-003") is None


def test_build_pr_body_closes_issue_for_github_slice() -> None:
    body = build_pr_body(_slice("issue-42"), closes_issue="42")
    assert "Closes #42" in body
    assert "login" in body


def test_build_pr_body_no_closes_for_local_slice() -> None:
    body = build_pr_body(_slice("slice-003"), closes_issue=None)
    assert "Closes #" not in body


def test_open_pushes_branch_and_creates_pr() -> None:
    git, gh = FakeRunner(), FakeRunner(ret="https://gh/pr/1")
    pr = GitHubPR(git=git, gh=gh)

    url = pr.open(branch="nightshift/issue-42", base="main", title="Add login", body="b")

    assert url == "https://gh/pr/1"
    assert git.calls[0] == ("push", "-u", "origin", "nightshift/issue-42")
    create = gh.calls[0]
    assert create[0:2] == ("pr", "create")
    assert "--head" in create and "nightshift/issue-42" in create
    assert "--base" in create and "main" in create


def test_open_pr_for_slice_automerges_and_closes_issue() -> None:
    git, gh = FakeRunner(), FakeRunner(ret="url")
    pr = GitHubPR(git=git, gh=gh)

    open_pr_for_slice(
        pr, _slice("issue-42"), branch="nightshift/issue-42", base="main", automerge=True
    )

    create = next(c for c in gh.calls if c[0:2] == ("pr", "create"))
    body = create[create.index("--body") + 1]
    assert "Closes #42" in body
    assert any(c[0:2] == ("pr", "merge") and "--auto" in c for c in gh.calls)


def test_open_pr_for_slice_no_automerge_leaves_pr_open() -> None:
    git, gh = FakeRunner(), FakeRunner(ret="url")
    pr = GitHubPR(git=git, gh=gh)

    open_pr_for_slice(
        pr, _slice("issue-42"), branch="nightshift/issue-42", base="main", automerge=False
    )

    assert not any(c[0:2] == ("pr", "merge") for c in gh.calls)  # awaits human sign-off
