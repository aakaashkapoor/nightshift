"""Tests for concierge helpers: check auto-detection + config registration."""

from nightshift.config import Config, register_repo
from nightshift.detect import detect_check


def test_detect_check_python(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert detect_check(tmp_path) == "pytest"


def test_detect_check_node_and_make_and_none(tmp_path) -> None:
    (tmp_path / "node").mkdir()
    (tmp_path / "node" / "package.json").write_text("{}", encoding="utf-8")
    assert detect_check(tmp_path / "node") == "npm test"

    (tmp_path / "mk").mkdir()
    (tmp_path / "mk" / "Makefile").write_text("test:\n", encoding="utf-8")
    assert detect_check(tmp_path / "mk") == "make test"

    (tmp_path / "empty").mkdir()
    assert detect_check(tmp_path / "empty") is None


def test_language_marker_wins_over_makefile(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("test:\n", encoding="utf-8")
    assert detect_check(tmp_path) == "pytest"


def test_register_repo_roundtrips_through_config(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    register_repo(cfg, name="my-app", path=tmp_path / "app", check="make test")

    repo_cfg = Config.load(cfg).repo("my-app")
    assert repo_cfg.check == "make test"
    assert repo_cfg.source == "local-md"
    assert repo_cfg.base_branch == "main"


def test_register_repo_with_symlinks(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    register_repo(
        cfg,
        name="app",
        path=tmp_path / "app",
        check="npm run check",
        source="github-issues",
        symlink_dirs=["node_modules"],
    )
    rc = Config.load(cfg).repo("app")
    assert rc.symlink_dirs == ["node_modules"]
    assert rc.source == "github-issues"


def test_register_repo_updates_existing(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    register_repo(cfg, name="a", path=tmp_path / "a", check="pytest")
    register_repo(cfg, name="b", path=tmp_path / "b", check="npm test")
    register_repo(cfg, name="a", path=tmp_path / "a", check="pytest -q")  # update

    loaded = Config.load(cfg)
    assert loaded.repo("a").check == "pytest -q"
    assert loaded.repo("b").check == "npm test"
