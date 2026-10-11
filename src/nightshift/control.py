"""Operator controls for a running daemon: pause, unpause, stop after current work.

The CLI writes ``control.json`` in the repo home; the daemon reads it on every poll.
A file (not a signal) works the same on Windows and survives daemon restarts.
"""

from __future__ import annotations

import json
from pathlib import Path


class Control:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def paused(self) -> bool:
        return bool(self._read().get("paused", False))

    @property
    def stop(self) -> bool:
        return bool(self._read().get("stop", False))

    def set(self, **flags: bool) -> None:
        data = self._read()
        data.update(flags)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")
