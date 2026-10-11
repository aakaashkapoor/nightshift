"""Pull-request flow (SPEC §9).

For repos with `pr.enabled`, a slice's branch is pushed and a PR is opened whose
body closes the backing issue (`Closes #N`). With no external review required the
PR is auto-merged; otherwise it's left open for human sign-off. git + gh calls go
through injectable runners so this is unit-testable without a live remote.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ._exe import GH_DIRS, which

BABYSIT_LABEL = "nightshift:babysit"
REVIEW_LABEL = "review"  # on the issue: wait for a human instead of auto-merging


def issue_number_for(slice_id: str) -> str | None:
    """The backing GitHub issue number, or None for a local-md slice."""
    if slice_id.startswith("issue-"):
        return slice_id.removeprefix("issue-")
    return None


def build_pr_body(sl, *, closes_issue: str | None = None, decisions: Sequence[str] = ()) -> str:
    parts = [sl.body.strip(), ""]
    if decisions:
        parts += ["## Decisions", *(f"- {d}" for d in decisions), ""]
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

    def open(
        self, *, branch: str, base: str, title: str, body: str, labels: tuple[str, ...] = ()
    ) -> str:
        """Push the branch and open a PR; returns the PR URL."""
        self.git("push", "-u", "origin", branch)
        for label in labels:
            self.gh("label", "create", label, "--force")  # idempotent: create or update
        args = ["pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body]
        if labels:
            args += ["--label", ",".join(labels)]
        return self.gh(*args)

    def state(self, branch: str) -> str:
        """The PR's state for this head branch: OPEN, MERGED or CLOSED."""
        return json.loads(self.gh("pr", "view", branch, "--json", "state"))["state"]

    def view(self, branch: str) -> tuple[str, str]:
        """(state, mergeable) for this head branch in ONE gh call; polled for every in-review PR."""
        data = json.loads(self.gh("pr", "view", branch, "--json", "state,mergeable"))
        return data["state"], data["mergeable"]

    def automerge(self, branch: str) -> None:
        self.gh("pr", "merge", branch, "--squash", "--auto")

    def merge(
        self,
        branch: str,
        *,
        wait: float = 60.0,
        poll: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Squash-merge once GitHub has worked out mergeability; raise if it isn't mergeable.

        GitHub reports UNKNOWN for a few seconds after a push, so wait it out (field note #8).
        """
        deadline = clock() + wait
        while True:
            mergeable = json.loads(self.gh("pr", "view", branch, "--json", "mergeable"))[
                "mergeable"
            ]
            if mergeable != "UNKNOWN" or clock() >= deadline:
                break
            sleep(poll)
        if mergeable != "MERGEABLE":
            raise RuntimeError(f"not mergeable: {mergeable}")
        self.gh("pr", "merge", branch, "--squash", "--delete-branch")


def open_pr_for_slice(
    pr: GitHubPR,
    sl,
    *,
    branch: str,
    base: str,
    automerge: bool,
    labels: tuple[str, ...] = (),
    decisions: Sequence[str] = (),
) -> str:
    body = build_pr_body(sl, closes_issue=issue_number_for(sl.id), decisions=decisions)
    url = pr.open(branch=branch, base=base, title=sl.title, body=body, labels=labels)
    if automerge:
        pr.automerge(branch)
    return url
