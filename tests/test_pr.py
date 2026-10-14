"""Tests for the PR flow (injected git + gh runners)."""

import json

import pytest

from nightshift.pr import (
    BABYSIT_LABEL,
    REVIEW_LABEL,
    GitHubPR,
    build_pr_body,
    checks_state,
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


def test_view_reads_state_and_mergeable_in_one_call() -> None:
    gh = FakeRunner(ret='{"state": "OPEN", "mergeable": "CONFLICTING"}')
    assert GitHubPR(git=FakeRunner(), gh=gh).view("b") == ("OPEN", "CONFLICTING")
    assert gh.calls == [("pr", "view", "b", "--json", "state,mergeable")]


def test_review_label_name() -> None:
    assert REVIEW_LABEL == "review"


def test_build_pr_body_lists_decisions() -> None:
    body = build_pr_body(_slice("issue-42"), closes_issue="42", decisions=["used zod", "no retry"])
    assert "## Decisions\n- used zod\n- no retry\n" in body
    assert body.index("## Decisions") < body.index("Closes #42")
    assert "## Decisions" not in build_pr_body(_slice("issue-42"), closes_issue="42")


def test_open_pr_for_slice_puts_decisions_in_the_body() -> None:
    gh = FakeRunner(ret="url")
    open_pr_for_slice(
        GitHubPR(git=FakeRunner(), gh=gh),
        _slice("issue-42"),
        branch="b",
        base="main",
        automerge=False,
        decisions=["used zod"],
    )
    create = next(c for c in gh.calls if c[0:2] == ("pr", "create"))
    assert "- used zod" in create[create.index("--body") + 1]


class MergeGh:
    """Answers `pr view --json mergeable` from a list of states; records the rest."""

    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "view"):
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return json.dumps({"mergeable": state})
        return ""


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds

    def __call__(self):
        return self.t


def test_merge_waits_through_unknown_then_squash_merges() -> None:
    gh, clock = MergeGh(["UNKNOWN", "UNKNOWN", "MERGEABLE"]), Clock()
    GitHubPR(git=FakeRunner(), gh=gh).merge("b", sleep=clock.sleep, clock=clock, checks_grace=0.0)
    assert clock.slept == [3.0, 3.0]
    assert gh.calls[-1] == ("pr", "merge", "b", "--squash", "--delete-branch")


def test_merge_raises_on_conflicting_without_merging() -> None:
    gh, clock = MergeGh(["CONFLICTING"]), Clock()
    with pytest.raises(RuntimeError, match="not mergeable: CONFLICTING"):
        GitHubPR(git=FakeRunner(), gh=gh).merge("b", sleep=clock.sleep, clock=clock)
    assert not any(c[:2] == ("pr", "merge") for c in gh.calls)


def test_merge_times_out_while_unknown() -> None:
    gh, clock = MergeGh(["UNKNOWN"]), Clock()
    with pytest.raises(RuntimeError, match="not mergeable: UNKNOWN"):
        GitHubPR(git=FakeRunner(), gh=gh).merge(
            "b", wait=10.0, poll=4.0, sleep=clock.sleep, clock=clock
        )
    assert clock.slept == [4.0, 4.0, 4.0]  # polled until the 10s wait ran out


class ChecksGh(MergeGh):
    """MergeGh that also answers `pr view --json statusCheckRollup` from a list of rollups."""

    def __init__(self, rollups, mergeable="MERGEABLE"):
        super().__init__([mergeable])
        self.rollups = list(rollups)

    def __call__(self, *args):
        if args[:2] == ("pr", "view") and "statusCheckRollup" in args:
            self.calls.append(args)
            rollup = self.rollups.pop(0) if len(self.rollups) > 1 else self.rollups[0]
            return json.dumps({"statusCheckRollup": rollup})
        return super().__call__(*args)


def _run(name, status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def _ctx(context, state):
    return {"__typename": "StatusContext", "context": context, "state": state}


@pytest.mark.parametrize(
    ("rollup", "expected"),
    [
        ([], ("none", [])),
        (
            [_run("check"), _run("lint", conclusion="SKIPPED"), _run("x", conclusion="NEUTRAL")],
            ("passed", []),
        ),
        ([_run("check"), _run("lint", status="IN_PROGRESS", conclusion="")], ("pending", [])),
        (
            [_run("check", conclusion="FAILURE"), _run("lint", status="QUEUED", conclusion="")],
            ("failed", ["check"]),
        ),
        ([_ctx("ci/legacy", "SUCCESS")], ("passed", [])),
        ([_ctx("ci/legacy", "PENDING"), _ctx("ci/other", "EXPECTED")], ("pending", [])),
        (
            [_ctx("ci/legacy", "ERROR"), _run("check", conclusion="CANCELLED")],
            ("failed", ["ci/legacy", "check"]),
        ),
        ([{"status": "COMPLETED", "conclusion": "TIMED_OUT"}], ("failed", ["check"])),
    ],
)
def test_checks_state(rollup, expected) -> None:
    assert checks_state(rollup) == expected


def test_merge_waits_for_pending_ci_then_merges() -> None:
    pending = [_run("check", status="IN_PROGRESS", conclusion="")]
    gh, clock = ChecksGh([pending, pending, [_run("check")]]), Clock()
    GitHubPR(git=FakeRunner(), gh=gh).merge("b", sleep=clock.sleep, clock=clock)
    assert clock.slept == [15.0, 15.0]
    assert gh.calls[-1] == ("pr", "merge", "b", "--squash", "--delete-branch")


def test_merge_refuses_when_ci_fails() -> None:
    gh, clock = ChecksGh([[_run("check"), _run("lint-repo", conclusion="FAILURE")]]), Clock()
    with pytest.raises(RuntimeError, match="CI failed: lint-repo"):
        GitHubPR(git=FakeRunner(), gh=gh).merge("b", sleep=clock.sleep, clock=clock)
    assert not any(c[:2] == ("pr", "merge") for c in gh.calls)


def test_merge_without_ci_waits_out_the_grace_then_merges() -> None:
    gh, clock = ChecksGh([[]]), Clock()
    GitHubPR(git=FakeRunner(), gh=gh).merge("b", sleep=clock.sleep, clock=clock, checks_grace=30.0)
    assert clock.slept == [15.0, 15.0]  # checks get time to register; none did
    assert gh.calls[-1] == ("pr", "merge", "b", "--squash", "--delete-branch")


def test_merge_gives_up_when_ci_outlasts_the_wait() -> None:
    gh, clock = ChecksGh([[_run("check", status="QUEUED", conclusion="")]]), Clock()
    with pytest.raises(RuntimeError, match="CI still running after 1 min"):
        GitHubPR(git=FakeRunner(), gh=gh).merge(
            "b", sleep=clock.sleep, clock=clock, checks_wait=60.0
        )
    assert not any(c[:2] == ("pr", "merge") for c in gh.calls)
