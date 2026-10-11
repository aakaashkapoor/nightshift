"""The daemon's live state (v2 design, Step 0) and its `nsh status` rendering.

``state.json`` is rewritten atomically on every change, so a reader never sees a half
file. It's a view of what the daemon is doing now, not a source of truth: the issue
labels stay authoritative.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path


def _empty() -> dict:
    return {"daemon": {}, "slices": {}}


class StateStore:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self.data = _empty()
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = {"daemon": loaded["daemon"], "slices": loaded["slices"]}
            except (json.JSONDecodeError, KeyError, TypeError, OSError):
                self.data = _empty()

    def set_daemon(self, **fields) -> None:
        self.data["daemon"].update(fields)
        self.data["daemon"]["updated"] = self.clock()
        self._save()

    def set_slice(self, slice_id: str, phase: str, **fields) -> None:
        now = self.clock()
        prev = self.data["slices"].get(slice_id) or {}
        since = prev["since"] if prev.get("phase") == phase else now
        entry = {"phase": phase, "since": since}
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.data["slices"][slice_id] = entry
        self._save()

    def drop_slice(self, slice_id: str) -> None:
        if self.data["slices"].pop(slice_id, None) is not None:
            self._save()

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self.data))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s % 3600 >= 60 else f"{s // 3600}h"
    return f"{s // 86400}d"


def render_status(snapshot: dict, now: float) -> str:
    daemon = snapshot.get("daemon") or {}
    status = daemon.get("status")
    if not status:
        head = "daemon: not started"
    elif status == "paused_limit":
        head = f"daemon: paused_limit (resumes in {_ago(daemon.get('paused_until', now) - now)})"
    else:
        head = f"daemon: {status} (pid {daemon.get('pid', '?')}, updated {_ago(now - daemon.get('updated', now))} ago)"
    slices = snapshot.get("slices") or {}
    if not slices:
        return f"{head}\nno slices in flight"
    lines = [head, "slices:"]
    for sid in sorted(slices, key=lambda k: (slices[k].get("since", 0) * -1, k)):
        entry = slices[sid]
        extra = entry.get("pr") or entry.get("detail") or ""
        lines.append(
            f"  {sid:<12} {entry.get('phase', '?'):<11} {_ago(now - entry.get('since', now)):>6}  {extra}".rstrip()
        )
    return "\n".join(lines)
