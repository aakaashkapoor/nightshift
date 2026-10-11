"""The always-on AI review step (`nightshift-review`, SPEC §9).

A reviewer inspects a slice's work before it commits. **Blocking** findings loop the
slice back for a fix round; **advisory** findings are logged and don't stop the merge.
AI review always runs in production; it's behind an interface so a fake is used in
tests and so external/human review can layer on top later.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .limits import UsageLimitReached
from .pipeline import _git

BLOCKING = "blocking"
ADVISORY = "advisory"


@dataclass
class Finding:
    severity: str  # "blocking" | "advisory"
    message: str


@dataclass
class ReviewResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == BLOCKING]

    @property
    def advisory(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ADVISORY]


class Reviewer(Protocol):
    def review(self, worktree_path: Path | str, sl) -> ReviewResult: ...


REVIEW_PROMPT = """You are Nightshift's code reviewer. Below are the issue, the agent's plan (if any) and the full diff. Review the diff against the issue's goal, acceptance and contract. Look for correctness bugs, missed acceptance items, and duplicated or conflicting definitions.

Respond with ONLY a JSON object: {{"findings": [{{"severity": "blocking"|"advisory", "message": "..."}}]}}.
A blocking finding MUST name the location as path:line in its message; one without a location is treated as advisory. An empty list means the work is good.

# Issue: {title}

{body}

# Plan

{plan}

# Diff

{diff}
"""

_LOCATION = re.compile(r"[\w./\\-]+\.\w+:\d+")

FIX_PREFIX = (
    "Code review found blocking issues in your work. Fix ALL of them, keeping the "
    "acceptance criteria satisfied. Do not commit — the harness handles git.\n\n"
)


class AgentReviewer:
    """Real reviewer: a headless Claude that returns findings as JSON."""

    def __init__(self, executor, base_branch: str = "main", max_diff: int = 80_000):
        self.executor = executor
        self.base_branch = base_branch
        self.max_diff = max_diff

    def _diff(self, worktree_path: Path | str) -> str:
        """The work against where the branch started, new files included."""
        _git(worktree_path, "add", "-A", "-N")  # intent-to-add: new files show in the diff
        start = _git(worktree_path, "merge-base", self.base_branch, "HEAD")
        diff = _git(worktree_path, "diff", start)
        if len(diff) > self.max_diff:
            diff = diff[: self.max_diff] + "\n[diff truncated]"
        return diff

    def review(self, worktree_path: Path | str, sl) -> ReviewResult:
        plan_path = Path(worktree_path) / ".nightshift" / "plan.md"
        plan = plan_path.read_text(encoding="utf-8").strip() if plan_path.exists() else ""
        prompt = REVIEW_PROMPT.format(
            title=sl.title,
            body=sl.body.strip(),
            plan=plan or "(none)",
            diff=self._diff(worktree_path),
        )
        result = self.executor.execute_prompt(str(worktree_path), prompt)
        if result.usage_limited:
            raise UsageLimitReached(result.session_id)
        parsed = parse_findings(result.result_text)
        return ReviewResult(
            [
                Finding(ADVISORY, f.message)
                if f.severity == BLOCKING and not _LOCATION.search(f.message)
                else f
                for f in parsed.findings
            ]
        )


def parse_findings(text: str) -> ReviewResult:
    """Parse a reviewer agent's JSON output; unparseable output => no findings."""
    try:
        data = json.loads(text)
        raw = data.get("findings", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, TypeError, AttributeError):
        return ReviewResult([])
    findings = [
        Finding(f.get("severity", ADVISORY), f.get("message", ""))
        for f in raw
        if isinstance(f, dict)
    ]
    return ReviewResult(findings)


def fix_prompt(findings: list[Finding]) -> str:
    bullets = "\n".join(f"- {f.message}" for f in findings)
    return FIX_PREFIX + bullets + "\n"
