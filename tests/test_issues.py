"""Tests for `nsh issue new` and `nsh issue set`: deterministic issue filing and labels."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightshift import issues
from nightshift.cli import app
from nightshift.issues import create_issue, render_issue, set_issue
from nightshift.slice import Slice

runner = CliRunner()


class Calls:
    def __init__(self, reply="https://github.com/o/r/issues/9\n"):
        self.calls: list[tuple] = []
        self.reply = reply

    def __call__(self, *args):
        self.calls.append(args)
        return self.reply


FULL = {
    "size": "L",
    "goal": "Build the thing.",
    "acceptance": ["pytest passes", "page renders"],
    "touches": ["src/a.py", "src/b/"],
    "decisions": ["Name it Foo"],
    "owns": ["Foo", "bar.py"],
    "uses": ["Baz"],
    "out_of_scope": ["Theming"],
    "subtasks": "### A\nfiles: x",
    "depends": ["1", "#2"],
}


def test_render_full_issue_has_every_section_in_order() -> None:
    body = render_issue(**FULL)
    assert body == (
        "## Size\nL\n\n"
        "## Goal\nBuild the thing.\n\n"
        "## Decisions\n- Name it Foo\n\n"
        "## Contract\nOwns: Foo, bar.py\nUses: Baz\n\n"
        "## Out of scope\n- Theming\n\n"
        "## Acceptance\n- pytest passes\n- page renders\n\n"
        "## Touches\n- src/a.py\n- src/b/\n\n"
        "## Subtasks\n### A\nfiles: x\n\n"
        "Depends-on: #1, #2\n"
    )
    sl = Slice(id="x", title="t", status="ready", body=body)
    assert sl.size() == "L" and sl.touches() == {"src/a.py", "src/b/"}


def test_render_minimal_issue_leaves_optional_sections_out() -> None:
    body = render_issue(size="S", goal="G", acceptance=["ok"], touches=["a.py"])
    assert body == "## Size\nS\n\n## Goal\nG\n\n## Acceptance\n- ok\n\n## Touches\n- a.py\n"


def test_render_contract_with_only_uses() -> None:
    body = render_issue(size="M", goal="G", acceptance=["ok"], touches=["a.py"], uses=["Baz"])
    assert "## Contract\nUses: Baz\n" in body and "Owns" not in body


def test_create_files_the_issue_with_labels() -> None:
    gh = Calls()
    body = render_issue(size="S", goal="G", acceptance=["ok"], touches=["a.py"])
    url = create_issue(gh, title="T", body=body, review=True, visual=True)
    assert url == "https://github.com/o/r/issues/9"
    assert gh.calls == [
        ("label", "create", "nightshift:ready", "--force"),
        ("label", "create", "review", "--force"),
        ("label", "create", "visual", "--force"),
        (
            "issue",
            "create",
            "--title",
            "T",
            "--body",
            body,
            "--label",
            "nightshift:ready,review,visual",
        ),
    ]


def test_create_without_optional_labels() -> None:
    gh = Calls()
    create_issue(gh, title="T", body=render_issue(size="S", goal="G", acceptance=["a"], touches=[]))
    assert gh.calls[-1][-1] == "nightshift:ready"
    assert len(gh.calls) == 2


def test_lint_failure_refuses_and_never_calls_gh() -> None:
    gh = Calls()
    with pytest.raises(ValueError, match=r"issue not filed:\n- missing '## Goal'"):
        create_issue(gh, title="T", body="## Acceptance\n- ok\n")
    assert gh.calls == []


# --- set ------------------------------------------------------------------------------


def test_set_ready_goes_through_the_source() -> None:
    gh = Calls()
    set_issue(gh, 5, "ready")
    assert gh.calls[0][:3] == ("issue", "edit", "5")
    assert "nightshift:ready" in gh.calls[0]


@pytest.mark.parametrize(
    "action,flag,label",
    [
        ("review", "--add-label", "review"),
        ("no-review", "--remove-label", "review"),
        ("visual", "--add-label", "visual"),
        ("no-visual", "--remove-label", "visual"),
        ("hold", "--remove-label", "nightshift:ready"),
    ],
)
def test_set_label_actions(action, flag, label) -> None:
    gh = Calls()
    set_issue(gh, 5, action)
    expected = []
    if flag == "--add-label":
        expected.append(("label", "create", label, "--force"))
    expected.append(("issue", "edit", "5", flag, label))
    assert gh.calls == expected


def test_set_unknown_action_lists_the_valid_ones() -> None:
    gh = Calls()
    with pytest.raises(ValueError, match="unknown action 'nope'.*ready.*hold"):
        set_issue(gh, 5, "nope")
    assert gh.calls == []


# --- CLI --------------------------------------------------------------------------------


class FakePR:
    instances: list["FakePR"] = []

    def __init__(self, repo):
        self.gh = Calls()
        FakePR.instances.append(self)


@pytest.fixture
def fake(monkeypatch):
    FakePR.instances = []
    monkeypatch.setattr(issues, "GitHubPR", FakePR)


def test_cli_new_from_body_file(fake, tmp_path: Path) -> None:
    body = tmp_path / "b.md"
    body.write_text(render_issue(size="M", goal="G", acceptance=["a"], touches=["x.py"]), "utf-8")
    result = runner.invoke(
        app,
        ["issue", "new", "--title", "T", "--body-file", str(body), "--review", "--repo", "."],
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "https://github.com/o/r/issues/9"
    assert FakePR.instances[0].gh.calls[-1][-1] == "nightshift:ready,review"


def test_cli_new_from_section_flags(fake, tmp_path: Path) -> None:
    sub = tmp_path / "s.md"
    sub.write_text("### A\n", "utf-8")
    result = runner.invoke(
        app,
        [
            *("issue", "new", "--title", "T", "--size", "l", "--goal", "G"),
            *("--acceptance", "a1", "--acceptance", "a2", "--touches", "p/q.py"),
            *("--decision", "d", "--owns", "O", "--uses", "U", "--out-of-scope", "N"),
            *("--subtasks-file", str(sub), "--depends", "3", "--visual"),
        ],
    )
    assert result.exit_code == 0, result.output
    call = FakePR.instances[0].gh.calls[-1]
    body = call[call.index("--body") + 1]
    assert "## Size\nL\n" in body and "Depends-on: #3" in body and "### A" in body
    assert call[-1] == "nightshift:ready,visual"


def test_cli_new_lint_failure_exits_1(fake) -> None:
    result = runner.invoke(app, ["issue", "new", "--title", "T", "--size", "M", "--goal", "G"])
    assert result.exit_code == 1
    assert "issue not filed" in result.output and "Acceptance" in result.output
    assert FakePR.instances[0].gh.calls == []


def test_cli_new_without_a_body_or_size_fails(fake) -> None:
    result = runner.invoke(app, ["issue", "new", "--title", "T"])
    assert result.exit_code == 1 and "--size" in result.output


def test_cli_gh_failure_exits_1(fake, monkeypatch) -> None:
    class Down(FakePR):
        def __init__(self, repo):
            self.gh = Calls()

            def boom(*a):
                raise RuntimeError("gh down")

            self.gh = boom

    monkeypatch.setattr(issues, "GitHubPR", Down)
    result = runner.invoke(app, ["issue", "set", "5", "hold"])
    assert result.exit_code == 1 and "gh down" in result.output


def test_cli_set_prints_what_changed(fake) -> None:
    result = runner.invoke(app, ["issue", "set", "5", "review"])
    assert result.exit_code == 0 and "#5: review" in result.stdout
    assert FakePR.instances[0].gh.calls[-1] == ("issue", "edit", "5", "--add-label", "review")


def test_cli_set_unknown_action_exits_1(fake) -> None:
    result = runner.invoke(app, ["issue", "set", "5", "bogus"])
    assert result.exit_code == 1 and "unknown action" in result.output
