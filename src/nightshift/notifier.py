"""Notifiers — the optional out-of-band alert channel (SPEC §7, §15).

Notifiers are a *convenience layer*, never required: with no notifier, a blocked
slice's state still lives in the issue + logs. `NullNotifier` is the default. Slack /
Teams / etc. are future adapters behind this same interface.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Protocol


class Notifier(Protocol):
    def notify(self, event: str, message: str) -> None: ...


class NullNotifier:
    """Default: does nothing. The pipeline never depends on a notifier existing."""

    def notify(self, event: str, message: str) -> None:  # noqa: D401
        return None


def _default_poster(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)  # noqa: S310 (user-configured webhook)


class SlackNotifier:
    """Posts to a Slack incoming webhook. `poster` is injectable for tests."""

    def __init__(self, webhook_url: str, poster=None):
        self.webhook_url = webhook_url
        self.poster = poster or _default_poster

    def notify(self, event: str, message: str) -> None:
        self.poster(self.webhook_url, {"text": f":new_moon: *nightshift/{event}* — {message}"})


def build_notifier(config: dict | None) -> Notifier:
    """Factory: pick a notifier from a config block (SPEC §15).

    ``{type: none}`` (default) -> NullNotifier; ``{type: slack, webhook_url: ...}``
    -> SlackNotifier. Unknown types fall back to NullNotifier.
    """
    config = config or {}
    kind = config.get("type", "none")
    if kind == "slack":
        return SlackNotifier(config["webhook_url"])
    return NullNotifier()
