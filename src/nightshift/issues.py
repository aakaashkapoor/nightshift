"""Deterministic issue tooling behind `nsh issue new | set` (v2 design, "The issue template").

Issues are filed from the template and linted first; labels change only through
``set_issue``. The ``gh`` runner is injected so the logic is testable without GitHub.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .github import LABELS, GitHubIssuesSource
from .lint import lint
from .pr import GitHubPR
from .slice import Slice

TEMPLATE_ORDER = (
    "Size",
    "Goal",
    "Decisions",
    "Contract",
    "Out of scope",
    "Acceptance",
    "Touches",
    "Subtasks",
)
ACTIONS = ("ready", "review", "no-review", "visual", "no-visual", "hold")


def _items(values: Sequence[str]) -> str:
    return "\n".join(f"- {v}" for v in values)


def render_issue(
    *,
    size: str,
    goal: str,
    acceptance: Sequence[str],
    touches: Sequence[str],
    decisions: Sequence[str] = (),
    owns: Sequence[str] = (),
    uses: Sequence[str] = (),
    out_of_scope: Sequence[str] = (),
    subtasks: str = "",
    depends: Sequence[str] = (),
) -> str:
    """The v2 issue body. Empty optional sections are left out."""
    contract = []
    if owns:
        contract.append(f"Owns: {', '.join(owns)}")
    if uses:
        contract.append(f"Uses: {', '.join(uses)}")
    sections = {
        "Size": size,
        "Goal": goal,
        "Decisions": _items(decisions),
        "Contract": "\n".join(contract),
        "Out of scope": _items(out_of_scope),
        "Acceptance": _items(acceptance),
        "Touches": _items(touches),
        "Subtasks": subtasks.strip(),
    }
    blocks = [f"## {name}\n{sections[name]}" for name in TEMPLATE_ORDER if sections[name]]
    if depends:
        blocks.append(_depends_line(depends))
    return "\n\n".join(blocks) + "\n"


def _depends_line(depends: Sequence[str]) -> str:
    return "Depends-on: " + ", ".join(f"#{d.lstrip('#')}" for d in depends)


def with_depends(body: str, depends: Sequence[str]) -> str:
    """``body`` with a ``Depends-on:`` line for ``depends`` appended (unchanged when there are none)."""
    if not depends:
        return body
    return body.rstrip("\n") + "\n\n" + _depends_line(depends) + "\n"


def create_issue(gh, *, title: str, body: str, review: bool = False, visual: bool = False) -> str:
    """Lint, then file the issue as ready; returns its URL. A lint failure files nothing."""
    problems = lint(Slice(id="new", title=title, status="ready", body=body))
    if problems:
        raise ValueError("issue not filed:\n" + "\n".join(f"- {p}" for p in problems))
    labels = [LABELS["ready"], *(["review"] if review else []), *(["visual"] if visual else [])]
    for label in labels:
        gh("label", "create", label, "--force")  # idempotent: create or update
    out = gh("issue", "create", "--title", title, "--body", body, "--label", ",".join(labels))
    return out.strip()


def set_issue(gh, number: int | str, action: str) -> None:
    """The only way labels change by hand."""
    num = str(number)
    if action == "ready":
        GitHubIssuesSource(gh=gh).set_status(num, "ready")
    elif action in ("review", "visual"):
        gh("label", "create", action, "--force")
        gh("issue", "edit", num, "--add-label", action)
    elif action in ("no-review", "no-visual"):
        gh("issue", "edit", num, "--remove-label", action.removeprefix("no-"))
    elif action == "hold":
        gh("issue", "edit", num, "--remove-label", LABELS["ready"])
    else:
        raise ValueError(f"unknown action {action!r}; valid: {', '.join(ACTIONS)}")


def new_cli(repo: Path, *, title: str, body: str, review: bool, visual: bool) -> str:
    return create_issue(
        GitHubPR(repo.resolve()).gh, title=title, body=body, review=review, visual=visual
    )


def set_cli(repo: Path, number: int, action: str) -> str:
    set_issue(GitHubPR(repo.resolve()).gh, number, action)
    return f"#{number}: {action}"
