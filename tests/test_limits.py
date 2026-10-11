"""Tests for usage-limit detection and reset-time lookup."""

import json

from nightshift.limits import (
    FALLBACK_WAIT,
    RESET_GRACE,
    UsageLimitReached,
    is_usage_limit,
    reset_time,
    resume_at,
)


def test_detects_session_limit_text() -> None:
    assert is_usage_limit("You've hit your session limit · resets 2:30pm (America/Los_Angeles)")


def test_detects_weekly_and_legacy_text() -> None:
    assert is_usage_limit("You've hit your weekly limit · resets Oct 14")
    assert is_usage_limit("Claude AI usage limit reached|1791581400")


def test_ignores_normal_text() -> None:
    assert not is_usage_limit("Implemented the uploader retry.")
    assert not is_usage_limit("")


def _write_transcript(root, session_id, lines) -> None:
    d = root / "projects" / "C--some-worktree"
    d.mkdir(parents=True)
    (d / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_reset_time_reads_resets_at_from_transcript(tmp_path) -> None:
    record = {
        "type": "assistant",
        "apiError": "usage_limit_reached",
        "apiErrorParams": {"rate_limit_info": {"resetsAt": 1791581400}},
    }
    _write_transcript(tmp_path, "sess-1", ['{"type":"user"}', "not json", json.dumps(record)])
    assert reset_time("sess-1", root=tmp_path) == 1791581400.0


def test_reset_time_skips_broken_matching_lines(tmp_path) -> None:
    good = json.dumps(
        {"apiError": "usage_limit_reached", "apiErrorParams": {"rate_limit_info": {"resetsAt": 5}}}
    )
    no_time = json.dumps({"apiError": "usage_limit_reached", "apiErrorParams": None})
    _write_transcript(tmp_path, "s2", [good, "usage_limit_reached {broken", no_time])
    assert reset_time("s2", root=tmp_path) == 5.0


def test_reset_time_none_when_missing(tmp_path) -> None:
    assert reset_time(None, root=tmp_path) is None
    assert reset_time("nope", root=tmp_path) is None
    _write_transcript(tmp_path, "s3", ['{"type":"user"}'])
    assert reset_time("s3", root=tmp_path) is None


def test_reset_time_default_root_uses_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    record = {
        "apiError": "usage_limit_reached",
        "apiErrorParams": {"rate_limit_info": {"resetsAt": 9}},
    }
    _write_transcript(tmp_path, "s4", [json.dumps(record)])
    assert reset_time("s4") == 9.0


def test_reset_time_default_root_is_home_dot_claude(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr("nightshift.limits.Path.home", lambda: tmp_path)
    record = {
        "apiError": "usage_limit_reached",
        "apiErrorParams": {"rate_limit_info": {"resetsAt": 7}},
    }
    _write_transcript(tmp_path / ".claude", "s5", [json.dumps(record)])
    assert reset_time("s5") == 7.0


def test_resume_at_uses_known_future_reset() -> None:
    assert resume_at(1000.0, now=400.0) == 1000.0 + RESET_GRACE


def test_resume_at_falls_back_when_unknown_or_past() -> None:
    assert resume_at(None, now=400.0) == 400.0 + FALLBACK_WAIT
    assert resume_at(100.0, now=400.0) == 400.0 + FALLBACK_WAIT


def test_usage_limit_reached_carries_session() -> None:
    exc = UsageLimitReached("sess-9")
    assert exc.session_id == "sess-9"
    assert "usage limit" in str(exc)
