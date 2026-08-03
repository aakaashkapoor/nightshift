"""Notifiers — the optional out-of-band alert channel (SPEC §7, §15).

Notifiers are a *convenience layer*, never required: with no notifier, a blocked
slice's state still lives in the issue + logs. `NullNotifier` is the default. Slack /
Teams / etc. are future adapters behind this same interface.
"""

from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    def notify(self, event: str, message: str) -> None: ...


class NullNotifier:
    """Default: does nothing. The pipeline never depends on a notifier existing."""

    def notify(self, event: str, message: str) -> None:  # noqa: D401
        return None
