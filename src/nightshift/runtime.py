"""Machine-local ephemera store (SPEC §8).

`runtime.json` holds per-slice bookkeeping that is meaningless on another machine
and must NOT pollute the issue: the agent's ``session_id`` (to resume), the branch,
attempt count. It is NOT a source of truth — it's rebuildable from scratch by
scanning worktrees + issues (`reconcile`), so a daemon restart loses nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_RUNTIME_PATH = Path.home() / ".nightshift" / "runtime.json"


class Runtime:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else DEFAULT_RUNTIME_PATH
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def record(self, slice_id: str, **fields) -> None:
        entry = self._data.setdefault(slice_id, {})
        entry.update({k: v for k, v in fields.items() if v is not None})
        self._save()

    def get(self, slice_id: str) -> dict:
        return self._data.get(slice_id, {})

    def forget(self, slice_id: str) -> None:
        if self._data.pop(slice_id, None) is not None:
            self._save()

    def reconcile(self, *, known_slice_ids: set[str], worktrees) -> None:
        """Drop entries whose slice is gone or whose worktree no longer exists."""
        stale = [
            sid for sid in self._data if sid not in known_slice_ids or not worktrees.exists(sid)
        ]
        for sid in stale:
            self._data.pop(sid, None)
        if stale:
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
