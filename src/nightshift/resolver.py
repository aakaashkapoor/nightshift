"""The conflict-resolution agent (`nightshift-resolve`, SPEC §6).

When a branch's rebase onto ``main`` conflicts, a fresh headless Claude is spawned
*in the conflicted worktree* to edit the code into a correct merged state — a real
agent, not a text-merge tool. It's called as ``resolver(worktree_path)`` by the
merge-train (``integrate_branch``); the git mechanics (add / rebase --continue /
re-check) stay in the pipeline.
"""

from __future__ import annotations

from pathlib import Path

from .executor import Executor

RESOLVE_PROMPT = (
    "You are Nightshift's conflict resolver. A git rebase of this branch onto the "
    "main branch has CONFLICTED. This worktree contains files with conflict markers "
    "(<<<<<<<, =======, >>>>>>>).\n\n"
    "Resolve every conflict so the result is correct and coherent — keep the intent "
    "of BOTH sides where they don't truly conflict; choose the correct behaviour "
    "where they do. Remove ALL conflict markers. Leave the working tree building and "
    "consistent.\n\n"
    "Do NOT run git, do NOT commit, do NOT stage — just edit the files to a clean, "
    "resolved state. The Nightshift harness will continue the rebase and re-run the "
    "check."
)


class Resolver:
    def __init__(self, executor: Executor):
        self.executor = executor

    def __call__(self, worktree_path: Path | str) -> None:
        self.executor.execute_prompt(str(worktree_path), RESOLVE_PROMPT)
