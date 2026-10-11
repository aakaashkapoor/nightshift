"""What the build agent is told (v2 design, Step 2). One variant per issue size."""

from __future__ import annotations

COMMON = """You are Nightshift, implementing ONE GitHub issue in this repository. This directory is a git worktree just for this issue.

Rules:
- Read AGENTS.md or CLAUDE.md first if the repo has one.
- Do NOT commit, push, create branches, or edit the issue. The harness does all git and status.
- Existing files you may modify: {files}. You may create new files anywhere. If you must modify another existing file, stop and report it in result.json as needs_files.
- If the issue doesn't settle a choice: decide it yourself when it only affects this issue's own files, and list it under decisions. If it affects the issue's Contract (names, types, APIs or files other issues use), stop and ask (outcome "question").
- Finish by writing .nightshift/result.json:
  {{"outcome": "done" | "question" | "needs_files", "decisions": ["..."], "question": "...", "needs_files": ["path"], "notes": "..."}}
"""

BY_SIZE = {
    "S": "This is a SMALL change. Implement it directly. No plan file, no subagents.",
    "M": (
        "This is a MEDIUM change. First write .nightshift/plan.md: the approach, the files "
        "you will touch, and a short task checklist (use the superpowers writing-plans "
        "skill if you have it). Then implement the plan in this session, test-first. You "
        "may use subagents for independent tasks."
    ),
    "L": (
        "This is a LARGE change. Write .nightshift/plan.md from the issue's '## Subtasks' "
        "(one section per subtask: files, names, how it is verified). Then execute it with "
        "subagent-driven development (superpowers:subagent-driven-development if you have "
        "it): one subagent per subtask, and review each one before starting the next."
    ),
}

CHECK_FAILED = """The repository's check failed after your change. Fix the cause; don't weaken or skip tests.

Command: {command}
Output (last part):
{output}

Update .nightshift/result.json when you're done."""

REPAIR = """.nightshift/result.json is missing or invalid: {error}
Write it now (valid JSON, the schema from your instructions). Do nothing else."""


FILES_PROMPT = (
    "You may now also modify: {files}. Continue the task from where you stopped, "
    "then update .nightshift/result.json."
)

ANSWER_PROMPT = (
    "The owner answered your question:\n\n{answer}\n\n"
    "Continue the task from where you stopped, then update .nightshift/result.json."
)


def build_prompt(sl) -> str:
    touches = sorted(sl.touches())
    files = ", ".join(touches) if touches else "any (the issue lists no Touches)"
    return (
        COMMON.format(files=files)
        + "\n"
        + BY_SIZE[sl.size()]
        + f"\n\n# {sl.title}\n\n{sl.body.strip()}\n"
    )


def trim_output(output: str, max_lines: int = 80, max_chars: int = 6000) -> str:
    """The end of a check's output, where the failures are."""
    tail = "\n".join(output.strip().splitlines()[-max_lines:])
    return tail[-max_chars:]


def check_failed_prompt(command: str, output: str) -> str:
    return CHECK_FAILED.format(command=command, output=trim_output(output))


def repair_prompt(error: str) -> str:
    return REPAIR.format(error=error)
