"""Tests for the headless Claude executor's logic (prompt/argv/result parsing).

The real subprocess runner is injected, so these tests never spawn a live Claude
(that path is smoke-tested at the 1.8 demo). We test everything *around* the call.
"""

import json

from nightshift.executor import Executor, RunOutput
from nightshift.slice import Slice

SLICE = Slice.parse(
    "---\n"
    "id: slice-003\n"
    "title: Add retry to the uploader\n"
    "status: ready\n"
    "---\n"
    "## Goal\n"
    "Make the uploader resilient to flaky networks.\n\n"
    "## Acceptance criteria\n"
    "- [ ] Retries failed uploads up to 3 times\n"
)


class FakeRunner:
    def __init__(self, output: RunOutput):
        self.output = output
        self.calls: list[dict] = []

    def __call__(self, argv, cwd, stdin_text) -> RunOutput:
        self.calls.append({"argv": argv, "cwd": cwd, "stdin": stdin_text})
        return self.output


def test_build_prompt_includes_task_and_guardrails() -> None:
    prompt = Executor().build_prompt(SLICE)
    assert "Add retry to the uploader" in prompt
    assert "Make the uploader resilient to flaky networks." in prompt
    assert "Retries failed uploads up to 3 times" in prompt
    # Git is the harness's job — the agent must not commit/push/branch.
    assert "commit" in prompt.lower()


def test_build_argv_defaults() -> None:
    assert Executor().build_argv() == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
    ]


def test_build_argv_with_model_and_resume() -> None:
    argv = Executor(model="opus").build_argv(resume_session="sess_9")
    assert "--model" in argv and "opus" in argv
    assert "--resume" in argv and "sess_9" in argv


def test_execute_passes_prompt_and_cwd_to_runner(tmp_path) -> None:
    runner = FakeRunner(RunOutput(0, json.dumps({"result": "ok", "session_id": "s1"}), ""))
    Executor(runner=runner).execute(tmp_path, SLICE)
    call = runner.calls[0]
    assert call["cwd"] == str(tmp_path)
    assert call["argv"][:2] == ["claude", "-p"]
    assert "Add retry to the uploader" in call["stdin"]


def test_execute_parses_session_id_and_result_on_success() -> None:
    runner = FakeRunner(
        RunOutput(0, json.dumps({"result": "all done", "session_id": "sess_x"}), "")
    )
    result = Executor(runner=runner).execute(".", SLICE)
    assert result.succeeded is True
    assert result.session_id == "sess_x"
    assert result.result_text == "all done"


def test_execute_reports_failure_on_nonzero() -> None:
    runner = FakeRunner(RunOutput(1, "", "boom"))
    result = Executor(runner=runner).execute(".", SLICE)
    assert result.succeeded is False


def test_execute_handles_non_json_output() -> None:
    runner = FakeRunner(RunOutput(0, "not json at all", ""))
    result = Executor(runner=runner).execute(".", SLICE)
    assert result.succeeded is True
    assert result.session_id is None
    assert result.result_text == "not json at all"
