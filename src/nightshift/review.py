"""The always-on AI review step (`nightshift-review`, SPEC §9).

A reviewer inspects a slice's work before it commits. **Blocking** findings loop the
slice back for a fix round; **advisory** findings are logged and don't stop the merge.
AI review always runs in production; it's behind an interface so a fake is used in
tests and so external/human review can layer on top later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

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


REVIEW_PROMPT = (
    "You are Nightshift's code reviewer. Review the changes on this branch against "
    "the slice's goal and acceptance criteria. Focus on correctness bugs and clear "
    "requirement misses.\n\n"
    "Respond with ONLY a JSON object: "
    '{\"findings\": [{\"severity\": \"blocking\"|\"advisory\", \"message\": \"...\"}]}. '
    "Use \"blocking\" only for real correctness/acceptance failures; everything else "
    "is \"advisory\". An empty findings list means the work is good."
)

FIX_PREFIX = (
    "Code review found blocking issues in your work. Fix ALL of them, keeping the "
    "acceptance criteria satisfied. Do not commit — the harness handles git.\n\n"
)


class AgentReviewer:
    """Real reviewer: a headless Claude that returns findings as JSON."""

    def __init__(self, executor):
        self.executor = executor

    def review(self, worktree_path: Path | str, sl) -> ReviewResult:
        result = self.executor.execute_prompt(str(worktree_path), REVIEW_PROMPT)
        return parse_findings(result.result_text)


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
