"""Tests for the pluggable adapters (Notifier / Tracker factories)."""

from nightshift.notifier import NullNotifier, SlackNotifier, build_notifier
from nightshift.tracker import NullTracker, build_tracker


class FakePoster:
    def __init__(self):
        self.posts = []

    def __call__(self, url, payload):
        self.posts.append((url, payload))


def test_slack_notifier_posts_to_webhook() -> None:
    poster = FakePoster()
    SlackNotifier("https://hooks.slack/abc", poster=poster).notify("blocked", "slice-001 stuck")
    url, payload = poster.posts[0]
    assert url == "https://hooks.slack/abc"
    assert "blocked" in payload["text"] and "slice-001 stuck" in payload["text"]


def test_build_notifier_selects_type() -> None:
    assert isinstance(build_notifier(None), NullNotifier)
    assert isinstance(build_notifier({"type": "none"}), NullNotifier)
    assert isinstance(build_notifier({"type": "unknown"}), NullNotifier)
    slack = build_notifier({"type": "slack", "webhook_url": "https://x"})
    assert isinstance(slack, SlackNotifier)
    assert slack.webhook_url == "https://x"


def test_null_tracker_is_noop() -> None:
    t = build_tracker({"type": "none"})
    assert isinstance(t, NullTracker)
    # No-ops must never raise.
    t.on_start("s")
    t.on_done("s")
    t.on_blocked("s", "reason")
