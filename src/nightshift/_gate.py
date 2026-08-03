"""The `nsh-check` quality gate — run from the repo root before committing.

Runs, in order: Ruff lint, Ruff format-check, mypy, and pytest with 100% branch
coverage. Exits non-zero if any step fails. Excluded from coverage itself (it only
orchestrates other tools). Invoke as ``nsh-check`` or ``python -m nightshift._gate``.
"""

from __future__ import annotations

import subprocess
import sys

STEPS: list[tuple[str, list[str]]] = [
    ("ruff lint", [sys.executable, "-m", "ruff", "check", "."]),
    ("ruff format", [sys.executable, "-m", "ruff", "format", "--check", "."]),
    ("mypy", [sys.executable, "-m", "mypy"]),
    (
        "pytest + 100% coverage",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--cov=nightshift",
            "--cov-branch",
            "--cov-fail-under=100",
        ],
    ),
]


def main() -> int:
    failed: list[str] = []
    for name, cmd in STEPS:
        print(f"\n=== {name} ===", flush=True)
        if subprocess.run(cmd).returncode != 0:
            failed.append(name)
    print("\n" + "=" * 48)
    if failed:
        print("CHECK FAILED: " + ", ".join(failed))
        return 1
    print("CHECK PASSED — ruff + mypy + 100% coverage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
