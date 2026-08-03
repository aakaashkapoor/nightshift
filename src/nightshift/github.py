"""GitHub Issues source adapter (SPEC §4).

A first-class, authoritative source (interchangeable with `LocalMdSource`): each
slice is a GitHub issue, status is carried by ``nightshift:*`` labels (done = the
issue is closed). All `gh` calls go through an injectable runner so this is
unit-testable without a live repo; the default runner shells out to `gh` in the
repo directory.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .slice import Slice

PREFIX = "nightshift:"
LABELS = {
    "ready": f"{PREFIX}ready",
    "in-progress": f"{PREFIX}in-progress",
    "blocked": f"{PREFIX}blocked",
}
_LABEL_TO_STATUS = {v: k for k, v in LABELS.items()}
_DEPENDS_RE = re.compile(r"(?im)^depends-on:\s*(.+)$")


def _default_gh(repo_path: Path | str):
    exe = shutil.which("gh") or "gh"

    def run(*args: str) -> str:
        result = subprocess.run(
            [exe, *args], cwd=str(repo_path), capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    return run


class GitHubIssuesSource:
    def __init__(self, repo_path: Path | str = ".", gh=None):
        self.repo_path = repo_path
        self.gh = gh or _default_gh(repo_path)

    # --- reads ---------------------------------------------------------------

    def list_all(self) -> list[Slice]:
        import json

        out = self.gh(
            "issue", "list", "--state", "all", "--limit", "500",
            "--json", "number,title,body,labels,state",
        )
        slices = [self._to_slice(issue) for issue in json.loads(out)]
        return [s for s in slices if s is not None]

    def list_ready(self) -> list[Slice]:
        return [s for s in self.list_all() if s.status == "ready"]

    def get(self, slice_id: str) -> Slice | None:
        return next((s for s in self.list_all() if s.id == slice_id), None)

    # --- writes --------------------------------------------------------------

    def set_status(self, slice_id: str, status: str) -> None:
        num = _num(slice_id)
        if status == "done":
            self.gh("issue", "close", num)
            return
        add = LABELS[status]
        remove = ",".join(v for k, v in LABELS.items() if k != status)
        self.gh("issue", "edit", num, "--add-label", add, "--remove-label", remove)

    def set_blocked(self, slice_id: str, reason: str) -> None:
        num = _num(slice_id)
        remove = ",".join(v for k, v in LABELS.items() if k != "blocked")
        self.gh("issue", "edit", num, "--add-label", LABELS["blocked"], "--remove-label", remove)
        if reason:
            self.gh("issue", "comment", num, "--body", f"⛔ Blocked: {reason}")

    # --- mapping -------------------------------------------------------------

    def _to_slice(self, issue: dict) -> Slice | None:
        labels = {label["name"] for label in issue.get("labels", [])}
        status = self._status_of(labels, issue.get("state", ""))
        if status is None:
            return None  # not a Nightshift-managed issue
        body = issue.get("body") or ""
        return Slice(
            id=f"issue-{issue['number']}",
            title=issue.get("title", ""),
            status=status,
            depends_on=_parse_depends(body),
            body=body,
        )

    @staticmethod
    def _status_of(labels: set[str], state: str) -> str | None:
        if state.lower() == "closed":
            return "done"
        for label in labels:
            if label in _LABEL_TO_STATUS:
                return _LABEL_TO_STATUS[label]
        return None


def _num(slice_id: str) -> str:
    return slice_id.removeprefix("issue-")


def _parse_depends(body: str) -> list[str]:
    match = _DEPENDS_RE.search(body)
    if not match:
        return []
    return [
        f"issue-{tok.lstrip('#').strip()}"
        for tok in match.group(1).split(",")
        if tok.strip()
    ]
