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


def _cfg(entry: str) -> Config:
    return Config.parse(f"repos:\n  r:\n    path: .\n    check: x\n{entry}")


def test_babysit_defaults_on_for_github_issues() -> None:
    assert _cfg("    source: github-issues\n").repo("r").babysit is True


def test_babysit_defaults_off_for_local_md() -> None:
    assert _cfg("    source: local-md\n").repo("r").babysit is False


def test_babysit_can_be_turned_off() -> None:
    rc = _cfg("    source: github-issues\n    babysit: false\n").repo("r")
    assert rc.babysit is False


def test_babysit_on_local_md_is_an_error() -> None:
    with pytest.raises(ValueError, match="babysit needs PRs"):
        _cfg("    source: local-md\n    babysit: true\n").repo("r")


def test_model_and_effort_default_to_none() -> None:
    repo = Config.parse(SAMPLE).repo("my-app")
    assert repo.model is None and repo.effort is None


def test_model_and_effort_from_defaults_then_repo() -> None:
    text = """
defaults: { model: claude-opus-5-5, effort: high }
repos:
  a: { path: ., check: "true" }
  b: { path: ., check: "true", effort: xhigh }
"""
    cfg = Config.parse(text)
    assert (cfg.repo("a").model, cfg.repo("a").effort) == ("claude-opus-5-5", "high")
    assert (cfg.repo("b").model, cfg.repo("b").effort) == ("claude-opus-5-5", "xhigh")


def test_unknown_effort_is_an_error() -> None:
    cfg = Config.parse('repos:\n  a: { path: ., check: "true", effort: turbo }\n')
    with pytest.raises(ValueError, match="effort"):
        cfg.repo("a")
