"""Tests for the GitHub Issues source adapter (injected gh runner)."""

import json

from nightshift.github import LABELS, GitHubIssuesSource
from nightshift.slice import Slice


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
    assert ("issue", "close", "1") in gh.calls


def test_set_status_done_strips_every_status_label() -> None:
    src, gh = _source()
    src.set_status("issue-1", "done")
    edit = next(c for c in gh.calls if c[:2] == ("issue", "edit"))
    assert edit[2] == "1" and "--add-label" not in edit
    assert set(edit[edit.index("--remove-label") + 1].split(",")) == set(LABELS.values())


def test_set_status_done_tolerates_a_failed_label_edit() -> None:
    class Failing(FakeGh):
        def __call__(self, *args):
            if args[:2] == ("issue", "edit"):
                raise RuntimeError("label not found")
            return super().__call__(*args)

    gh = Failing(ISSUES)
    GitHubIssuesSource(".", gh=gh).set_status("issue-1", "done")  # must not raise
    assert ("issue", "close", "1") in gh.calls


def test_every_depends_on_line_is_read() -> None:
    issues = [
        {
            "number": 9,
            "title": "t",
            "body": "## Goal\nx\n\nDepends-on: #7\nDepends-on: #8, #7\ndepends-on: 6",
            "state": "OPEN",
            "labels": [{"name": "nightshift:ready"}],
        }
    ]
    src = GitHubIssuesSource(".", gh=FakeGh(issues))
    assert src.get("issue-9").depends_on == ["issue-7", "issue-8", "issue-6"]


def test_set_blocked_labels_and_comments() -> None:
    src, gh = _source()
    src.set_blocked("issue-2", "conflict")
    kinds = [c[0:2] for c in gh.calls]
    assert ("issue", "edit") in kinds
    assert ("issue", "comment") in kinds
    comment = next(c for c in gh.calls if c[0:2] == ("issue", "comment"))
    assert "conflict" in comment[-1]


def test_in_review_label_maps_to_status() -> None:
    issues = [
        {
            "number": 7,
            "title": "t",
            "body": "",
            "state": "OPEN",
            "labels": [{"name": "nightshift:in-review"}],
        }
    ]
    src = GitHubIssuesSource(".", gh=FakeGh(issues))
    assert src.get("issue-7").status == "in-review"


def test_set_status_in_review_swaps_labels() -> None:
    gh = FakeGh([])
    GitHubIssuesSource(".", gh=gh).set_status("issue-7", "in-review")
    edit = [c for c in gh.calls if c[0] == "issue"][0]
    assert edit[edit.index("--add-label") + 1] == "nightshift:in-review"
    assert "nightshift:in-progress" in edit[edit.index("--remove-label") + 1]


def test_append_to_section_rewrites_the_issue_body() -> None:
    class Gh(FakeGh):
        def __call__(self, *args):
            out = super().__call__(*args)
            if args[:2] == ("issue", "view"):
                return json.dumps({"body": "## Goal\ng\n\n## Touches\n- a.py\n"})
            return out

    gh = Gh(ISSUES)
    GitHubIssuesSource(".", gh=gh).append_to_section("issue-7", "Touches", "- b.py")
    assert gh.calls[0] == ("issue", "view", "7", "--json", "body")
    assert gh.calls[1] == (
        "issue",
        "edit",
        "7",
        "--body",
        "## Goal\ng\n\n## Touches\n- a.py\n- b.py\n",
    )


def test_append_to_section_tolerates_an_empty_body() -> None:
    class Gh(FakeGh):
        def __call__(self, *args):
            super().__call__(*args)
            return json.dumps({"body": None}) if args[:2] == ("issue", "view") else ""

    gh = Gh(ISSUES)
    GitHubIssuesSource(".", gh=gh).append_to_section("issue-7", "Answers", "- yes")
    assert gh.calls[1][-1] == "\n## Answers\n- yes\n"


def test_slices_carry_their_issue_labels() -> None:
    issues = [
        {
            "number": 7,
            "title": "T",
            "body": "b",
            "labels": [{"name": "nightshift:ready"}, {"name": "review"}],
            "state": "OPEN",
        }
    ]
    src = GitHubIssuesSource(".", gh=FakeGh(issues))
    assert src.get("issue-7").labels == {"nightshift:ready", "review"}
    assert Slice(id="x", title="t", status="ready").labels == set()


def _label_creates(gh):
    return [c for c in gh.calls if c[:2] == ("label", "create")]


def test_labels_are_created_once_before_the_first_edit() -> None:
    src, gh = _source()
    assert _label_creates(gh) == []  # lazy: reads create nothing
    src.list_all()
    assert _label_creates(gh) == []
    src.set_status("issue-1", "in-progress")
    creates = _label_creates(gh)
    assert {c[2] for c in creates} == set(LABELS.values())
    assert all("--force" in c and "--description" in c for c in creates)
    first_edit = next(i for i, c in enumerate(gh.calls) if c[:2] == ("issue", "edit"))
    assert all(gh.calls.index(c) < first_edit for c in creates)
    src.set_blocked("issue-1", "why")
    src.set_needs_detail("issue-1", "more")
    assert len(_label_creates(gh)) == len(LABELS)  # once per instance


def test_each_write_path_ensures_labels() -> None:
    for write in (
        lambda s: s.set_blocked("issue-1", ""),
        lambda s: s.set_needs_detail("issue-1", "m"),
    ):
        src, gh = _source()
        write(src)
        assert len(_label_creates(gh)) == len(LABELS)


def test_label_creation_failure_is_ignored() -> None:
    class Failing(FakeGh):
        def __call__(self, *args):
            if args[:2] == ("label", "create"):
                self.calls.append(args)
                raise RuntimeError("no permission")
            return super().__call__(*args)

    gh = Failing(ISSUES)
    src = GitHubIssuesSource(".", gh=gh)
    src.set_status("issue-1", "in-progress")  # must not raise
    assert gh.calls[-1][:2] == ("issue", "edit")
    src.set_status("issue-1", "in-review")
    assert len(_label_creates(gh)) == len(LABELS)  # no retry storm


def test_status_precedence_is_deterministic() -> None:
    order = ["in-progress", "in-review", "blocked", "needs-detail", "ready"]
    for i, winner in enumerate(order):
        labels = {LABELS[s] for s in order[i:]}
        for _ in range(20):
            assert GitHubIssuesSource._status_of(set(labels), "OPEN") == winner
    assert GitHubIssuesSource._status_of({LABELS["ready"]}, "CLOSED") == "done"
    assert GitHubIssuesSource._status_of({"bug"}, "OPEN") is None
