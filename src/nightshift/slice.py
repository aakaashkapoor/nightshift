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

    def scope_hints(self) -> set[str]:
        """Path-like tokens under the '## Scope hints' section (advisory).

        Used only for the parallel-safety overlap check (SPEC §5) — prose words are
        dropped; a token counts only if it looks like a path (has '.', '/' or '\\').
        """
        hints: set[str] = set()
        collecting = False
        for line in self.body.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("## scope hints"):
                collecting = True
                continue
            if collecting:
                if stripped.startswith("## "):  # next section ends the block
                    break
                for token in re.split(r"[,\s]+", stripped):
                    token = token.strip("`-*() ")
                    if token and ("." in token or "/" in token or "\\" in token):
                        hints.add(token)
        return hints

    @classmethod
    def load(cls, path: Path | str) -> Slice:
        return cls.parse(Path(path).read_text(encoding="utf-8"))

    def save(self, path: Path | str) -> None:
        Path(path).write_text(self.to_markdown(), encoding="utf-8")
