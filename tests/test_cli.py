"""Tests for the Nightshift CLI."""

from typer.testing import CliRunner

from nightshift import __version__
from nightshift.cli import app

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
