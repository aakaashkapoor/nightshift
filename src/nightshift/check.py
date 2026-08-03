"""Run a repo's ``check`` command — the verification gate (SPEC §6, §8).

The check is a single per-repo shell command (e.g. ``pytest && ruff check .``) that
must pass before a slice commits and again after it rebases onto ``main``. It runs
through the shell so operators can chain commands; stdout and stderr are captured
together so a failing gate is fully explained in one place.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 1800  # 30 minutes


@dataclass
class CheckResult:
    passed: bool
    output: str
    returncode: int | None
    timed_out: bool = False


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def run_check(
    command: str, cwd: Path | str, timeout: int = DEFAULT_TIMEOUT
) -> CheckResult:
    """Run ``command`` in ``cwd`` through the shell; capture combined output."""
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = _as_text(exc.stdout) + _as_text(exc.stderr)
        return CheckResult(passed=False, output=output, returncode=None, timed_out=True)

    output = _as_text(result.stdout) + _as_text(result.stderr)
    return CheckResult(
        passed=result.returncode == 0,
        output=output,
        returncode=result.returncode,
    )
