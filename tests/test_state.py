"""Tests for the daemon's state file and its human rendering."""

import json

from nightshift.state import StateStore, render_status


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_new_store_is_empty(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    assert store.snapshot() == {"daemon": {}, "slices": {}}


def test_set_daemon_and_slice_persist(tmp_path) -> None:
    clock = Clock()
    path = tmp_path / "d" / "state.json"
    store = StateStore(path, clock=clock)
    store.set_daemon(status="running", pid=42)
    store.set_slice("issue-1", "working")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["daemon"] == {"status": "running", "pid": 42, "updated": 1000.0}
    assert data["slices"]["issue-1"] == {"phase": "working", "since": 1000.0}
    assert StateStore(path).snapshot() == data  # reload


def test_same_phase_keeps_since_new_phase_resets(tmp_path) -> None:
    clock = Clock()
    store = StateStore(tmp_path / "state.json", clock=clock)
    store.set_slice("issue-1", "working")
    clock.t = 1100.0
    store.set_slice("issue-1", "working", detail="attempt 2")
    assert store.snapshot()["slices"]["issue-1"]["since"] == 1000.0
    store.set_slice("issue-1", "blocked", detail="check failed", pr=None)
    entry = store.snapshot()["slices"]["issue-1"]
    assert entry == {"phase": "blocked", "since": 1100.0, "detail": "check failed"}


def test_drop_slice(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.set_slice("issue-1", "working")
    store.drop_slice("issue-1")
    store.drop_slice("missing")
    assert store.snapshot()["slices"] == {}


def test_corrupt_file_starts_empty(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{nope", encoding="utf-8")
    assert StateStore(path).snapshot() == {"daemon": {}, "slices": {}}


def test_render_status_running() -> None:
    snap = {
        "daemon": {"status": "running", "pid": 7, "updated": 990.0},
        "slices": {
            "issue-2": {"phase": "in-review", "since": 1000.0 - 7200, "pr": "https://pr/2"},
            "issue-1": {"phase": "working", "since": 1000.0 - 65},
            "issue-3": {"phase": "blocked", "since": 1000.0 - 90000, "detail": "check failed"},
        },
    }
    text = render_status(snap, now=1000.0)
    assert "daemon: running (pid 7, updated 10s ago)" in text
    lines = text.splitlines()
    assert lines[1] == "slices:"
    assert "issue-1" in lines[2] and "working" in lines[2] and "1m" in lines[2]
    assert "issue-2" in lines[3] and "2h" in lines[3] and "https://pr/2" in lines[3]
    assert "issue-3" in lines[4] and "1d" in lines[4] and "check failed" in lines[4]


def test_render_status_paused_and_empty() -> None:
    snap = {"daemon": {"status": "paused_limit", "paused_until": 1000.0 + 3725}, "slices": {}}
    text = render_status(snap, now=1000.0)
    assert "daemon: paused_limit (resumes in 1h02m)" in text
    assert "no slices in flight" in text


def test_render_status_unknown_daemon() -> None:
    assert render_status({"daemon": {}, "slices": {}}, now=0.0).startswith("daemon: not started")
