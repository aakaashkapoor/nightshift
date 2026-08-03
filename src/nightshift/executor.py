"""Spawn a local headless Claude Code to implement a slice (SPEC §6, Work phase).

This is the core: for a given worktree + slice, run ``claude -p`` non-interactively
so it edits files autonomously, then return its result (incl. the session id, used
later to *resume* a blocked slice without redoing work — SPEC §7).

Design notes:
- The prompt is passed on **stdin**, not as a CLI arg, so long multi-line specs
  never hit shell/argv quoting (important on Windows).
- ``--output-format json`` gives us ``result`` + ``session_id``.
- ``--permission-mode bypassPermissions`` keeps the agent from ever stalling on a
  permission prompt (it runs in an isolated worktree — SPEC §0 "never block").
- The subprocess call is behind an injectable ``runner`` so all logic here is unit
  tested; the default runner is thin and gets its live smoke test at the 1.8 demo.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

CLAUDE_BIN = "claude"


@dataclass
class RunOutput:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class ExecResult:
    succeeded: bool
    session_id: str | None
    result_text: str
    output: str
    returncode: int


Runner = Callable[[list[str], str, str], RunOutput]


def _default_runner(argv: list[str], cwd: str, stdin_text: str) -> RunOutput:
    """Run the real ``claude`` CLI. Prompt on stdin; JSON on stdout.

    Not unit tested (needs a live, authenticated Claude) — smoke-tested at 1.8.
    """
    exe = shutil.which(argv[0])
    if exe is None:
        raise FileNotFoundError(
            f"'{argv[0]}' not found on PATH — is Claude Code installed?"
        )
    cmd = [exe, *argv[1:]]
    # On Windows the CLI may be a .cmd/.bat shim, which must go through the shell.
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        proc = subprocess.run(
            subprocess.list2cmdline(cmd),
            cwd=cwd,
            input=stdin_text,
            capture_output=True,
            text=True,
            shell=True,
        )
    else:
        proc = subprocess.run(
            cmd, cwd=cwd, input=stdin_text, capture_output=True, text=True
        )
    return RunOutput(proc.returncode, proc.stdout, proc.stderr)


class Executor:
    def __init__(
        self,
        runner: Runner = _default_runner,
        permission_mode: str = "bypassPermissions",
        model: str | None = None,
        claude_bin: str = CLAUDE_BIN,
    ):
        self.runner = runner
        self.permission_mode = permission_mode
        self.model = model
        self.claude_bin = claude_bin

    def build_argv(self, resume_session: str | None = None) -> list[str]:
        argv = [
            self.claude_bin,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            self.permission_mode,
        ]
        if self.model:
            argv += ["--model", self.model]
        if resume_session:
            argv += ["--resume", resume_session]
        return argv

    def build_prompt(self, sl) -> str:
        return (
            "You are Nightshift, implementing ONE self-contained unit of work in "
            "this repository.\n"
            "Work only within this working directory. Implement the task fully and "
            "correctly against the acceptance criteria below.\n"
            "Do NOT commit, push, or create branches — the Nightshift harness "
            "handles all git.\n\n"
            f"# {sl.title}\n\n"
            f"{sl.body.strip()}\n"
        )

    def execute(
        self, cwd: Path | str, sl, resume_session: str | None = None
    ) -> ExecResult:
        argv = self.build_argv(resume_session)
        prompt = self.build_prompt(sl)
        out = self.runner(argv, str(cwd), prompt)
        return self._parse(out)

    def _parse(self, out: RunOutput) -> ExecResult:
        session_id: str | None = None
        result_text = out.stdout
        try:
            data = json.loads(out.stdout)
            if isinstance(data, dict):
                session_id = data.get("session_id")
                result_text = data.get("result", out.stdout)
        except (json.JSONDecodeError, TypeError):
            pass
        return ExecResult(
            succeeded=out.returncode == 0,
            session_id=session_id,
            result_text=result_text,
            output=out.stdout,
            returncode=out.returncode,
        )
