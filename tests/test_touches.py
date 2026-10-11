"""Touches parsing and path-prefix overlap (field notes #4, #21, #28)."""

from nightshift.scheduler import overlaps
from nightshift.slice import Slice


def _s(body: str) -> Slice:
    return Slice.parse(f"---\nid: s\ntitle: s\nstatus: ready\n---\n{body}")


def test_touches_list_items() -> None:
    sl = _s("## Goal\nx\n\n## Touches\n- src/a.ts\n- `src/kit/`\n* tests/a.test.ts\n")
    assert sl.touches() == {"src/a.ts", "src/kit/", "tests/a.test.ts"}


def test_touches_comma_line_from_scope_hints() -> None:
    sl = _s("## Scope hints\nsrc/engine/timeline.ts, src/render/page.tsx\n")
    assert sl.touches() == {"src/engine/timeline.ts", "src/render/page.tsx"}


def test_touches_section_wins_over_scope_hints() -> None:
    sl = _s("## Scope hints\nsrc/x.ts\n\n## Touches\n- src/y.ts\n")
    assert sl.touches() == {"src/y.ts"}


def test_trailing_paragraph_is_not_paths() -> None:
    body = (
        "## Scope hints\nsrc/a.ts, src/b.ts\n\n"
        "General: read `docs/design-v3.md` and `AGENTS.md`; register in `src/index.ts`.\n"
    )
    assert _s(body).touches() == {"src/a.ts", "src/b.ts"}


def test_blank_line_before_first_path_is_skipped() -> None:
    assert _s("## Touches\n\n- src/a.ts\n").touches() == {"src/a.ts"}


def test_prose_words_dropped_and_next_heading_ends() -> None:
    sl = _s("## Touches\n- src/a.ts (the parser)\n## Out of scope\n- src/zzz.ts\n")
    assert sl.touches() == {"src/a.ts"}


def test_no_section_means_empty() -> None:
    assert _s("## Goal\nx\n").touches() == set()


def test_scope_hints_is_an_alias() -> None:
    sl = _s("## Touches\n- src/a.ts\n")
    assert sl.scope_hints() == sl.touches()


def test_overlaps_exact_prefix_and_separators() -> None:
    assert overlaps({"src/a.ts"}, {"src/a.ts"})
    assert overlaps({"src/kit/"}, {"src/kit/Card.tsx"})
    assert overlaps({"src/kit"}, {"src\\kit\\Card.tsx"})
    assert overlaps({"./src/kit/Card.tsx"}, {"src/kit"})
    assert not overlaps({"src/kit"}, {"src/kitchen.ts"})
    assert not overlaps({"src/a.ts"}, {"src/b.ts"})
    assert not overlaps(set(), {"src/a.ts"})
