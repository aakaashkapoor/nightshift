"""The issue linter (v2 design, Step 1): cheap, deterministic, no LLM."""

from __future__ import annotations

from .slice import SIZES, Slice


def lint(sl: Slice) -> list[str]:
    problems = []
    if not sl.section("Goal"):
        problems.append("missing '## Goal'")
    if not (sl.section("Acceptance") or sl.section("Acceptance criteria")):
        problems.append("missing '## Acceptance'")
    raw = (sl.section("Size") or "").split()
    if raw and raw[0].upper() not in SIZES:
        problems.append(f"'## Size' must be S, M or L (got {raw[0]!r})")
    if sl.size() == "L" and not sl.section("Subtasks"):
        problems.append("size L needs '## Subtasks'")
    return problems
