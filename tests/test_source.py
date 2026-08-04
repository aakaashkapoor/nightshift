"""Tests for the local-md slice source (discovery + status write-back)."""

from nightshift.source import LocalMdSource


def _write_slice(dirpath, sid, status, deps=None) -> None:
    deps_line = f"depends_on: {deps}\n" if deps is not None else ""
    (dirpath / f"{sid}.md").write_text(
        f"---\nid: {sid}\ntitle: {sid} title\nstatus: {status}\n{deps_line}---\n"
        f"## Goal\nDo {sid}.\n",
        encoding="utf-8",
    )


def _slices_dir(tmp_path):
    d = tmp_path / ".slices"
    d.mkdir()
    return d


def test_list_all_and_ready(tmp_path) -> None:
    d = _slices_dir(tmp_path)
    _write_slice(d, "slice-001", "ready")
    _write_slice(d, "slice-002", "ready", deps="[slice-001]")
    _write_slice(d, "slice-003", "done")
    (d / "notes.md").write_text("just notes, not a slice\n", encoding="utf-8")

    source = LocalMdSource(tmp_path)

    all_ids = {s.id for s in source.list_all()}
    assert all_ids == {"slice-001", "slice-002", "slice-003"}  # notes.md skipped

    ready_ids = {s.id for s in source.list_ready()}
    assert ready_ids == {"slice-001", "slice-002"}


def test_get_returns_slice_or_none(tmp_path) -> None:
    d = _slices_dir(tmp_path)
    _write_slice(d, "slice-001", "ready")
    source = LocalMdSource(tmp_path)
    assert source.get("slice-001").title == "slice-001 title"
    assert source.get("missing") is None


def test_set_status_persists(tmp_path) -> None:
    d = _slices_dir(tmp_path)
    _write_slice(d, "slice-001", "ready")
    source = LocalMdSource(tmp_path)

    source.set_status("slice-001", "in-progress")

    assert LocalMdSource(tmp_path).get("slice-001").status == "in-progress"


def test_missing_slices_dir_returns_empty(tmp_path) -> None:
    source = LocalMdSource(tmp_path)  # no .slices dir at all
    assert source.list_all() == []
    assert source.list_ready() == []


def test_build_source_selects_adapter_from_config(tmp_path) -> None:
    from nightshift.config import RepoConfig
    from nightshift.github import GitHubIssuesSource
    from nightshift.source import build_source

    local = build_source(RepoConfig(name="a", path=tmp_path, check="x", source="local-md"))
    gh = build_source(RepoConfig(name="a", path=tmp_path, check="x", source="github-issues"))
    assert isinstance(local, LocalMdSource)
    assert isinstance(gh, GitHubIssuesSource)
