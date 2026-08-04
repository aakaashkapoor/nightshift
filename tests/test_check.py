"""Tests for the check runner (the per-repo verification gate)."""

import sys

from nightshift.check import run_check


def test_passing_check_captures_output(tmp_path) -> None:
    result = run_check("echo hello", tmp_path)
    assert result.passed is True
    assert "hello" in result.output
    assert result.returncode == 0
    assert result.timed_out is False


def test_failing_check_reports_failure_and_stderr(tmp_path) -> None:
    cmd = f'"{sys.executable}" -c "import sys; sys.stderr.write(\'errtext\'); sys.exit(2)"'
    result = run_check(cmd, tmp_path)
    assert result.passed is False
    assert result.returncode == 2
    assert "errtext" in result.output


def test_check_runs_in_given_cwd(tmp_path) -> None:
    run_check("echo 1 > ran_here.txt", tmp_path)
    assert (tmp_path / "ran_here.txt").exists()


def test_check_survives_undecodable_output(tmp_path) -> None:
    # Byte 0x9d is undefined in Windows' default cp1252; must not crash the reader.
    cmd = f'"{sys.executable}" -c "import sys; sys.stdout.buffer.write(bytes([0x9d])); sys.exit(0)"'
    result = run_check(cmd, tmp_path)
    assert result.passed is True  # decoded with errors=replace, no crash


def test_timeout_returns_timed_out(tmp_path) -> None:
    cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    result = run_check(cmd, tmp_path, timeout=1)
    assert result.timed_out is True
    assert result.passed is False
