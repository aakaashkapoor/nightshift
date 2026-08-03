"""Tests for the slice parser (SPEC §3 standard format)."""

from nightshift.slice import Slice

SAMPLE = """---
id: slice-003
title: Add retry to the uploader
status: ready
depends_on: [slice-001]
attempts: 0
jira: PROJ-142
---
## Goal
Make the uploader resilient to flaky networks.

## Acceptance criteria
- [ ] Retries failed uploads up to 3 times
- [ ] Backs off between attempts

## Scope hints
src/uploader.py
"""


def test_parse_reads_frontmatter_fields() -> None:
    s = Slice.parse(SAMPLE)
    assert s.id == "slice-003"
    assert s.title == "Add retry to the uploader"
    assert s.status == "ready"
    assert s.depends_on == ["slice-001"]
    assert s.attempts == 0
    assert s.jira == "PROJ-142"


def test_parse_defaults_for_optional_fields() -> None:
    minimal = "---\nid: slice-009\ntitle: Tiny\nstatus: ready\n---\n## Goal\nDo a thing.\n"
    s = Slice.parse(minimal)
    assert s.depends_on == []
    assert s.attempts == 0
    assert s.jira is None


def test_status_writeback_roundtrips_and_preserves_body() -> None:
    s = Slice.parse(SAMPLE)
    s.status = "in-progress"

    out = s.to_markdown()

    reparsed = Slice.parse(out)
    assert reparsed.status == "in-progress"
    # Body and untouched frontmatter survive the round-trip.
    assert "## Goal" in out
    assert "Make the uploader resilient to flaky networks." in out
    assert "src/uploader.py" in out
    assert reparsed.id == "slice-003"
    assert reparsed.depends_on == ["slice-001"]
    assert reparsed.jira == "PROJ-142"


def test_load_and_save_file(tmp_path) -> None:
    path = tmp_path / "slice-003.md"
    path.write_text(SAMPLE, encoding="utf-8")

    s = Slice.load(path)
    s.status = "done"
    s.attempts = 2
    s.save(path)

    reloaded = Slice.load(path)
    assert reloaded.status == "done"
    assert reloaded.attempts == 2
    assert reloaded.title == "Add retry to the uploader"


def test_parse_rejects_missing_frontmatter() -> None:
    import pytest

    with pytest.raises(ValueError):
        Slice.parse("## Goal\nNo frontmatter here.\n")


def test_scope_hints_extracts_path_tokens() -> None:
    s = Slice.parse(SAMPLE)  # "## Scope hints\nsrc/uploader.py"
    assert s.scope_hints() == {"src/uploader.py"}


def test_scope_hints_ignores_prose_and_notes() -> None:
    text = (
        "---\nid: x\ntitle: x\nstatus: ready\n---\n"
        "## Goal\ng\n\n## Scope hints\n"
        "`greet.py` (new file), src/util.py\nprobably touches the api layer\n\n"
        "## Notes\nnot-a-scope.py\n"
    )
    s = Slice.parse(text)
    # path-like tokens only; prose words dropped; the Notes section not included.
    assert s.scope_hints() == {"greet.py", "src/util.py"}


def test_scope_hints_empty_when_absent() -> None:
    text = "---\nid: x\ntitle: x\nstatus: ready\n---\n## Goal\ng\n"
    assert Slice.parse(text).scope_hints() == set()
