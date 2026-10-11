"""Pause / unpause / stop via control.json."""

from typer.testing import CliRunner

from nightshift.cli import app
from nightshift.control import Control
from nightshift.paths import repo_home
from nightshift.wait import run_control_cli


def test_defaults_when_missing_or_corrupt(tmp_path) -> None:
    c = Control(tmp_path / "control.json")
    assert (c.paused, c.stop) == (False, False)
    (tmp_path / "control.json").write_text("{nope", encoding="utf-8")
    assert (c.paused, c.stop) == (False, False)


def test_set_and_read_back(tmp_path) -> None:
    c = Control(tmp_path / "home" / "control.json")
    c.set(paused=True)
    assert Control(tmp_path / "home" / "control.json").paused is True
    c.set(stop=True)
    assert (c.paused, c.stop) == (True, True)
    c.set(paused=False, stop=False)
    assert (c.paused, c.stop) == (False, False)


def _config(tmp_path, repo):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"repos:\n  demo:\n    path: {repo.as_posix()}\n    check: echo ok\n",
        encoding="utf-8",
    )
    return cfg


def test_run_control_cli(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _config(tmp_path, repo)
    root = tmp_path / "ns"
    assert (
        run_control_cli(repo, "pause", config_path=cfg, home_root=root)
        == "paused: no new slices will start"
    )
    assert Control(repo_home("demo", root) / "control.json").paused is True
    assert run_control_cli(repo, "unpause", config_path=cfg, home_root=root) == "unpaused"
    assert run_control_cli(repo, "stop", config_path=cfg, home_root=root) == (
        "stopping: the daemon exits after the running slices finish"
    )
    c = Control(repo_home("demo", root) / "control.json")
    assert (c.paused, c.stop) == (False, True)


def test_cli_commands(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(
        "nightshift.wait.run_control_cli",
        lambda repo, action, **k: seen.append(action) or action,
    )
    runner = CliRunner()
    for cmd in ("pause", "unpause", "stop"):
        res = runner.invoke(app, [cmd])
        assert res.exit_code == 0 and cmd in res.output
    assert seen == ["pause", "unpause", "stop"]
