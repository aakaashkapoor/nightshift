"""Tests for the PR flow (injected git + gh runners)."""

from nightshift.pr import (
    BABYSIT_LABEL,
    GitHubPR,
    build_pr_body,
    issue_number_for,
    open_pr_for_slice,
)
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


def test_open_with_labels_ensures_label_and_attaches_it() -> None:
    git, gh = FakeRunner(), FakeRunner(ret="url")
    GitHubPR(git=git, gh=gh).open(
        branch="b", base="main", title="t", body="x", labels=(BABYSIT_LABEL,)
    )
    assert gh.calls[0] == ("label", "create", BABYSIT_LABEL, "--force")
    create = gh.calls[1]
    assert create[create.index("--label") + 1] == BABYSIT_LABEL


def test_open_pr_for_slice_passes_labels() -> None:
    gh = FakeRunner(ret="url")
    open_pr_for_slice(
        GitHubPR(git=FakeRunner(), gh=gh),
        _slice("issue-42"),
        branch="b",
        base="main",
        automerge=False,
        labels=(BABYSIT_LABEL,),
    )
    assert any("--label" in c for c in gh.calls)


def test_state_reads_pr_state() -> None:
    gh = FakeRunner(ret='{"state": "MERGED"}')
    assert GitHubPR(git=FakeRunner(), gh=gh).state("nightshift/issue-42") == "MERGED"
    assert gh.calls[0] == ("pr", "view", "nightshift/issue-42", "--json", "state")
