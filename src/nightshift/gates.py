"""Deterministic content gate: changed text files must be UTF-8 with no conflict markers.

Cheap, plain-Python checks that run after the repo's check passes and before review
(field note #16): a stray cp1252 byte or a left-over ``<<<<<<<`` costs nothing to
find here and a lot to find in review. Problems go back to the agent like a red check.
"""

from __future__ import annotations

from pathlib import Path

from .conflicts import _git

_BINARY_PROBE = 8192


def _changed_files(worktree: Path, base_branch: str) -> list[str]:
    """Files this slice added or modified, committed or not, relative to where it branched."""
    _git(worktree, "add", "-A", "-N")  # so new files show up in the diff
    base = _git(worktree, "merge-base", base_branch, "HEAD").stdout.strip()
    out = _git(worktree, "diff", "-z", "--name-only", "--diff-filter=d", base).stdout
    return [name for name in out.split("\0") if name]


def _is_marker(line: bytes, has_open: bool) -> bool:
    """A bare ======= is a marker only in a file that also opens a conflict.

    Alone it is a legitimate Markdown setext heading underline.
    """
    line = line.rstrip(b"\r")
    return line.startswith((b"<<<<<<< ", b">>>>>>> ")) or (has_open and line == b"=======")


def content_problems(worktree: Path | str, base_branch: str = "main") -> list[str]:
    """One message per problem in the changed text files; empty when all is well."""
    root = Path(worktree)
    problems: list[str] = []
    for name in _changed_files(root, base_branch):
        path = root / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:_BINARY_PROBE]:
            continue  # binary
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append(
                f"{name}: not UTF-8 (byte 0x{data[exc.start]:02X} at offset {exc.start})"
            )
        lines = data.split(b"\n")
        has_open = any(line.startswith(b"<<<<<<< ") for line in lines)
        for number, line in enumerate(lines, start=1):
            if _is_marker(line, has_open):
                problems.append(f"{name}:{number}: conflict marker")
    return problems
