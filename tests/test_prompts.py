from nightshift.prompts import (
    build_prompt,
    check_failed_prompt,
    repair_prompt,
    trim_output,
)
from nightshift.slice import Slice


def _sl(size: str | None, touches: str = "") -> Slice:
    body = "## Goal\nDo it.\n"
    if size:
        body += f"\n## Size\n{size}\n"
    if touches:
        body += f"\n## Touches\n{touches}\n"
    return Slice.parse(f"---\nid: s1\ntitle: The title\nstatus: ready\n---\n{body}")


def test_small_prompt() -> None:
    p = build_prompt(_sl("S"))
    assert "SMALL" in p and "No plan file" in p
    assert "# The title" in p and "Do it." in p
    assert "Do NOT commit" in p


def test_medium_prompt_is_the_default() -> None:
    assert "MEDIUM" in build_prompt(_sl("M"))
    assert "MEDIUM" in build_prompt(_sl(None))


def test_large_prompt() -> None:
    p = build_prompt(_sl("L"))
    assert "LARGE" in p and "subagent-driven-development" in p
    # Background work dies with the session in a non-interactive run.
    assert "never run_in_background" in p


def test_touches_are_listed_sorted() -> None:
    p = build_prompt(_sl("S", "- src/b.py\n- src/a.py"))
    assert "src/a.py, src/b.py" in p


def test_no_touches_means_any() -> None:
    assert "any (the issue lists no Touches)" in build_prompt(_sl("S"))


def test_trim_output_keeps_the_tail() -> None:
    text = "\n".join(f"line {i}" for i in range(200))
    out = trim_output(text, max_lines=5)
    assert out.splitlines() == [f"line {i}" for i in range(195, 200)]
    assert len(trim_output("x" * 10000)) == 6000


def test_check_failed_prompt_has_command_and_output() -> None:
    p = check_failed_prompt("pytest -q", "FAILED test_x\n")
    assert "pytest -q" in p and "FAILED test_x" in p


def test_repair_prompt_has_error() -> None:
    assert "file not found" in repair_prompt("file not found")


def test_files_and_answer_prompts() -> None:
    from nightshift.prompts import ANSWER_PROMPT, FILES_PROMPT

    assert "a.py, b.py" in FILES_PROMPT.format(files="a.py, b.py")
    assert "result.json" in FILES_PROMPT
    text = ANSWER_PROMPT.format(answer="use sqlite")
    assert "use sqlite" in text and "result.json" in text
