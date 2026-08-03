"""Tests for the Nightshift config loader (SPEC §8)."""

import pytest

from nightshift.config import Config

SAMPLE = """
defaults:
  max_parallel: 5
  executor: local-headless
  notifier: { type: none }

repos:
  my-app:
    path: ~/code/my-app
    source: github-issues
    check: "make test"
    base_branch: develop
    pr: { enabled: true, automerge: true }
  other-app:
    path: ~/code/other
    source: local-md
    check: "pytest && ruff check ."
"""


def test_resolve_repo_by_name_returns_check() -> None:
    cfg = Config.parse(SAMPLE)
    assert cfg.repo("my-app").check == "make test"
    assert cfg.repo("other-app").check == "pytest && ruff check ."


def test_field_defaults_applied_when_omitted() -> None:
    cfg = Config.parse(SAMPLE)
    assert cfg.repo("other-app").base_branch == "main"  # omitted -> default
    assert cfg.repo("my-app").base_branch == "develop"  # explicit
    assert cfg.repo("other-app").pr.get("enabled") is False  # pr omitted -> off


def test_global_default_max_parallel_applied() -> None:
    cfg = Config.parse(SAMPLE)
    assert cfg.repo("my-app").max_parallel == 5


def test_source_resolved() -> None:
    cfg = Config.parse(SAMPLE)
    assert cfg.repo("my-app").source == "github-issues"
    assert cfg.repo("other-app").source == "local-md"


def test_env_interpolation_in_check(monkeypatch) -> None:
    monkeypatch.setenv("EXTRA_FLAGS", "--maxfail=1")
    text = 'repos:\n  app:\n    path: ~/x\n    check: "pytest ${EXTRA_FLAGS}"\n'
    cfg = Config.parse(text)
    assert cfg.repo("app").check == "pytest --maxfail=1"


def test_lookup_by_path(tmp_path) -> None:
    text = f'repos:\n  app:\n    path: "{tmp_path.as_posix()}"\n    check: "make"\n'
    cfg = Config.parse(text)
    assert cfg.repo(str(tmp_path)).name == "app"


def test_unknown_repo_raises() -> None:
    cfg = Config.parse(SAMPLE)
    with pytest.raises(ValueError):
        cfg.repo("does-not-exist")


def test_missing_check_raises() -> None:
    cfg = Config.parse("repos:\n  app:\n    path: ~/x\n")
    with pytest.raises(ValueError):
        cfg.repo("app")


def test_load_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "nope.yaml")
