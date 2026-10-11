"""Tests for the machine-local runtime.json store."""

from nightshift.runtime import Runtime


class FakeWT:
    def __init__(self, existing):
        self.existing = set(existing)

    def exists(self, sid) -> bool:
        return sid in self.existing


def test_record_get_forget(tmp_path) -> None:
    rt = Runtime(tmp_path / "runtime.json")
    rt.record("slice-001", session_id="sess_1", branch="nightshift/slice-001")
    assert rt.get("slice-001")["session_id"] == "sess_1"
    rt.forget("slice-001")
    assert rt.get("slice-001") == {}


def test_persists_across_instances(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    Runtime(path).record("slice-001", session_id="sess_1")
    assert Runtime(path).get("slice-001")["session_id"] == "sess_1"


def test_record_ignores_none_values(tmp_path) -> None:
    rt = Runtime(tmp_path / "runtime.json")
    rt.record("slice-001", session_id="sess_1", branch=None)
    assert "branch" not in rt.get("slice-001")


def test_reconcile_drops_unknown_and_missing_worktree(tmp_path) -> None:
    rt = Runtime(tmp_path / "runtime.json")
    rt.record("a", session_id="x")
    rt.record("b", session_id="y")
    rt.record("c", session_id="z")
    # b is no longer a known slice; c's worktree is gone; a survives.
    rt.reconcile(known_slice_ids={"a", "c"}, worktrees=FakeWT({"a"}))
    assert rt.get("a") and not rt.get("b") and not rt.get("c")


def test_clear_drops_only_the_named_keys(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    rt = Runtime(path)
    rt.record("s", session_id="x", resume_prompt="p", interrupted=True)
    rt.clear("s", "resume_prompt", "missing")
    assert rt.get("s") == {"session_id": "x", "interrupted": True}
    assert Runtime(path).get("s") == {"session_id": "x", "interrupted": True}


def test_clear_on_unknown_slice_is_a_noop(tmp_path) -> None:
    rt = Runtime(tmp_path / "runtime.json")
    rt.clear("nope", "resume_prompt")
    assert rt.get("nope") == {}


def test_reads_see_writes_from_another_process(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    daemon, cli = Runtime(path), Runtime(path)
    daemon.record("s", session_id="x")
    cli.record("s", interrupted=True, resume_prompt="answer")
    assert daemon.get("s") == {"session_id": "x", "interrupted": True, "resume_prompt": "answer"}
    daemon.record("s", branch="b")  # must not clobber what the CLI wrote
    assert Runtime(path).get("s")["resume_prompt"] == "answer"


def test_a_torn_file_keeps_the_last_good_data(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    rt = Runtime(path)
    rt.record("s", session_id="x")
    path.write_text("{ not json", encoding="utf-8")
    assert rt.get("s") == {"session_id": "x"}
