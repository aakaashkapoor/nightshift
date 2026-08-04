"""Pull-request flow (SPEC §9).

For repos with `pr.enabled`, a slice's branch is pushed and a PR is opened whose
body closes the backing issue (`Closes #N`). With no external review required the
PR is auto-merged; otherwise it's left open for human sign-off. git + gh calls go
through injectable runners so this is unit-testable without a live remote.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ._exe import GH_DIRS, which


def issue_number_for(slice_id: str) -> str | None:
    """The backing GitHub issue number, or None for a local-md slice."""
    if slice_id.startswith("issue-"):
        return slice_id.removeprefix("issue-")
    return None


def build_pr_body(sl, *, closes_issue: str | None = None) -> str:
    parts = [sl.body.strip(), ""]
    if closes_issue:
        parts.append(f"Closes #{closes_issue}")
    parts.append("🌙 Shipped autonomously by Nightshift.")
    return "\n".join(parts).strip() + "\n"


def _runner(bin_name: str, repo_path: Path | str):  # pragma: no cover
    exe = which(bin_name, GH_DIRS) or bin_name

    def run(*args: str) -> str:
        result = subprocess.run(
            [exe, *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(f"{bin_name} {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    return run


class GitHubPR:
    def __init__(self, repo_path: Path | str = ".", git=None, gh=None):
        self.git = git or _runner("git", repo_path)
        self.gh = gh or _runner("gh", repo_path)

    def open(self, *, branch: str, base: str, title: str, body: str) -> str:
        """Push the branch and open a PR; returns the PR URL."""
        self.git("push", "-u", "origin", branch)
        return self.gh(
            "pr",
            "create",
            "--head",
            branch,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        )

    def automerge(self, branch: str) -> None:
        self.gh("pr", "merge", branch, "--squash", "--auto")


def open_pr_for_slice(pr: GitHubPR, sl, *, branch: str, base: str, automerge: bool) -> str:
    body = build_pr_body(sl, closes_issue=issue_number_for(sl.id))
    url = pr.open(branch=branch, base=base, title=sl.title, body=body)
    if automerge:
        pr.automerge(branch)
    return url
