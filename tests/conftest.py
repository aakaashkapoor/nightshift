"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch) -> None:
    """Point the home directory at a temp dir so no test writes to the real ~/.nightshift."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
