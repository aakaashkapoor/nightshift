"""Tests for the CLI command wiring (Typer), with the heavy calls patched."""

from typer.testing import CliRunner

from nightshift.cli import app
from nightshift.pipeline import SliceResult

runner = CliRunner()


def _done(sid="s"):
    return SliceResult(sid, "done", 1, "abcdef1234", "nightshift/" + sid, "merged")


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "nightshift" in result.stdout


def test_init_registers_repo(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    result = runner.invoke(
        app, ["init", "--repo", str(tmp_path), "--config", str(cfg), "--name", "x"]
    )
    assert result.exit_code == 0
    assert cfg.exists()
    assert "pytest" in result.stdout  # auto-detected check


def test_init_with_symlink(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    result = runner.invoke(
        app,
        [
            "init",
            "--repo",
            str(tmp_path),
            "--config",
            str(cfg),
            "--name",
            "y",
            "--check",
            "npm run check",
            "--symlink",
            "node_modules, .venv",
        ],
    )
    assert result.exit_code == 0
    from nightshift.config import Config

    assert Config.load(cfg).repo("y").symlink_dirs == ["node_modules", ".venv"]


def test_init_with_push(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    result = runner.invoke(
        app, ["init", "--repo", str(tmp_path), "--config", str(cfg), "--name", "z", "--push"]
    )
    assert result.exit_code == 0
    from nightshift.config import Config

    assert Config.load(cfg).repo("z").push is True


def test_run_command_done(monkeypatch) -> None:
    monkeypatch.setattr("nightshift.pipeline.run_slice_cli", lambda *a, **k: _done())
    result = runner.invoke(app, ["run", "s", "--config", "c.yaml"])
    assert result.exit_code == 0
    assert "DONE" in result.stdout


def test_run_command_blocked_exits_1(monkeypatch) -> None:
    blocked = SliceResult("s", "blocked", 1, None, "br", "stuck")
    monkeypatch.setattr("nightshift.pipeline.run_slice_cli", lambda *a, **k: blocked)
    result = runner.invoke(app, ["run", "s"])
    assert result.exit_code == 1


def test_resume_command(monkeypatch) -> None:
    monkeypatch.setattr("nightshift.pipeline.run_resume_cli", lambda *a, **k: _done())
    result = runner.invoke(app, ["resume", "s"])
    assert result.exit_code == 0
    assert "DONE" in result.stdout


def test_daemon_command_with_results(monkeypatch) -> None:
    monkeypatch.setattr(
        "nightshift.daemon.run_daemon_cli", lambda *a, **k: [_done("a"), _done("b")]
    )
    result = runner.invoke(app, ["daemon", "--once"])
    assert result.exit_code == 0
    assert result.stdout.count("DONE") == 2


def test_daemon_command_no_results(monkeypatch) -> None:
    monkeypatch.setattr("nightshift.daemon.run_daemon_cli", lambda *a, **k: [])
    result = runner.invoke(app, ["daemon", "--once"])
    assert "no runnable slices" in result.stdout
