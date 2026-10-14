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
from typing import Any

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
        checks_grace: float = 30.0,
        checks_wait: float = 1800.0,
        checks_poll: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Squash-merge once GitHub has worked out mergeability and CI has passed.

        GitHub reports UNKNOWN for a few seconds after a push, so wait it out (field note #8).
        Then wait for the PR's checks (`wait_for_checks`); raise if it isn't mergeable or CI fails.
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
        self.wait_for_checks(
            branch, grace=checks_grace, wait=checks_wait, poll=checks_poll, sleep=sleep, clock=clock
        )
        self.gh("pr", "merge", branch, "--squash", "--delete-branch")

    def wait_for_checks(
        self,
        branch: str,
        *,
        grace: float,
        wait: float,
        poll: float,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        """Block until the PR's CI checks pass; raise if any fail or they outlast `wait`.

        Checks take a few seconds to register after a push, so a PR with none yet is
        given `grace` before it counts as a repo without CI (and merges).
        """
        start = clock()
        while True:
            view = json.loads(self.gh("pr", "view", branch, "--json", "statusCheckRollup"))
            state, failed = checks_state(view.get("statusCheckRollup") or [])
            if state == "passed":
                return
            if state == "failed":
                raise RuntimeError("CI failed: " + ", ".join(failed))
            elapsed = clock() - start
            if state == "none" and elapsed >= grace:
                return
            if elapsed >= wait:
                raise RuntimeError(f"CI still running after {int(wait // 60)} min")
            sleep(poll)


# A finished check run with one of these conclusions does not block a merge.
_CHECK_RUN_OK = ("SUCCESS", "NEUTRAL", "SKIPPED")


def checks_state(rollup: Sequence[dict[str, Any]]) -> tuple[str, list[str]]:
    """Summarize a PR's `statusCheckRollup`: ("none" | "pending" | "passed" | "failed", failed names).

    Entries are GitHub Actions check runs (`status` + `conclusion`) or legacy commit
    statuses (`state`). Any failure wins over anything still running.
    """
    if not rollup:
        return "none", []
    failed: list[str] = []
    pending = False
    for check in rollup:
        name = check.get("name") or check.get("context") or "check"
        if "status" in check:
            if check["status"] != "COMPLETED":
                pending = True
            elif check.get("conclusion") not in _CHECK_RUN_OK:
                failed.append(name)
        elif check.get("state") in ("PENDING", "EXPECTED"):
            pending = True
        elif check.get("state") != "SUCCESS":
            failed.append(name)
    if failed:
        return "failed", failed
    return ("pending", []) if pending else ("passed", [])


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
