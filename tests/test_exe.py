"""Tests for robust executable resolution."""

import os

from nightshift._exe import which


def test_which_finds_tool_on_path() -> None:
    assert which("git") is not None  # git is on PATH in the test env


def test_which_falls_back_to_extra_dirs(tmp_path) -> None:
    name = "faketool"
    exe = tmp_path / (f"{name}.exe" if os.name == "nt" else name)
    exe.write_text("", encoding="utf-8")
    assert which(name, extra_dirs=[str(tmp_path)]) == str(exe)


def test_which_returns_none_when_missing(tmp_path) -> None:
    assert which("definitely-not-a-real-tool-xyz", extra_dirs=[str(tmp_path)]) is None
