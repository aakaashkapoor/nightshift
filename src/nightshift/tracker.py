"""Trackers — optional external issue-tracker sync (SPEC §15).

A Tracker mirrors a slice's lifecycle into an external system (Jira, Linear, …).
Optional and off by default (`NullTracker`). Behind an interface so adapters can be
contributed without touching the core; Jira/Linear are future adapters.
"""

from __future__ import annotations

from typing import Protocol


class Tracker(Protocol):
    def on_start(self, slice_id: str) -> None: ...
    def on_done(self, slice_id: str) -> None: ...
    def on_blocked(self, slice_id: str, reason: str) -> None: ...


class NullTracker:
    """Default: no external tracker."""

    def on_start(self, slice_id: str) -> None:
        return None

    def on_done(self, slice_id: str) -> None:
        return None

    def on_blocked(self, slice_id: str, reason: str) -> None:
        return None


def build_tracker(config: dict | None) -> Tracker:
    """Factory. ``{type: none}`` -> NullTracker. Jira/Linear are future adapters."""
    # (kind := (config or {}).get("type", "none")) intentionally only "none" for now.
    return NullTracker()
