"""Tests for the GitHub Issues source adapter (injected gh runner)."""

import json

from nightshift.github import GitHubIssuesSource


class FakeGh:
    def __init__(self, issues):
        self.issues = issues
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("issue", "list"):
            return json.dumps(self.issues)
        return ""


ISSUES = [
    {
        "number": 1,
        "title": "Model",
        "body": "## Goal\nmodel",
        "labels": [{"name": "nightshift:ready"}],
        "state": "OPEN",
    },
    {
        "number": 2,
        "title": "Login",
        "body": "## Goal\nlogin\n\nDepends-on: #1",
        "labels": [{"name": "nightshift:in-progress"}],
        "state": "OPEN",
    },
    {"number": 3, "title": "Old", "body": "done", "labels": [], "state": "CLOSED"},
    {"number": 4, "title": "Not ours", "body": "x", "labels": [], "state": "OPEN"},
]


def _source():
    gh = FakeGh(ISSUES)
    return GitHubIssuesSource(".", gh=gh), gh


def test_list_all_maps_issues_and_skips_unmanaged() -> None:
    src, _ = _source()
    slices = {s.id: s for s in src.list_all()}
    # #4 has no nightshift label and is open -> skipped; #3 closed -> done.
    assert set(slices) == {"issue-1", "issue-2", "issue-3"}
    assert slices["issue-1"].status == "ready"
    assert slices["issue-2"].status == "in-progress"
    assert slices["issue-3"].status == "done"
    assert slices["issue-2"].depends_on == ["issue-1"]


def test_list_ready_only() -> None:
    src, _ = _source()
    assert [s.id for s in src.list_ready()] == ["issue-1"]


def test_set_status_in_progress_edits_labels() -> None:
    src, gh = _source()
    src.set_status("issue-1", "in-progress")
    call = gh.calls[-1]
    assert call[0:3] == ("issue", "edit", "1")
    assert "--add-label" in call and "nightshift:in-progress" in call
    assert "--remove-label" in call


def test_set_status_done_closes_issue() -> None:
    src, gh = _source()
    src.set_status("issue-1", "done")
    assert gh.calls[-1] == ("issue", "close", "1")


def test_set_blocked_labels_and_comments() -> None:
    src, gh = _source()
    src.set_blocked("issue-2", "conflict")
    kinds = [c[0:2] for c in gh.calls]
    assert ("issue", "edit") in kinds
    assert ("issue", "comment") in kinds
    comment = next(c for c in gh.calls if c[0:2] == ("issue", "comment"))
    assert "conflict" in comment[-1]
