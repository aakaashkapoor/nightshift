"""The build agent's report (.nightshift/result.json), validated (v2 design, Step 2)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

OUTCOMES = ("done", "question", "needs_files")
RESULT_PATH = Path(".nightshift") / "result.json"


@dataclass
class AgentResult:
    outcome: str
    decisions: list[str] = field(default_factory=list)
    question: str = ""
    needs_files: list[str] = field(default_factory=list)
    notes: str = ""


def read_result(worktree: Path | str) -> tuple[AgentResult | None, str]:
    """(result, "") or (None, why it's invalid)."""
    path = Path(worktree) / RESULT_PATH
    if not path.exists():
        return None, "file not found"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"not valid JSON ({exc})"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    outcome = data.get("outcome")
    if outcome not in OUTCOMES:
        return None, f"outcome must be one of {', '.join(OUTCOMES)}"
    decisions = data.get("decisions") or []
    needs = data.get("needs_files") or []
    if not (isinstance(decisions, list) and all(isinstance(d, str) for d in decisions)):
        return None, "decisions must be a list of strings"
    if not (isinstance(needs, list) and all(isinstance(n, str) for n in needs)):
        return None, "needs_files must be a list of strings"
    question = data.get("question") or ""
    if outcome == "question" and not (isinstance(question, str) and question.strip()):
        return None, "outcome question needs a question"
    if outcome == "needs_files" and not needs:
        return None, "outcome needs_files needs needs_files"
    return AgentResult(outcome, decisions, str(question), needs, str(data.get("notes") or "")), ""


def clear_result(worktree: Path | str) -> None:
    (Path(worktree) / RESULT_PATH).unlink(missing_ok=True)
