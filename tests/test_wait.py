"""Tests for nsh wait / nsh status and the daemon's per-repo wiring."""

import json

from typer.testing import CliRunner

from nightshift.cli import app
from nightshift.events import EventLog
from nightshift.paths import repo_home
from nightshift.state import StateStore
from nightshift.wait import run_status_cli, run_wait_cli, wait_for_attention


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_wait_starts_at_end_without_cursor(tmp_path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.emit("blocked", "issue-1")  # history before the first wait
    clock = Clock()

    def sleep(s):
        clock.t += s
        log.emit("review_ready", "issue-2", pr="u")

    hits = wait_for_attention(
        log, tmp_path / "wait.cursor", timeout=10, poll=1, sleep=sleep, clock=clock
    )
    assert [(e["kind"], e["slice"]) for e in hits] == [("review_ready", "issue-2")]


def test_wait_resumes_from_cursor_and_skips_noise(tmp_path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    cursor = tmp_path / "wait.cursor"
    cursor.write_text("0", encoding="utf-8")
    log.emit("slice_started", "issue-1")
    log.emit("blocked", "issue-1", reason="x")
    hits = wait_for_attention(log, cursor, timeout=0, poll=1, sleep=lambda s: None, clock=Clock())
    assert [e["kind"] for e in hits] == ["blocked"]
    again = wait_for_attention(log, cursor, timeout=0, poll=1, sleep=lambda s: None, clock=Clock())
    assert again == []


def test_wait_times_out(tmp_path) -> None:
    clock = Clock()

    def sleep(s):
        clock.t += s

    hits = wait_for_attention(
        EventLog(tmp_path / "e.jsonl"), tmp_path / "c", timeout=3, poll=1, sleep=sleep, clock=clock
    )
    assert hits == []
    assert clock.t >= 3


def test_wait_corrupt_cursor_starts_at_end(tmp_path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.emit("blocked", "issue-1")
    cursor = tmp_path / "wait.cursor"
    cursor.write_text("junk", encoding="utf-8")
    assert (
        wait_for_attention(log, cursor, timeout=0, poll=1, sleep=lambda s: None, clock=Clock())
        == []
    )


def _config(tmp_path, repo) -> object:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"repos:\n  demo:\n    path: {repo.as_posix()}\n    check: echo ok\n", encoding="utf-8"
    )
    return cfg


def test_run_status_cli_text_and_json(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _config(tmp_path, repo)
    home = repo_home("demo", tmp_path / "ns")
    StateStore(home / "state.json", clock=lambda: 5.0).set_slice("issue-1", "working")
    text = run_status_cli(repo, config_path=cfg, as_json=False, home_root=tmp_path / "ns", now=65.0)
    assert "issue-1" in text and "working" in text
    raw = json.loads(run_status_cli(repo, config_path=cfg, as_json=True, home_root=tmp_path / "ns"))
    assert raw["slices"]["issue-1"]["phase"] == "working"


def test_run_wait_cli_reads_repo_home(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _config(tmp_path, repo)
    home = repo_home("demo", tmp_path / "ns")
    (home).mkdir(parents=True)
    (home / "wait.cursor").write_text("0", encoding="utf-8")
    EventLog(home / "events.jsonl").emit("all_done")
    hits = run_wait_cli(repo, config_path=cfg, timeout=0, poll=1, home_root=tmp_path / "ns")
    assert [e["kind"] for e in hits] == ["all_done"]


def test_cli_wait_and_status(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        "nightshift.wait.run_wait_cli", lambda *a, **k: [{"kind": "blocked", "slice": "issue-1"}]
    )
    res = runner.invoke(app, ["wait"])
    assert res.exit_code == 0
    assert json.loads(res.output.strip()) == {"kind": "blocked", "slice": "issue-1"}
    monkeypatch.setattr("nightshift.wait.run_wait_cli", lambda *a, **k: [])
    res = runner.invoke(app, ["wait", "--timeout", "1"])
    assert res.exit_code == 2
    assert json.loads(res.output.strip()) == {"kind": "timeout"}
    monkeypatch.setattr("nightshift.wait.run_status_cli", lambda *a, **k: "daemon: running")
    res = runner.invoke(app, ["status", "--json"])
    assert res.exit_code == 0 and "daemon: running" in res.output


def test_run_daemon_cli_wires_repo_home(tmp_path) -> None:
    import subprocess

    from nightshift.daemon import run_daemon_cli
    from nightshift.executor import Executor, RunOutput

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    cfg = _config(tmp_path, repo)
    ex = Executor(runner=lambda a, c, s: RunOutput(0, "{}", ""))
    assert (
        run_daemon_cli(repo, config_path=cfg, once=True, executor=ex, home_root=tmp_path / "ns")
        == []
    )
