"""Parse and write Nightshift slices (SPEC §3 standard format).

A slice is a YAML frontmatter block (between ``---`` fences) followed by a
markdown body. The frontmatter carries the logical state the daemon reads and
writes back (``status``, ``attempts``); the body is the no-code spec.

Write-back is done through ruamel.yaml round-tripping so that flipping a single
field (e.g. ``status``) leaves the rest of a hand-authored file — key order,
quoting, flow-style lists — untouched.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_FENCE = "---"
SIZES = ("S", "M", "L")

_yaml = YAML()
_yaml.preserve_quotes = True


def _split_frontmatter(text: str) -> tuple[Any, str]:
    """Return (frontmatter_map, body) or raise ValueError if malformed."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FENCE:
        raise ValueError("slice is missing its YAML frontmatter (opening '---')")
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            meta = _yaml.load("".join(lines[1:i])) or {}
            body = "".join(lines[i + 1 :])
            return meta, body
    raise ValueError("slice frontmatter is not terminated (missing closing '---')")


def append_to_section(body: str, heading: str, line: str) -> str:
    """Add ``line`` at the end of '## <heading>', creating the section if missing."""
    lines = body.rstrip("\n").splitlines()
    target = f"## {heading}".lower()
    for i, existing in enumerate(lines):
        if existing.strip().lower() == target:
            end = i + 1
            while end < len(lines) and not lines[end].strip().startswith("## "):
                end += 1
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            lines.insert(end, line)
            return "\n".join(lines) + "\n"
    return "\n".join([*lines, "", f"## {heading}", line]) + "\n"


def replace_section(body: str, heading: str, text: str) -> str:
    """Set the content of '## <heading>' to ``text``, appending the section if missing."""
    lines = body.rstrip("\n").splitlines()
    target = f"## {heading}".lower()
    for i, existing in enumerate(lines):
        if existing.strip().lower() == target:
            end = i + 1
            while end < len(lines) and not lines[end].strip().startswith("## "):
                end += 1
            tail = ["", *lines[end:]] if end < len(lines) else []
            return "\n".join([*lines[: i + 1], text, *tail]) + "\n"
    return "\n".join([*lines, "", f"## {heading}", text]) + "\n"


@dataclass
class Slice:
    """One unit of work. See SPEC §3."""

    id: str
    title: str
    status: str
    depends_on: list[str] = field(default_factory=list)
    attempts: int = 0
    jira: str | None = None
    body: str = ""
    _meta: Any = None  # ruamel map retained for format-preserving write-back

    @classmethod
    def parse(cls, text: str) -> Slice:
        meta, body = _split_frontmatter(text)
        return cls(
            id=meta.get("id"),
            title=meta.get("title"),
            status=meta.get("status"),
            depends_on=list(meta.get("depends_on") or []),
            attempts=int(meta.get("attempts", 0)),
            jira=meta.get("jira"),
            body=body,
            _meta=meta,
        )

    def to_markdown(self) -> str:
        # Sync the mutable logical fields back into the preserved map.
        self._meta["status"] = self.status
        self._meta["attempts"] = self.attempts
        buf = io.StringIO()
        _yaml.dump(self._meta, buf)
        return f"{_FENCE}\n{buf.getvalue()}{_FENCE}\n{self.body}"

    def touches(self) -> set[str]:
        """Paths this slice will change, for the scheduler's lock (v2 design, Step 1).

        Read from '## Touches', or the older '## Scope hints' if there is no Touches
        section. Accepts list items or comma-separated paths. The block ends at the next
        '## ' heading or at the first blank line after a path, so a closing prose
        paragraph is never read as paths. A token counts only if it looks like a path.
        """
        lines = self.body.splitlines()
        heads = [line.strip().lower() for line in lines]
        if "## touches" in heads:
            start = heads.index("## touches")
        elif "## scope hints" in heads:
            start = heads.index("## scope hints")
        else:
            return set()
        paths: set[str] = set()
        for line in lines[start + 1 :]:
            stripped = line.strip()
            if stripped.startswith("## "):
                break
            if not stripped:
                if paths:
                    break
                continue
            for token in re.split(r"[,\s]+", stripped):
                token = token.strip("`-*() ")
                if token and ("." in token or "/" in token or "\\" in token):
                    paths.add(token)
        return paths

    def section(self, name: str) -> str | None:
        """Text under '## <name>' (case-insensitive) up to the next '## ' heading.

        None if the heading is absent; '' if present but empty.
        """
        lines = self.body.splitlines()
        target = f"## {name}".lower()
        for i, line in enumerate(lines):
            if line.strip().lower() == target:
                out = []
                for nxt in lines[i + 1 :]:
                    if nxt.strip().startswith("## "):
                        break
                    out.append(nxt)
                return "\n".join(out).strip()
        return None

    def size(self) -> str:
        """S, M or L from '## Size'; M when missing or unreadable."""
        raw = (self.section("Size") or "").split()
        token = raw[0].upper() if raw else ""
        return token if token in SIZES else "M"

    def scope_hints(self) -> set[str]:
        """Old name for ``touches()``."""
        return self.touches()

    @classmethod
    def load(cls, path: Path | str) -> Slice:
        return cls.parse(Path(path).read_text(encoding="utf-8"))

    def save(self, path: Path | str) -> None:
        Path(path).write_text(self.to_markdown(), encoding="utf-8")
