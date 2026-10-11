"""Tests for the per-repo home and the append-only event log."""

import json

from nightshift.events import ATTENTION, EventLog
from nightshift.paths import repo_home


def test_repo_home_default_and_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nightshift.paths.Path.home", lambda: tmp_path)
    assert repo_home("demo") == tmp_path / ".nightshift" / "repos" / "demo"
    assert repo_home("demo", root=tmp_path / "x") == tmp_path / "x" / "repos" / "demo"


def test_emit_appends_json_lines(tmp_path) -> None:
    log = EventLog(tmp_path / "sub" / "events.jsonl", clock=lambda: 12.5)
    rec = log.emit("blocked", "issue-3", reason="check failed")
    assert rec == {"ts": 12.5, "kind": "blocked", "slice": "issue-3", "reason": "check failed"}
    log.emit("all_done")
    lines = (tmp_path / "sub" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["blocked", "all_done"]
    assert "slice" not in json.loads(lines[1])


def test_read_from_returns_events_and_new_offset(tmp_path) -> None:
    log = EventLog(tmp_path / "events.jsonl", clock=lambda: 1.0)
    assert log.read_from(0) == ([], 0)  # no file yet
    log.emit("a")
    events, offset = log.read_from(0)
    assert [e["kind"] for e in events] == ["a"]
    log.emit("b")
    events, offset2 = log.read_from(offset)
    assert [e["kind"] for e in events] == ["b"]
    assert log.read_from(offset2) == ([], offset2)


def test_read_from_ignores_partial_trailing_line(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"kind": "a"}\n{"kind": "b"')
    events, offset = EventLog(path).read_from(0)
    assert [e["kind"] for e in events] == ["a"]
    assert offset == len('{"kind": "a"}\n')
    assert EventLog(path).read_from(offset) == ([], offset)


def test_read_from_restarts_when_file_shrank(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"kind": "a"}\n', encoding="utf-8")
    events, _ = EventLog(path).read_from(10_000)
    assert [e["kind"] for e in events] == ["a"]


def test_read_from_skips_corrupt_lines(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('garbage\n\n{"kind": "a"}\n', encoding="utf-8")
    events, _ = EventLog(path).read_from(0)
    assert [e["kind"] for e in events] == ["a"]


def test_end_offset(tmp_path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    assert log.end_offset() == 0
    log.emit("a")
    assert log.end_offset() == (tmp_path / "events.jsonl").stat().st_size


def test_attention_kinds() -> None:
    assert {
        "review_ready",
        "blocked",
        "paused_limit",
        "resumed",
        "daemon_exit",
        "all_done",
        "stalled",
    } == set(ATTENTION)
