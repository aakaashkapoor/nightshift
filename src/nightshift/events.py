"""The daemon's append-only event log (v2 design, Step 6).

Every notable thing the daemon does is one JSON line in ``events.jsonl``. The master
agent never parses the human log: ``nsh wait`` reads this file from a saved byte
offset and wakes on the ``ATTENTION`` kinds.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

# Kinds that need the master agent. Others (slice_started, merged, ...) are history.
ATTENTION = frozenset(
    {"review_ready", "blocked", "paused_limit", "resumed", "daemon_exit", "all_done", "stalled"}
)


class EventLog:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock

    def emit(self, kind: str, slice_id: str | None = None, **data) -> dict:
        record: dict = {"ts": round(self.clock(), 3), "kind": kind}
        if slice_id:
            record["slice"] = slice_id
        record.update(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return record

    def end_offset(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def read_from(self, offset: int) -> tuple[list[dict], int]:
        """Complete events after byte ``offset``, and the offset to continue from."""
        if not self.path.exists():
            return [], 0
        if offset > self.path.stat().st_size:  # file was replaced/truncated
            offset = 0
        with self.path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        end = data.rfind(b"\n")
        if end < 0:
            return [], offset
        events = []
        for line in data[: end + 1].decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events, offset + end + 1
