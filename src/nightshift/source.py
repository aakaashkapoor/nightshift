"""Slice sources — where the daemon discovers work (SPEC §4).

`LocalMdSource` is the default, offline, authoritative source: slices live as
`.md` files under a repo's ``.slices/`` directory. Status is written back into the
file (the issue *is* the state — SPEC §0). A ``github-issues`` source is a future
adapter behind the same shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .slice import Slice

READY = "ready"


class Source(Protocol):
    """The interface every slice source (local-md, github-issues) implements."""

    def list_all(self) -> list[Slice]: ...
    def list_ready(self) -> list[Slice]: ...
    def get(self, slice_id: str) -> Slice | None: ...
    def set_status(self, slice_id: str, status: str) -> None: ...
    def set_blocked(self, slice_id: str, reason: str) -> None: ...


class LocalMdSource:
    def __init__(self, repo_path: Path | str, slices_dir: str = ".slices"):
        self.root = Path(repo_path) / slices_dir

    def path_for(self, slice_id: str) -> Path:
        return self.root / f"{slice_id}.md"

    def list_all(self) -> list[Slice]:
        if not self.root.exists():
            return []
        slices: list[Slice] = []
        for path in sorted(self.root.glob("*.md")):
            try:
                slices.append(Slice.load(path))
            except (ValueError, OSError):
                continue  # not a slice (no frontmatter) / unreadable — skip
        return slices

    def list_ready(self) -> list[Slice]:
        return [s for s in self.list_all() if s.status == READY]

    def get(self, slice_id: str) -> Slice | None:
        path = self.path_for(slice_id)
        if not path.exists():
            return None
        return Slice.load(path)

    def save(self, sl: Slice) -> None:
        sl.save(self.path_for(sl.id))

    def set_status(self, slice_id: str, status: str) -> None:
        sl = self.get(slice_id)
        if sl is None:
            raise KeyError(f"no slice {slice_id!r} in {self.root}")
        sl.status = status
        self.save(sl)

    def set_blocked(self, slice_id: str, reason: str) -> None:
        """Mark blocked and record *why* into the issue body (SPEC §7)."""
        sl = self.get(slice_id)
        if sl is None:
            raise KeyError(f"no slice {slice_id!r} in {self.root}")
        sl.status = "blocked"
        if reason and "## Blocked" not in sl.body:
            sl.body = sl.body.rstrip() + f"\n\n## Blocked\n{reason}\n"
        self.save(sl)


def build_source(repo_cfg) -> Source:
    """Pick the source adapter from a repo's config (SPEC §4)."""
    if repo_cfg.source == "github-issues":
        from .github import GitHubIssuesSource

        return GitHubIssuesSource(repo_cfg.path)
    return LocalMdSource(repo_cfg.path)
