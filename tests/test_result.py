import json

import pytest

from nightshift.result import RESULT_PATH, clear_result, read_result


def _write(wt, content) -> None:
    path = wt / RESULT_PATH
    path.parent.mkdir(exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


def test_missing_file(tmp_path) -> None:
    assert read_result(tmp_path) == (None, "file not found")


def test_invalid_json(tmp_path) -> None:
    _write(tmp_path, "{nope")
    result, why = read_result(tmp_path)
    assert result is None and why.startswith("not valid JSON")


def test_not_an_object(tmp_path) -> None:
    _write(tmp_path, [1])
    assert read_result(tmp_path) == (None, "not a JSON object")


def test_bad_outcome(tmp_path) -> None:
    _write(tmp_path, {"outcome": "maybe"})
    result, why = read_result(tmp_path)
    assert result is None and "outcome must be one of" in why


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"outcome": "done", "decisions": "x"}, "decisions must be"),
        ({"outcome": "done", "decisions": [1]}, "decisions must be"),
        ({"outcome": "done", "needs_files": "x"}, "needs_files must be"),
        ({"outcome": "question"}, "needs a question"),
        ({"outcome": "question", "question": "  "}, "needs a question"),
        ({"outcome": "needs_files"}, "needs needs_files"),
    ],
)
def test_invalid_fields(tmp_path, data, fragment) -> None:
    _write(tmp_path, data)
    result, why = read_result(tmp_path)
    assert result is None and fragment in why


def test_good_files(tmp_path) -> None:
    _write(tmp_path, {"outcome": "done", "decisions": ["a"], "notes": "n"})
    result, why = read_result(tmp_path)
    assert why == "" and result is not None
    assert (result.outcome, result.decisions, result.notes) == ("done", ["a"], "n")
    _write(tmp_path, {"outcome": "question", "question": "which?"})
    q, _ = read_result(tmp_path)
    assert q is not None and q.question == "which?"
    _write(tmp_path, {"outcome": "needs_files", "needs_files": ["x.py"]})
    n, _ = read_result(tmp_path)
    assert n is not None and n.needs_files == ["x.py"]


def test_clear_result(tmp_path) -> None:
    _write(tmp_path, {"outcome": "done"})
    clear_result(tmp_path)
    clear_result(tmp_path)  # idempotent
    assert not (tmp_path / RESULT_PATH).exists()
