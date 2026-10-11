# Nightshift v2 Phase 1: usage-limit pause, events, status, wait

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Claude's usage limit hits, the daemon pauses until the reset and then resumes the interrupted slices from their sessions. A master agent can see exact status (`nsh status`) and block until something needs it (`nsh wait`).

**Architecture:** A new `limits` module detects the usage limit from `claude -p` output and reads the exact reset time from the session transcript. The daemon gains three collaborators: `EventLog` (append-only `events.jsonl`), `StateStore` (`state.json`, rewritten atomically) and a pause clock. All three live in a per-repo home, `~/.nightshift/repos/<name>/`. `nsh status` renders `state.json`; `nsh wait` tails `events.jsonl` from a saved cursor.

**Tech Stack:** Python 3.11+, typer, pytest (100% branch coverage), ruff, mypy.

**Design:** `docs/superpowers/specs/2026-10-11-nightshift-v2-design.md` (Step 0, Step 6, toolkit).

**Roadmap (later plans, not this one):**
- Phase 2: daemon reliability (worker pool, critical path, supervisor, pause/stop, main-green gate, config reload, why-not-runnable log, Touches lock by path prefix).
- Phase 3: sized slices (issue template, linter, S/M/L prompts, models per role, `result.json`, strict file list, check output on retry).
- Phase 4: conflicts and ship (union merge, lockfile regeneration, rebasing own PRs, auto-merge unless `review`, `nsh pr` helpers, per-worktree install).
- Phase 5: skills (slicer contracts-first, babysit on the new toolkit).

---

## Rules for every task

- Work in `C:\Users\aakaa\code\nightshift`. Python: `.venv/Scripts/python`.
- Test-first. Run single tests with `.venv/Scripts/python -m pytest tests/<file>::<test> -q`.
- Before each commit run the full gate: `.venv/Scripts/python -m nightshift._gate`. It must print `CHECK PASSED` (ruff lint and format, mypy, pytest with **100% branch coverage**). If coverage drops, add tests; never add `# pragma: no cover` except on real I/O shims.
- Run `.venv/Scripts/python -m ruff format src tests` before the gate.
- Commit author is already configured. End each commit message with:

```
Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UcNkjNqsVKMUAvGQ1Pates
```

- Do not push; the controller pushes.

## File map

| File | Change | Responsibility |
|---|---|---|
| `src/nightshift/limits.py` | create | Detect usage limit; read reset time; `UsageLimitReached` |
| `src/nightshift/paths.py` | create | Per-repo home dir |
| `src/nightshift/events.py` | create | `EventLog`: append and read-from-offset |
| `src/nightshift/state.py` | create | `StateStore` and `render_status` |
| `src/nightshift/wait.py` | create | `wait_for_attention`, `run_wait_cli`, `run_status_cli` |
| `src/nightshift/executor.py` | modify | `ExecResult.is_error`, `ExecResult.usage_limited` |
| `src/nightshift/review.py` | modify | Reviewer raises on usage limit |
| `src/nightshift/resolver.py` | modify | Resolver raises on usage limit |
| `src/nightshift/pipeline.py` | modify | `run_slice(resume_session=...)`, raise on limit |
| `src/nightshift/daemon.py` | modify | Pause/resume, events, state, idle kinds, resilient `run_forever` |
| `src/nightshift/cli.py` | modify | `nsh status`, `nsh wait` |
| `skills/nightshift-babysit/SKILL.md` | modify | Use `nsh wait` / `nsh status` |
| `tests/test_limits.py`, `tests/test_events.py`, `tests/test_state.py`, `tests/test_wait.py`, `tests/test_daemon_limits.py` | create | Tests |

---

### Task 1: `limits.py`

Real data this is built on: when the limit hits, the `claude -p` result text is like
`You've hit your session limit · resets 2:30pm (America/Los_Angeles)` and the session
transcript `~/.claude/projects/<dir>/<session_id>.jsonl` gets a line containing
`"apiError":"usage_limit_reached"` and
`"apiErrorParams":{"rate_limit_info":{"resetsAt":1791581400, ...}}` (Unix seconds).

**Files:**
- Create: `src/nightshift/limits.py`
- Test: `tests/test_limits.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for usage-limit detection and reset-time lookup."""

import json

from nightshift.limits import (
    FALLBACK_WAIT,
    RESET_GRACE,
    UsageLimitReached,
    is_usage_limit,
    reset_time,
    resume_at,
)


def test_detects_session_limit_text() -> None:
    assert is_usage_limit("You've hit your session limit · resets 2:30pm (America/Los_Angeles)")


def test_detects_weekly_and_legacy_text() -> None:
    assert is_usage_limit("You've hit your weekly limit · resets Oct 14")
    assert is_usage_limit("Claude AI usage limit reached|1791581400")


def test_ignores_normal_text() -> None:
    assert not is_usage_limit("Implemented the uploader retry.")
    assert not is_usage_limit("")


def _write_transcript(root, session_id, lines) -> None:
    d = root / "projects" / "C--some-worktree"
    d.mkdir(parents=True)
    (d / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_reset_time_reads_resets_at_from_transcript(tmp_path) -> None:
    record = {
        "type": "assistant",
        "apiError": "usage_limit_reached",
        "apiErrorParams": {"rate_limit_info": {"resetsAt": 1791581400}},
    }
    _write_transcript(tmp_path, "sess-1", ['{"type":"user"}', "not json", json.dumps(record)])
    assert reset_time("sess-1", root=tmp_path) == 1791581400.0


def test_reset_time_skips_broken_matching_lines(tmp_path) -> None:
    good = json.dumps(
        {"apiError": "usage_limit_reached", "apiErrorParams": {"rate_limit_info": {"resetsAt": 5}}}
    )
    no_time = json.dumps({"apiError": "usage_limit_reached", "apiErrorParams": None})
    _write_transcript(tmp_path, "s2", [good, "usage_limit_reached {broken", no_time])
    assert reset_time("s2", root=tmp_path) == 5.0


def test_reset_time_none_when_missing(tmp_path) -> None:
    assert reset_time(None, root=tmp_path) is None
    assert reset_time("nope", root=tmp_path) is None
    _write_transcript(tmp_path, "s3", ['{"type":"user"}'])
    assert reset_time("s3", root=tmp_path) is None


def test_reset_time_default_root_uses_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    record = {"apiError": "usage_limit_reached", "apiErrorParams": {"rate_limit_info": {"resetsAt": 9}}}
    _write_transcript(tmp_path, "s4", [json.dumps(record)])
    assert reset_time("s4") == 9.0


def test_reset_time_default_root_is_home_dot_claude(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr("nightshift.limits.Path.home", lambda: tmp_path)
    record = {"apiError": "usage_limit_reached", "apiErrorParams": {"rate_limit_info": {"resetsAt": 7}}}
    _write_transcript(tmp_path / ".claude", "s5", [json.dumps(record)])
    assert reset_time("s5") == 7.0


def test_resume_at_uses_known_future_reset() -> None:
    assert resume_at(1000.0, now=400.0) == 1000.0 + RESET_GRACE


def test_resume_at_falls_back_when_unknown_or_past() -> None:
    assert resume_at(None, now=400.0) == 400.0 + FALLBACK_WAIT
    assert resume_at(100.0, now=400.0) == 400.0 + FALLBACK_WAIT


def test_usage_limit_reached_carries_session() -> None:
    exc = UsageLimitReached("sess-9")
    assert exc.session_id == "sess-9"
    assert "usage limit" in str(exc)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_limits.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'nightshift.limits'`

- [ ] **Step 3: Implement**

```python
"""Claude usage limits (v2 design, Step 0: the budget gate).

When the account's usage limit hits, ``claude -p`` stops without doing any work and
returns an error result like "You've hit your session limit · resets 2:30pm (...)".
The session transcript records the exact reset as a Unix timestamp
(``apiErrorParams.rate_limit_info.resetsAt``). The daemon pauses until then instead
of blocking slices; if the time can't be found it retries every ``FALLBACK_WAIT``
seconds (a retry while still limited fails instantly and costs no tokens).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

FALLBACK_WAIT = 15 * 60  # seconds to wait when the reset time is unknown
RESET_GRACE = 60  # seconds after the reported reset before resuming

_LIMIT_RE = re.compile(r"(?i)hit your [a-z -]*limit|usage limit reached")


class UsageLimitReached(Exception):
    """An agent run stopped because the Claude usage limit was hit."""

    def __init__(self, session_id: str | None):
        super().__init__("usage limit reached")
        self.session_id = session_id


def is_usage_limit(text: str) -> bool:
    return bool(_LIMIT_RE.search(text or ""))


def _claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def reset_time(session_id: str | None, root: Path | None = None) -> float | None:
    """The limit's reset time (Unix seconds) from the session transcript, if recorded."""
    if not session_id:
        return None
    base = (root if root is not None else _claude_dir()) / "projects"
    for transcript in base.glob(f"*/{session_id}.jsonl"):
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if "usage_limit_reached" not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            info = (record.get("apiErrorParams") or {}).get("rate_limit_info") or {}
            resets = info.get("resetsAt")
            if isinstance(resets, int | float):
                return float(resets)
    return None


def resume_at(resets_at: float | None, now: float) -> float:
    """When the daemon should try again."""
    if resets_at is not None and resets_at > now:
        return resets_at + RESET_GRACE
    return now + FALLBACK_WAIT
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_limits.py -q`
Expected: all pass.

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add src/nightshift/limits.py tests/test_limits.py
git commit -m "Limits: detect the Claude usage limit and read its reset time"
```

---

### Task 2: executor, reviewer and resolver report the limit

A usage-limit result must never look like success. Today an `AgentReviewer` hitting
the limit returns unparseable text, which `parse_findings` treats as "no findings",
so the slice would be approved without review.

**Files:**
- Modify: `src/nightshift/executor.py` (`ExecResult`, `_parse`)
- Modify: `src/nightshift/review.py` (`AgentReviewer.review`)
- Modify: `src/nightshift/resolver.py` (`Resolver.__call__`)
- Test: `tests/test_limits.py` (append)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_limits.py`)

```python
import pytest

from nightshift.executor import Executor, RunOutput
from nightshift.resolver import Resolver
from nightshift.review import AgentReviewer

LIMIT_JSON = json.dumps(
    {
        "is_error": True,
        "result": "You've hit your session limit · resets 2:30pm (America/Los_Angeles)",
        "session_id": "lim-1",
    }
)


def _runner(stdout: str, rc: int = 0):
    return lambda argv, cwd, stdin: RunOutput(rc, stdout, "")


def test_executor_flags_usage_limit() -> None:
    res = Executor(runner=_runner(LIMIT_JSON)).execute_prompt("/wt", "go")
    assert res.is_error is True
    assert res.usage_limited is True
    assert res.session_id == "lim-1"


def test_executor_flags_limit_on_nonzero_exit_without_json() -> None:
    res = Executor(runner=_runner("Claude AI usage limit reached|1791581400", rc=1)).execute_prompt(
        "/wt", "go"
    )
    assert res.usage_limited is True


def test_executor_success_mentioning_limits_is_not_limited() -> None:
    ok = json.dumps({"is_error": False, "result": "Handled: usage limit reached text", "session_id": "s"})
    res = Executor(runner=_runner(ok)).execute_prompt("/wt", "go")
    assert res.is_error is False
    assert res.usage_limited is False


def test_reviewer_raises_on_usage_limit() -> None:
    with pytest.raises(UsageLimitReached) as info:
        AgentReviewer(Executor(runner=_runner(LIMIT_JSON))).review("/wt", None)
    assert info.value.session_id == "lim-1"


def test_resolver_raises_on_usage_limit() -> None:
    with pytest.raises(UsageLimitReached):
        Resolver(Executor(runner=_runner(LIMIT_JSON)))("/wt")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_limits.py -q`
Expected: FAIL, `AttributeError: 'ExecResult' object has no attribute 'is_error'`

- [ ] **Step 3: Implement**

In `src/nightshift/executor.py`, add the import and two fields, and update `_parse`:

```python
from .limits import is_usage_limit
```

```python
@dataclass
class ExecResult:
    succeeded: bool
    session_id: str | None
    result_text: str
    output: str
    returncode: int
    is_error: bool = False
    usage_limited: bool = False  # the run stopped on the Claude usage limit
```

```python
    def _parse(self, out: RunOutput) -> ExecResult:
        session_id: str | None = None
        result_text = out.stdout
        is_error = False
        try:
            data = json.loads(out.stdout)
            if isinstance(data, dict):
                session_id = data.get("session_id")
                result_text = data.get("result", out.stdout)
                is_error = bool(data.get("is_error", False))
        except (json.JSONDecodeError, TypeError):
            pass
        failed = is_error or out.returncode != 0
        return ExecResult(
            succeeded=out.returncode == 0,
            session_id=session_id,
            result_text=result_text,
            output=out.stdout,
            returncode=out.returncode,
            is_error=is_error,
            usage_limited=failed and is_usage_limit(f"{result_text}\n{out.stderr}"),
        )
```

In `src/nightshift/review.py`:

```python
from .limits import UsageLimitReached
```

```python
    def review(self, worktree_path: Path | str, sl) -> ReviewResult:
        result = self.executor.execute_prompt(str(worktree_path), REVIEW_PROMPT)
        if result.usage_limited:
            raise UsageLimitReached(result.session_id)
        return parse_findings(result.result_text)
```

In `src/nightshift/resolver.py`:

```python
from .limits import UsageLimitReached
```

```python
    def __call__(self, worktree_path: Path | str) -> None:
        result = self.executor.execute_prompt(str(worktree_path), RESOLVE_PROMPT)
        if result.usage_limited:
            raise UsageLimitReached(result.session_id)
```

- [ ] **Step 4: Run all tests**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass (existing fakes return `is_error`-free JSON, so nothing else changes).

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add src/nightshift/executor.py src/nightshift/review.py src/nightshift/resolver.py tests/test_limits.py
git commit -m "Executor, reviewer and resolver report the usage limit instead of passing silently"
```

---

### Task 3: per-repo home and `EventLog`

**Files:**
- Create: `src/nightshift/paths.py`
- Create: `src/nightshift/events.py`
- Test: `tests/test_events.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the per-repo home and the append-only event log."""

import json

from nightshift.events import ATTENTION, EventLog
from nightshift.paths import repo_home


def test_repo_home_default_and_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nightshift.paths.Path.home", lambda: tmp_path)
    assert repo_home("demo") == tmp_path / ".nightshift" / "repos" / "demo"
    assert repo_home("demo", root=tmp_path / "x") == tmp_path / "x" / "repos" / "demo"


def test_emit_appends_json_lines(tmp_path) -> None:
    log = EventLog(tmp_path / "sub" / "events.jsonl", clock=lambda: 12.5)
    rec = log.emit("blocked", "issue-3", reason="check failed")
    assert rec == {"ts": 12.5, "kind": "blocked", "slice": "issue-3", "reason": "check failed"}
    log.emit("all_done")
    lines = (tmp_path / "sub" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["blocked", "all_done"]
    assert "slice" not in json.loads(lines[1])


def test_read_from_returns_events_and_new_offset(tmp_path) -> None:
    log = EventLog(tmp_path / "events.jsonl", clock=lambda: 1.0)
    assert log.read_from(0) == ([], 0)  # no file yet
    log.emit("a")
    events, offset = log.read_from(0)
    assert [e["kind"] for e in events] == ["a"]
    log.emit("b")
    events, offset2 = log.read_from(offset)
    assert [e["kind"] for e in events] == ["b"]
    assert log.read_from(offset2) == ([], offset2)


def test_read_from_ignores_partial_trailing_line(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"kind": "a"}\n{"kind": "b"', encoding="utf-8")
    events, offset = EventLog(path).read_from(0)
    assert [e["kind"] for e in events] == ["a"]
    assert offset == len('{"kind": "a"}\n')
    assert EventLog(path).read_from(offset) == ([], offset)


def test_read_from_restarts_when_file_shrank(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"kind": "a"}\n', encoding="utf-8")
    events, _ = EventLog(path).read_from(10_000)
    assert [e["kind"] for e in events] == ["a"]


def test_read_from_skips_corrupt_lines(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('garbage\n\n{"kind": "a"}\n', encoding="utf-8")
    events, _ = EventLog(path).read_from(0)
    assert [e["kind"] for e in events] == ["a"]


def test_end_offset(tmp_path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    assert log.end_offset() == 0
    log.emit("a")
    assert log.end_offset() == (tmp_path / "events.jsonl").stat().st_size


def test_attention_kinds() -> None:
    assert {"review_ready", "blocked", "paused_limit", "resumed", "daemon_exit",
            "all_done", "stalled"} == set(ATTENTION)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_events.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/nightshift/paths.py`:

```python
"""Where Nightshift keeps per-repo machine-local files (events, state, runtime)."""

from __future__ import annotations

from pathlib import Path


def repo_home(name: str, root: Path | None = None) -> Path:
    base = root if root is not None else Path.home() / ".nightshift"
    return base / "repos" / name
```

`src/nightshift/events.py`:

```python
"""The daemon's append-only event log (v2 design, Step 6).

Every notable thing the daemon does is one JSON line in ``events.jsonl``. The master
agent never parses the human log: ``nsh wait`` reads this file from a saved byte
offset and wakes on the ``ATTENTION`` kinds.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

# Kinds that need the master agent. Others (slice_started, merged, ...) are history.
ATTENTION = frozenset(
    {"review_ready", "blocked", "paused_limit", "resumed", "daemon_exit", "all_done", "stalled"}
)


class EventLog:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock

    def emit(self, kind: str, slice_id: str | None = None, **data) -> dict:
        record: dict = {"ts": round(self.clock(), 3), "kind": kind}
        if slice_id:
            record["slice"] = slice_id
        record.update(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return record

    def end_offset(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def read_from(self, offset: int) -> tuple[list[dict], int]:
        """Complete events after byte ``offset``, and the offset to continue from."""
        if not self.path.exists():
            return [], 0
        if offset > self.path.stat().st_size:  # file was replaced/truncated
            offset = 0
        with self.path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        end = data.rfind(b"\n")
        if end < 0:
            return [], offset
        events = []
        for line in data[: end + 1].decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events, offset + end + 1
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_events.py -q`
Expected: all pass.

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add src/nightshift/paths.py src/nightshift/events.py tests/test_events.py
git commit -m "Events: per-repo home and an append-only event log"
```

---

### Task 4: `StateStore` and `render_status`

**Files:**
- Create: `src/nightshift/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the daemon's state file and its human rendering."""

import json

from nightshift.state import StateStore, render_status


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_new_store_is_empty(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    assert store.snapshot() == {"daemon": {}, "slices": {}}


def test_set_daemon_and_slice_persist(tmp_path) -> None:
    clock = Clock()
    path = tmp_path / "d" / "state.json"
    store = StateStore(path, clock=clock)
    store.set_daemon(status="running", pid=42)
    store.set_slice("issue-1", "working")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["daemon"] == {"status": "running", "pid": 42, "updated": 1000.0}
    assert data["slices"]["issue-1"] == {"phase": "working", "since": 1000.0}
    assert StateStore(path).snapshot() == data  # reload


def test_same_phase_keeps_since_new_phase_resets(tmp_path) -> None:
    clock = Clock()
    store = StateStore(tmp_path / "state.json", clock=clock)
    store.set_slice("issue-1", "working")
    clock.t = 1100.0
    store.set_slice("issue-1", "working", detail="attempt 2")
    assert store.snapshot()["slices"]["issue-1"]["since"] == 1000.0
    store.set_slice("issue-1", "blocked", detail="check failed", pr=None)
    entry = store.snapshot()["slices"]["issue-1"]
    assert entry == {"phase": "blocked", "since": 1100.0, "detail": "check failed"}


def test_drop_slice(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.set_slice("issue-1", "working")
    store.drop_slice("issue-1")
    store.drop_slice("missing")
    assert store.snapshot()["slices"] == {}


def test_corrupt_file_starts_empty(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{nope", encoding="utf-8")
    assert StateStore(path).snapshot() == {"daemon": {}, "slices": {}}


def test_render_status_running() -> None:
    snap = {
        "daemon": {"status": "running", "pid": 7, "updated": 990.0},
        "slices": {
            "issue-2": {"phase": "in-review", "since": 1000.0 - 7200, "pr": "https://pr/2"},
            "issue-1": {"phase": "working", "since": 1000.0 - 65},
            "issue-3": {"phase": "blocked", "since": 1000.0 - 90000, "detail": "check failed"},
        },
    }
    text = render_status(snap, now=1000.0)
    assert "daemon: running (pid 7, updated 10s ago)" in text
    lines = text.splitlines()
    assert lines[1] == "slices:"
    assert "issue-1" in lines[2] and "working" in lines[2] and "1m" in lines[2]
    assert "issue-2" in lines[3] and "2h" in lines[3] and "https://pr/2" in lines[3]
    assert "issue-3" in lines[4] and "1d" in lines[4] and "check failed" in lines[4]


def test_render_status_paused_and_empty() -> None:
    snap = {"daemon": {"status": "paused_limit", "paused_until": 1000.0 + 3725}, "slices": {}}
    text = render_status(snap, now=1000.0)
    assert "daemon: paused_limit (resumes in 1h02m)" in text
    assert "no slices in flight" in text


def test_render_status_unknown_daemon() -> None:
    assert render_status({"daemon": {}, "slices": {}}, now=0.0).startswith("daemon: not started")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_state.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""The daemon's live state (v2 design, Step 0) and its `nsh status` rendering.

``state.json`` is rewritten atomically on every change, so a reader never sees a half
file. It's a view of what the daemon is doing now, not a source of truth: the issue
labels stay authoritative.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path


def _empty() -> dict:
    return {"daemon": {}, "slices": {}}


class StateStore:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self.data = _empty()
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = {"daemon": loaded["daemon"], "slices": loaded["slices"]}
            except (json.JSONDecodeError, KeyError, TypeError, OSError):
                self.data = _empty()

    def set_daemon(self, **fields) -> None:
        self.data["daemon"].update(fields)
        self.data["daemon"]["updated"] = self.clock()
        self._save()

    def set_slice(self, slice_id: str, phase: str, **fields) -> None:
        now = self.clock()
        prev = self.data["slices"].get(slice_id) or {}
        since = prev["since"] if prev.get("phase") == phase else now
        entry = {"phase": phase, "since": since}
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.data["slices"][slice_id] = entry
        self._save()

    def drop_slice(self, slice_id: str) -> None:
        if self.data["slices"].pop(slice_id, None) is not None:
            self._save()

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self.data))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s % 3600 >= 60 else f"{s // 3600}h"
    return f"{s // 86400}d"


def render_status(snapshot: dict, now: float) -> str:
    daemon = snapshot.get("daemon") or {}
    status = daemon.get("status")
    if not status:
        head = "daemon: not started"
    elif status == "paused_limit":
        head = f"daemon: paused_limit (resumes in {_ago(daemon.get('paused_until', now) - now)})"
    else:
        head = f"daemon: {status} (pid {daemon.get('pid', '?')}, updated {_ago(now - daemon.get('updated', now))} ago)"
    slices = snapshot.get("slices") or {}
    if not slices:
        return f"{head}\nno slices in flight"
    lines = [head, "slices:"]
    for sid in sorted(slices, key=lambda k: (slices[k].get("since", 0) * -1, k)):
        entry = slices[sid]
        extra = entry.get("pr") or entry.get("detail") or ""
        lines.append(
            f"  {sid:<12} {entry.get('phase', '?'):<11} {_ago(now - entry.get('since', now)):>6}  {extra}".rstrip()
        )
    return "\n".join(lines)
```

Note on ordering: newest phase change first. Check the render test's expected line order
(issue-1 `since` is newest, then issue-2, then issue-3) matches; it does.

Note on `_ago`: `7200s` renders `2h`, `3725s` renders `1h02m`, `65s` renders `1m`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_state.py -q`
Expected: all pass.

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add src/nightshift/state.py tests/test_state.py
git commit -m "State: atomic state.json and the nsh status rendering"
```

---

### Task 5: `run_slice` resumes an interrupted slice and raises on the limit

**Files:**
- Modify: `src/nightshift/pipeline.py` (`run_slice`)
- Test: `tests/test_daemon_limits.py` (create; pipeline-level tests first)

Behaviour:
- `run_slice(..., resume_session=None)`. If `resume_session` is set **and** the worktree
  exists, attach to it, and make the first agent call `CONTINUE_PROMPT` on that session.
  Otherwise behave exactly as today.
- If any agent result in the work loop or the review-fix round has `usage_limited`,
  raise `UsageLimitReached(session)`. Reviewer limits already raise (Task 2).
- When resuming, the final commit uses `_commit_if_changes` (an earlier interrupted ship
  may have committed already).

- [ ] **Step 1: Write the failing tests**

```python
"""Usage-limit pause and resume: pipeline and daemon."""

import json
import subprocess
from pathlib import Path

import pytest

from nightshift.executor import Executor, RunOutput
from nightshift.limits import UsageLimitReached
from nightshift.pipeline import CONTINUE_PROMPT, run_slice
from nightshift.slice import Slice
from nightshift.worktree import WorktreeManager

LIMIT = RunOutput(
    0,
    json.dumps({"is_error": True, "result": "You've hit your session limit · resets 2:30pm", "session_id": "lim"}),
    "",
)


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(r)], check=True, capture_output=True)
    _git(r, "config", "user.email", "t@e.local")
    _git(r, "config", "user.name", "T")
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    (r / ".slices").mkdir()
    return r


def _slice(sid="slice-001") -> Slice:
    return Slice.parse(f"---\nid: {sid}\ntitle: {sid}\nstatus: ready\n---\n## Goal\ndo it\n")


class Script:
    """Fake agent: returns queued outputs; writes a file on non-limit calls."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.prompts: list[str] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        self.prompts.append(stdin)
        self.argvs.append(argv)
        out = self.outputs.pop(0)
        if out is not LIMIT:
            Path(cwd, "work.txt").write_text("x", encoding="utf-8")
        return out


def _ok(session="s1") -> RunOutput:
    return RunOutput(0, json.dumps({"result": "ok", "session_id": session}), "")


def test_run_slice_raises_on_usage_limit(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    with pytest.raises(UsageLimitReached) as info:
        run_slice(_slice(), repo_path=repo, check_cmd="echo ok", worktrees=wts,
                  executor=Executor(runner=Script(LIMIT)))
    assert info.value.session_id == "lim"
    assert wts.exists("slice-001")  # worktree kept for the resume


def test_run_slice_resumes_with_continue_prompt(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    with pytest.raises(UsageLimitReached):
        run_slice(_slice(), repo_path=repo, check_cmd="echo ok", worktrees=wts,
                  executor=Executor(runner=Script(LIMIT)))
    agent = Script(_ok("s2"))
    result = run_slice(_slice(), repo_path=repo, check_cmd="echo ok", worktrees=wts,
                       executor=Executor(runner=agent), resume_session="lim")
    assert result.status == "done"
    assert agent.prompts == [CONTINUE_PROMPT]
    assert "--resume" in agent.argvs[0] and "lim" in agent.argvs[0]
    assert result.session_id == "s2"


def test_resume_without_worktree_starts_fresh(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    agent = Script(_ok())
    result = run_slice(_slice(), repo_path=repo, check_cmd="echo ok", worktrees=wts,
                       executor=Executor(runner=agent), resume_session="gone")
    assert result.status == "done"
    assert agent.prompts[0] != CONTINUE_PROMPT
    assert "--resume" not in agent.argvs[0]


def test_resume_after_commit_does_not_fail_on_empty_commit(repo, tmp_path) -> None:
    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    first = run_slice(_slice(), repo_path=repo, check_cmd="echo ok", worktrees=wts,
                      executor=Executor(runner=Script(_ok())))
    assert first.status == "done"

    class NoEdit:
        def __call__(self, argv, cwd, stdin) -> RunOutput:
            return _ok("s3")

    again = run_slice(_slice(), repo_path=repo, check_cmd="echo ok", worktrees=wts,
                      executor=Executor(runner=NoEdit()), resume_session="s1")
    assert again.status == "done"
    assert again.commit == first.commit


def test_limit_during_review_fix_raises(repo, tmp_path) -> None:
    from nightshift.review import Finding, ReviewResult

    class OneBlocking:
        def review(self, path, sl):
            return ReviewResult([Finding("blocking", "bug")])

    wts = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
    with pytest.raises(UsageLimitReached):
        run_slice(_slice(), repo_path=repo, check_cmd="echo ok", worktrees=wts,
                  executor=Executor(runner=Script(_ok(), LIMIT)), reviewer=OneBlocking())
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_daemon_limits.py -q`
Expected: FAIL, `ImportError: cannot import name 'CONTINUE_PROMPT'`.

- [ ] **Step 3: Implement** (in `src/nightshift/pipeline.py`)

Add the import and the prompt near the top:

```python
from .limits import UsageLimitReached

CONTINUE_PROMPT = (
    "You were interrupted by the Claude usage limit and the limit has now reset. "
    "Continue the same task from where you stopped: check what is already done in "
    "this worktree, finish the rest, and make sure the acceptance criteria are met. "
    "Do NOT commit, push, or create branches — the Nightshift harness handles git."
)
```

Replace `run_slice` with:

```python
def run_slice(
    sl: Slice,
    *,
    repo_path: Path | str,
    check_cmd: str,
    worktrees: WorktreeManager,
    executor: Executor,
    base_branch: str = "main",
    max_attempts: int = 3,
    reviewer=None,
    review_rounds: int = 2,
    resume_session: str | None = None,
) -> SliceResult:
    """Work -> check -> review -> one commit.

    ``resume_session`` continues a slice the usage limit interrupted: its preserved
    worktree is reattached and the agent session resumed with ``CONTINUE_PROMPT``.
    Raises ``UsageLimitReached`` if the limit hits; the worktree is left in place.
    """
    resuming = resume_session is not None and worktrees.exists(sl.id)
    wt = worktrees.attach(sl.id) if resuming else worktrees.create(sl.id, base_branch)

    # Work: iterate until the check is green (or the cap trips).
    green = False
    attempts = 0
    last_session: str | None = resume_session if resuming else None
    while attempts < max_attempts:
        attempts += 1
        if resuming and attempts == 1:
            exec_result = executor.execute_prompt(wt.path, CONTINUE_PROMPT, last_session)
        else:
            exec_result = executor.execute(wt.path, sl, resume_session=last_session)
        last_session = exec_result.session_id or last_session
        if exec_result.usage_limited:
            raise UsageLimitReached(last_session)
        if run_check(check_cmd, wt.path).passed:
            green = True
            break

    def _blocked(reason: str) -> SliceResult:
        sl.status = "blocked"
        sl.attempts = attempts
        worktrees.preserve(sl.id)
        return SliceResult(sl.id, "blocked", attempts, None, wt.branch, reason, last_session)

    if not green:
        return _blocked(f"check failed after {attempts} attempt(s)")

    # Ship gate: always-on AI review (blocking findings loop back to a fix round).
    if reviewer is not None:
        for _ in range(review_rounds):
            review = reviewer.review(wt.path, sl)
            if not review.blocking:
                break
            from .review import fix_prompt

            res = executor.execute_prompt(wt.path, fix_prompt(review.blocking), last_session)
            last_session = res.session_id or last_session
            if res.usage_limited:
                raise UsageLimitReached(last_session)
            if not run_check(check_cmd, wt.path).passed:
                return _blocked("review fix broke the check")
        else:
            return _blocked("blocking review findings unresolved")

    commit_fn = _commit_if_changes if resuming else _commit_all
    commit = commit_fn(wt.path, _commit_message(sl))
    sl.status = "done"
    sl.attempts = attempts
    return SliceResult(sl.id, "done", attempts, commit, wt.branch, "check passed", last_session)
```

- [ ] **Step 4: Run all tests**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add src/nightshift/pipeline.py tests/test_daemon_limits.py
git commit -m "Pipeline: raise on the usage limit and resume an interrupted slice"
```

---

### Task 6: daemon pause/resume, events and state

**Files:**
- Modify: `src/nightshift/daemon.py`
- Test: `tests/test_daemon_limits.py` (append)

Behaviour:
- New constructor args: `events: EventLog | None = None`, `state: StateStore | None = None`,
  `clock: Callable[[], float] = time.time`, `reset_lookup: Callable[[str | None], float | None] = reset_time`.
  `_emit(kind, slice_id=None, **data)` and the state helpers do nothing when the
  collaborator is `None`, so existing tests are unaffected.
- New status constant `LIMITED = "limited"`.
- `_run_one` resumes when `runtime` has `interrupted: True` for the slice (passes
  `resume_session=runtime.get(id)["session_id"]`), and turns `UsageLimitReached` into
  `SliceResult(id, LIMITED, 0, None, branch, "usage limit", session_id)`.
- `_integrate`: `LIMITED` goes to `_limited(work)`. A `UsageLimitReached` raised during
  integration or the babysit prepare (resolver) aborts the rebase in the worktree and
  also goes to `_limited(work)`.
- `_limited(work)`: preserve the worktree, `source.set_status(id, "ready")`, state phase
  `queued` with detail `usage limit`, then `_pause(work.session_id)`. Returns the
  `LIMITED` result. It does **not** call `set_blocked` and does not notify "blocked".
- `_pause(session_id)`: `until = resume_at(reset_lookup(session_id), clock())`. If not
  already paused: set `paused_until`, log, emit `paused_limit` with `until`, set state
  daemon `status="paused_limit", paused_until=until`. If already paused, keep the later time.
- `tick()` order: list slices; babysit `_watch_reviews` (no Claude tokens, keeps
  settling merges while paused); then if `paused_until` is set and `clock() < paused_until`
  log `paused until HH:MM` and return `[]`; if it has passed, clear it, emit `resumed`,
  set state daemon `status="running"`.
- End-of-tick runtime bookkeeping: `LIMITED` results are recorded with
  `interrupted=True`; `BLOCKED` with `interrupted=False`; `DONE`/`IN_REVIEW` forgotten.
- Events and state for the existing paths:
  - batch start: `slice_started` + phase `working` for each slice.
  - `_block`: `blocked` with `reason` + phase `blocked` with `detail`.
  - local merge: `merged` with `commit` + `drop_slice`.
  - babysit PR opened: `review_ready` with `pr=url` + phase `in-review` with `pr`.
  - `_watch_reviews`: MERGED → `merged` + `drop_slice`; CLOSED → `blocked` with reason + phase `blocked`.
- Idle kinds, emitted only when the kind changes (stored in `self._idle`, reset to
  `None` when a batch runs):
  - every slice `done` → `all_done`.
  - no slice `in-progress` or `in-review`, and some not done → `stalled` with
    `waiting` = `{id: status}` for the open slices. Also log it.
  - otherwise nothing.
- `run_forever`: set state daemon `status="running", pid=os.getpid()` at start; catch
  `Exception` from `tick()` with `log.exception("tick failed")` and keep going; in
  `finally` set state `status="stopped"` and emit `daemon_exit` with `reason`
  (`"stopped"` normally, `"<ExcType>: <msg>"` for a `BaseException` like Ctrl-C).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_daemon_limits.py`)

```python
from nightshift.config import RepoConfig
from nightshift.daemon import LIMITED, Daemon
from nightshift.events import EventLog
from nightshift.limits import FALLBACK_WAIT, RESET_GRACE
from nightshift.runtime import Runtime
from nightshift.source import LocalMdSource
from nightshift.state import StateStore


def _write_slice(repo, sid, status="ready", deps=None) -> None:
    deps_line = f"depends_on: {deps}\n" if deps is not None else ""
    (repo / ".slices" / f"{sid}.md").write_text(
        f"---\nid: {sid}\ntitle: {sid}\nstatus: {status}\n{deps_line}---\n## Goal\ndo it\n",
        encoding="utf-8",
    )


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Flip:
    """Hits the limit while `limited` is True, works normally otherwise."""

    def __init__(self):
        self.limited = True
        self.prompts: list[str] = []

    def __call__(self, argv, cwd, stdin) -> RunOutput:
        self.prompts.append(stdin)
        if self.limited:
            return LIMIT
        Path(cwd, f"{Path(cwd).name}.txt").write_text("x", encoding="utf-8")
        return _ok("s-after")


def _daemon(repo, tmp_path, agent, clock, resets=None) -> Daemon:
    return Daemon(
        source=LocalMdSource(repo),
        repo_cfg=RepoConfig(name="demo", path=repo, check="echo ok", base_branch="main"),
        executor=Executor(runner=agent),
        worktrees=WorktreeManager(repo, worktrees_root=tmp_path / "wts"),
        runtime=Runtime(tmp_path / "home" / "runtime.json"),
        events=EventLog(tmp_path / "home" / "events.jsonl", clock=clock),
        state=StateStore(tmp_path / "home" / "state.json", clock=clock),
        clock=clock,
        reset_lookup=lambda sid: resets,
    )


def _kinds(tmp_path) -> list[str]:
    events, _ = EventLog(tmp_path / "home" / "events.jsonl").read_from(0)
    return [e["kind"] for e in events]


def test_limit_pauses_instead_of_blocking(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    clock = Clock()
    d = _daemon(repo, tmp_path, Flip(), clock, resets=5000.0)
    results = d.tick()
    assert [r.status for r in results] == [LIMITED]
    assert LocalMdSource(repo).get("slice-001").status == "ready"  # not blocked
    assert d.paused_until == 5000.0 + RESET_GRACE
    assert _kinds(tmp_path) == ["slice_started", "paused_limit"]
    snap = StateStore(tmp_path / "home" / "state.json").snapshot()
    assert snap["daemon"]["status"] == "paused_limit"
    assert snap["slices"]["slice-001"]["phase"] == "queued"
    assert Runtime(tmp_path / "home" / "runtime.json").get("slice-001")["interrupted"] is True


def test_paused_tick_does_nothing_then_resumes_session(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    clock = Clock()
    agent = Flip()
    d = _daemon(repo, tmp_path, agent, clock, resets=5000.0)
    d.tick()
    agent.limited = False
    clock.t = 2000.0
    assert d.tick() == []  # still paused
    assert len(agent.prompts) == 1
    clock.t = 5000.0 + RESET_GRACE
    results = d.tick()
    assert [r.status for r in results] == ["done"]
    assert agent.prompts[-1] == CONTINUE_PROMPT
    assert d.paused_until is None
    assert _kinds(tmp_path)[-3:] == ["resumed", "slice_started", "merged"]
    assert StateStore(tmp_path / "home" / "state.json").snapshot()["daemon"]["status"] == "running"
    assert Runtime(tmp_path / "home" / "runtime.json").get("slice-001") == {}


def test_unknown_reset_uses_fallback_and_one_pause_event(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002")
    clock = Clock()
    d = _daemon(repo, tmp_path, Flip(), clock, resets=None)
    d.tick()
    assert d.paused_until == 1000.0 + FALLBACK_WAIT
    assert _kinds(tmp_path).count("paused_limit") == 1


def test_all_done_and_stalled_emitted_once(repo, tmp_path) -> None:
    clock = Clock()
    agent = Flip()
    agent.limited = False
    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002", status="blocked")
    _write_slice(repo, "slice-003", deps="[slice-002]")
    d = _daemon(repo, tmp_path, agent, clock)
    d.tick()  # runs slice-001
    d.tick()  # idle: 002 blocked, 003 waits on it
    d.tick()
    assert _kinds(tmp_path).count("stalled") == 1
    events, _ = EventLog(tmp_path / "home" / "events.jsonl").read_from(0)
    stalled = next(e for e in events if e["kind"] == "stalled")
    assert stalled["waiting"] == {"slice-002": "blocked", "slice-003": "ready"}
    LocalMdSource(repo).set_status("slice-002", "done")
    LocalMdSource(repo).set_status("slice-003", "done")
    d.tick()
    d.tick()
    assert _kinds(tmp_path).count("all_done") == 1


def test_blocked_slice_emits_event_and_state(repo, tmp_path) -> None:
    _write_slice(repo, "slice-001")
    clock = Clock()
    agent = Flip()
    agent.limited = False
    d = _daemon(repo, tmp_path, agent, clock)
    d.repo_cfg.check = "exit 1"
    d.max_attempts = 1
    d.tick()
    assert "blocked" in _kinds(tmp_path)
    snap = StateStore(tmp_path / "home" / "state.json").snapshot()
    assert snap["slices"]["slice-001"]["phase"] == "blocked"
    assert Runtime(tmp_path / "home" / "runtime.json").get("slice-001")["interrupted"] is False


def test_limit_during_conflict_resolution_pauses(repo, tmp_path) -> None:
    from nightshift.limits import UsageLimitReached

    class LimitResolver:
        def __call__(self, worktree_path):
            raise UsageLimitReached("res-1")

    class SameFile:
        def __call__(self, argv, cwd, stdin) -> RunOutput:
            readme = Path(cwd, "README.md")
            readme.write_text(readme.read_text(encoding="utf-8") + Path(cwd).name + "\n",
                              encoding="utf-8")
            return _ok()

    _write_slice(repo, "slice-001")
    _write_slice(repo, "slice-002")
    clock = Clock()
    d = _daemon(repo, tmp_path, SameFile(), clock, resets=9000.0)
    d.resolver = LimitResolver()
    results = d.tick()
    assert sorted(r.status for r in results) == ["done", LIMITED]
    limited = next(r for r in results if r.status == LIMITED)
    assert LocalMdSource(repo).get(limited.slice_id).status == "ready"
    assert d.paused_until == 9000.0 + RESET_GRACE
    wt = tmp_path / "wts" / limited.slice_id
    assert "rebase in progress" not in _git(wt, "status")  # the rebase was aborted


def test_run_forever_records_exit_and_survives_tick_errors(repo, tmp_path) -> None:
    clock = Clock()
    d = _daemon(repo, tmp_path, Flip(), clock)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gh down")
        return []

    d.tick = boom  # type: ignore[method-assign]
    d.run_forever(interval=0, max_ticks=2, sleep=lambda s: None)
    assert calls["n"] == 2
    snap = StateStore(tmp_path / "home" / "state.json").snapshot()
    assert snap["daemon"]["status"] == "stopped"
    events, _ = EventLog(tmp_path / "home" / "events.jsonl").read_from(0)
    assert events[-1] == {"ts": 1000.0, "kind": "daemon_exit", "reason": "stopped"}


def test_run_forever_records_interrupt_reason(repo, tmp_path) -> None:
    d = _daemon(repo, tmp_path, Flip(), Clock())

    def interrupt():
        raise KeyboardInterrupt()

    d.tick = interrupt  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        d.run_forever(interval=0, max_ticks=1, sleep=lambda s: None)
    events, _ = EventLog(tmp_path / "home" / "events.jsonl").read_from(0)
    assert events[-1]["reason"] == "KeyboardInterrupt: "
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_daemon_limits.py -q`
Expected: FAIL, `ImportError: cannot import name 'LIMITED'`.

- [ ] **Step 3: Implement** (in `src/nightshift/daemon.py`)

Imports to add:

```python
import os

from .events import EventLog
from .limits import UsageLimitReached, reset_time, resume_at
from .pipeline import _git_ok
from .state import StateStore
```

Constants: add `LIMITED = "limited"` next to the others.

Constructor: add the four parameters and store them; add `self.paused_until: float | None = None` and `self._idle: str | None = None`.

Helpers:

```python
    def _emit(self, kind: str, slice_id: str | None = None, **data) -> None:
        if self.events is not None:
            self.events.emit(kind, slice_id, **data)

    def _phase(self, slice_id: str, phase: str, **fields) -> None:
        if self.state is not None:
            self.state.set_slice(slice_id, phase, **fields)

    def _drop(self, slice_id: str) -> None:
        if self.state is not None:
            self.state.drop_slice(slice_id)

    def _daemon_state(self, **fields) -> None:
        if self.state is not None:
            self.state.set_daemon(**fields)
```

`_run_one`:

```python
    def _run_one(self, sl: Slice) -> SliceResult:
        entry = self.runtime.get(sl.id) if self.runtime is not None else {}
        resume = entry.get("session_id") if entry.get("interrupted") else None
        try:
            return run_slice(
                sl,
                repo_path=self.repo_cfg.path,
                check_cmd=self.repo_cfg.check,
                worktrees=self.worktrees,
                executor=self.executor,
                base_branch=self.repo_cfg.base_branch,
                max_attempts=self.max_attempts,
                reviewer=self.reviewer,
                resume_session=resume,
            )
        except UsageLimitReached as exc:
            return SliceResult(
                sl.id, LIMITED, 0, None, self.worktrees.branch_for(sl.id), "usage limit",
                exc.session_id or resume,
            )
        except Exception as exc:  # a git/exec error must not kill the tick
            return SliceResult(
                sl.id, "blocked", 0, None, self.worktrees.branch_for(sl.id), f"error: {exc}"
            )
```

`_block`: after `self.notifier.notify(...)` add:

```python
        self._emit("blocked", work.slice_id, reason=reason)
        self._phase(work.slice_id, BLOCKED, detail=reason)
```

New `_limited` and `_pause`:

```python
    def _limited(self, work: SliceResult) -> SliceResult:
        """Usage limit: keep the worktree, put the slice back in the queue, pause."""
        self.worktrees.preserve(work.slice_id)
        self.source.set_status(work.slice_id, "ready")
        self._phase(work.slice_id, "queued", detail="usage limit")
        self._pause(work.session_id)
        return SliceResult(
            work.slice_id, LIMITED, work.attempts, work.commit, work.branch, "usage limit",
            work.session_id,
        )

    def _pause(self, session_id: str | None) -> None:
        until = resume_at(self.reset_lookup(session_id), self.clock())
        if self.paused_until is not None:
            self.paused_until = max(self.paused_until, until)
            return
        self.paused_until = until
        log.warning("usage limit reached; pausing until %s", time.strftime("%H:%M", time.localtime(until)))
        self._emit("paused_limit", until=until)
        self._daemon_state(status="paused_limit", paused_until=until)
```

`_integrate`: at the top:

```python
        if work.status == LIMITED:
            return self._limited(work)
```

and wrap the rest (from `if work.status != DONE` onward) so a resolver limit pauses:

```python
        try:
            return self._ship(work)
        except UsageLimitReached:
            _git_ok(self.worktrees.path_for(work.slice_id), "rebase", "--abort")
            return self._limited(work)
```

Move the current body of `_integrate` (from `if work.status != DONE:` to the end) into a
new method `_ship(self, work)`. In `_ship`, on local merge success add
`self._emit("merged", work.slice_id, commit=result.commit)` and `self._drop(work.slice_id)`.

`_open_for_babysit`: after `self.notifier.notify("in-review", ...)` add:

```python
        self._emit("review_ready", work.slice_id, pr=url)
        self._phase(work.slice_id, IN_REVIEW, pr=url)
```

`_watch_reviews`: in the MERGED branch add `self._emit("merged", sl.id)` and
`self._drop(sl.id)`; in the CLOSED branch add
`self._emit("blocked", sl.id, reason="PR closed without merging (babysit)")` and
`self._phase(sl.id, BLOCKED, detail="PR closed without merging (babysit)")`.

New `_note_idle`:

```python
    def _note_idle(self, slices: list[Slice]) -> None:
        open_ = {s.id: s.status for s in slices if s.status != DONE}
        if not open_:
            kind: str | None = "all_done"
        elif any(st in (IN_PROGRESS, IN_REVIEW) for st in open_.values()):
            kind = None
        else:
            kind = "stalled"
        if kind is not None and kind != self._idle:
            if kind == "stalled":
                log.warning("stalled: nothing runnable; open slices: %s", open_)
                self._emit(kind, waiting=open_)
            else:
                self._emit(kind)
        self._idle = kind
```

`tick()`:

```python
    def tick(self) -> list[SliceResult]:
        slices = self.source.list_all()
        if not slices:
            log.info("idle — no slices")
            return []
        if self.repo_cfg.babysit:
            self._watch_reviews(slices)
        if self.paused_until is not None:
            if self.clock() < self.paused_until:
                log.info("paused (usage limit) until %s",
                         time.strftime("%H:%M", time.localtime(self.paused_until)))
                return []
            self.paused_until = None
            self._emit("resumed")
            self._daemon_state(status="running", paused_until=None)
            log.info("usage limit reset; resuming")
        if self.runtime is not None:
            self.runtime.reconcile(known_slice_ids={s.id for s in slices}, worktrees=self.worktrees)
        batch = self._select_batch(Dag.build(slices).runnable())
        if not batch:
            log.info("idle — %d slice(s), none runnable", len(slices))
            self._note_idle(slices)
            return []
        self._idle = None
        log.info("running %d slice(s): %s", len(batch), ", ".join(s.id for s in batch))
        for sl in batch:
            self.source.set_status(sl.id, IN_PROGRESS)
            self._emit("slice_started", sl.id)
            self._phase(sl.id, "working")
        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            work_results = list(pool.map(self._run_one, batch))
        results = [self._integrate(work) for work in work_results]
        for r in results:
            if r.status == DONE:
                log.info("%s done (%s)", r.slice_id, (r.commit or "")[:8])
            elif r.status == IN_REVIEW:
                log.info("%s in review: %s", r.slice_id, r.detail)
            elif r.status == LIMITED:
                log.warning("%s paused on the usage limit", r.slice_id)
            else:
                log.warning("%s BLOCKED — %s", r.slice_id, r.detail)
        if self.runtime is not None:
            for r in results:
                if r.status in (DONE, IN_REVIEW):
                    self.runtime.forget(r.slice_id)
                else:
                    self.runtime.record(
                        r.slice_id,
                        session_id=r.session_id,
                        branch=r.branch,
                        interrupted=r.status == LIMITED,
                    )
        return results
```

Note: `paused_until=None` in `_daemon_state` stores `null` in `state.json`; that's fine.

`run_forever`:

```python
    def run_forever(
        self,
        *,
        interval: float = 30,
        max_ticks: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._daemon_state(status="running", pid=os.getpid())
        reason = "stopped"
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                try:
                    self.tick()
                except Exception:
                    log.exception("tick failed; continuing")
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                sleep(interval)
        except BaseException as exc:
            reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            log.info("daemon exiting (%s)", reason)
            self._daemon_state(status="stopped")
            self._emit("daemon_exit", reason=reason)
```

- [ ] **Step 4: Run all tests**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass. If an older daemon test asserted `run_forever` re-raises a tick
exception, update it to the new behaviour (log and continue) and say so in the report.

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add src/nightshift/daemon.py tests/test_daemon_limits.py
git commit -m "Daemon: pause on the usage limit and resume; events and state for every phase"
```

---

### Task 7: `nsh status`, `nsh wait`, and wiring the daemon

**Files:**
- Create: `src/nightshift/wait.py`
- Modify: `src/nightshift/daemon.py` (`run_daemon_cli`)
- Modify: `src/nightshift/cli.py`
- Test: `tests/test_wait.py`

Behaviour:
- `run_daemon_cli(..., home_root: Path | None = None)` builds `home = repo_home(repo_cfg.name, home_root)` and passes `runtime=Runtime(home / "runtime.json")`, `events=EventLog(home / "events.jsonl")`, `state=StateStore(home / "state.json")`.
- `wait_for_attention(log, cursor_path, *, timeout, poll, sleep, clock)`: if the cursor file is missing, start at the log's **end** (the master agent reads `nsh status` for the current picture). Reads from the cursor, saves the new cursor after every read, returns the `ATTENTION` events found, or `[]` on timeout.
- `nsh wait` prints each event as one JSON line and exits 0; on timeout prints `{"kind": "timeout"}` and exits 2.
- `nsh status` prints `render_status`; `--json` prints the raw snapshot.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for nsh wait / nsh status and the daemon's per-repo wiring."""

import json

from typer.testing import CliRunner

from nightshift.cli import app
from nightshift.events import EventLog
from nightshift.paths import repo_home
from nightshift.state import StateStore
from nightshift.wait import run_status_cli, run_wait_cli, wait_for_attention


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_wait_starts_at_end_without_cursor(tmp_path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.emit("blocked", "issue-1")  # history before the first wait
    clock = Clock()

    def sleep(s):
        clock.t += s
        log.emit("review_ready", "issue-2", pr="u")

    hits = wait_for_attention(log, tmp_path / "wait.cursor", timeout=10, poll=1,
                              sleep=sleep, clock=clock)
    assert [(e["kind"], e["slice"]) for e in hits] == [("review_ready", "issue-2")]


def test_wait_resumes_from_cursor_and_skips_noise(tmp_path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    cursor = tmp_path / "wait.cursor"
    cursor.write_text("0", encoding="utf-8")
    log.emit("slice_started", "issue-1")
    log.emit("blocked", "issue-1", reason="x")
    hits = wait_for_attention(log, cursor, timeout=0, poll=1, sleep=lambda s: None, clock=Clock())
    assert [e["kind"] for e in hits] == ["blocked"]
    again = wait_for_attention(log, cursor, timeout=0, poll=1, sleep=lambda s: None, clock=Clock())
    assert again == []


def test_wait_times_out(tmp_path) -> None:
    clock = Clock()

    def sleep(s):
        clock.t += s

    hits = wait_for_attention(EventLog(tmp_path / "e.jsonl"), tmp_path / "c", timeout=3, poll=1,
                              sleep=sleep, clock=clock)
    assert hits == []
    assert clock.t >= 3


def test_wait_corrupt_cursor_starts_at_end(tmp_path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.emit("blocked", "issue-1")
    cursor = tmp_path / "wait.cursor"
    cursor.write_text("junk", encoding="utf-8")
    assert wait_for_attention(log, cursor, timeout=0, poll=1, sleep=lambda s: None, clock=Clock()) == []


def _config(tmp_path, repo) -> object:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"repos:\n  demo:\n    path: {repo.as_posix()}\n    check: echo ok\n",
                   encoding="utf-8")
    return cfg


def test_run_status_cli_text_and_json(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _config(tmp_path, repo)
    home = repo_home("demo", tmp_path / "ns")
    StateStore(home / "state.json", clock=lambda: 5.0).set_slice("issue-1", "working")
    text = run_status_cli(repo, config_path=cfg, as_json=False, home_root=tmp_path / "ns", now=65.0)
    assert "issue-1" in text and "working" in text
    raw = json.loads(run_status_cli(repo, config_path=cfg, as_json=True, home_root=tmp_path / "ns"))
    assert raw["slices"]["issue-1"]["phase"] == "working"


def test_run_wait_cli_reads_repo_home(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _config(tmp_path, repo)
    home = repo_home("demo", tmp_path / "ns")
    (home).mkdir(parents=True)
    (home / "wait.cursor").write_text("0", encoding="utf-8")
    EventLog(home / "events.jsonl").emit("all_done")
    hits = run_wait_cli(repo, config_path=cfg, timeout=0, poll=1, home_root=tmp_path / "ns")
    assert [e["kind"] for e in hits] == ["all_done"]


def test_cli_wait_and_status(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr("nightshift.wait.run_wait_cli",
                        lambda *a, **k: [{"kind": "blocked", "slice": "issue-1"}])
    res = runner.invoke(app, ["wait"])
    assert res.exit_code == 0
    assert json.loads(res.output.strip()) == {"kind": "blocked", "slice": "issue-1"}
    monkeypatch.setattr("nightshift.wait.run_wait_cli", lambda *a, **k: [])
    res = runner.invoke(app, ["wait", "--timeout", "1"])
    assert res.exit_code == 2
    assert json.loads(res.output.strip()) == {"kind": "timeout"}
    monkeypatch.setattr("nightshift.wait.run_status_cli", lambda *a, **k: "daemon: running")
    res = runner.invoke(app, ["status", "--json"])
    assert res.exit_code == 0 and "daemon: running" in res.output


def test_run_daemon_cli_wires_repo_home(tmp_path) -> None:
    import subprocess

    from nightshift.daemon import run_daemon_cli
    from nightshift.executor import Executor, RunOutput

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    cfg = _config(tmp_path, repo)
    ex = Executor(runner=lambda a, c, s: RunOutput(0, "{}", ""))
    assert run_daemon_cli(repo, config_path=cfg, once=True, executor=ex,
                          home_root=tmp_path / "ns") == []
```

Note for `test_run_daemon_cli_wires_repo_home`: the repo has no `.slices`, so `tick()`
returns `[]` before touching state. It only proves the wiring runs. Coverage of the
three constructors comes from this call.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_wait.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'nightshift.wait'`.

- [ ] **Step 3: Implement**

`src/nightshift/wait.py`:

```python
"""`nsh wait` and `nsh status`: how the master agent follows the daemon (v2, Step 6)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .events import ATTENTION, EventLog
from .paths import repo_home
from .state import StateStore, render_status


def _read_cursor(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def wait_for_attention(
    log: EventLog,
    cursor_path: Path,
    *,
    timeout: float | None,
    poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Block until an ATTENTION event arrives after the saved cursor; [] on timeout."""
    offset = _read_cursor(cursor_path)
    if offset is None:
        offset = log.end_offset()
    deadline = None if timeout is None else clock() + timeout
    while True:
        events, offset = log.read_from(offset)
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_path.write_text(str(offset), encoding="utf-8")
        hits = [e for e in events if e.get("kind") in ATTENTION]
        if hits:
            return hits
        if deadline is not None and clock() >= deadline:
            return []
        sleep(poll)


def _home(repo, config_path, home_root) -> Path:
    name = Config.load(config_path).repo(str(repo)).name
    return repo_home(name, home_root)


def run_wait_cli(repo, *, config_path=None, timeout=None, poll=2.0, home_root=None) -> list[dict]:
    home = _home(repo, config_path, home_root)
    return wait_for_attention(
        EventLog(home / "events.jsonl"), home / "wait.cursor", timeout=timeout, poll=poll
    )


def run_status_cli(repo, *, config_path=None, as_json=False, home_root=None, now=None) -> str:
    snapshot = StateStore(_home(repo, config_path, home_root) / "state.json").snapshot()
    if as_json:
        return json.dumps(snapshot, indent=2)
    return render_status(snapshot, time.time() if now is None else now)
```

`src/nightshift/cli.py`, add (after `daemon`):

```python
@app.command()
def status(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(
        None, "--config", help="Config file (default: ~/.nightshift/config.yaml)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the raw state as JSON"),
) -> None:
    """Show what the daemon is doing: phase and age of every slice, pause state."""
    from nightshift import wait as wait_mod

    typer.echo(wait_mod.run_status_cli(repo, config_path=config, as_json=as_json))


@app.command()
def wait(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(
        None, "--config", help="Config file (default: ~/.nightshift/config.yaml)"
    ),
    timeout: float | None = typer.Option(None, "--timeout", help="Give up after N seconds"),
    poll: float = typer.Option(2.0, "--poll", help="Seconds between checks"),
) -> None:
    """Block until something needs the driving agent; print it as JSON lines."""
    import json

    from nightshift import wait as wait_mod

    events = wait_mod.run_wait_cli(repo, config_path=config, timeout=timeout, poll=poll)
    for event in events:
        typer.echo(json.dumps(event))
    if not events:
        typer.echo(json.dumps({"kind": "timeout"}))
        raise typer.Exit(code=2)
```

(The CLI imports the module, not the functions, so the test's monkeypatch on
`nightshift.wait.run_wait_cli` takes effect.)

`src/nightshift/daemon.py` `run_daemon_cli`: add parameter `home_root: Path | None = None`,
import `from .paths import repo_home` and `from .runtime import Runtime`, and pass:

```python
    home = repo_home(repo_cfg.name, home_root)
    daemon = Daemon(
        ...existing args...,
        runtime=Runtime(home / "runtime.json"),
        events=EventLog(home / "events.jsonl"),
        state=StateStore(home / "state.json"),
    )
```

- [ ] **Step 4: Run all tests**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add src/nightshift/wait.py src/nightshift/cli.py src/nightshift/daemon.py tests/test_wait.py
git commit -m "CLI: nsh status and nsh wait; daemon writes events and state to the repo home"
```

---

### Task 8: babysit skill and docs

**Files:**
- Modify: `skills/nightshift-babysit/SKILL.md` ("Keep watching" section)
- Modify: `README.md` (command list, if it has one: add `nsh status`, `nsh wait`)

- [ ] **Step 1: Replace the "Keep watching" section of `skills/nightshift-babysit/SKILL.md`**

```markdown
## Keep watching

Don't tail the daemon log. Nightshift tells you when it needs you:

1. Run `nsh status` once for the current picture.
2. Start `nsh wait --repo <repo>` as a **background task**. It blocks until something
   needs you, prints one JSON line per event, and exits:
   - `review_ready` (with `pr`): a PR is waiting. Do a pass.
   - `blocked` (with `reason`): read the reason; fix the issue text or file a follow-up.
   - `paused_limit` (with `until`) / `resumed`: the Claude usage limit. Nothing to do.
   - `stalled` (with `waiting`): nothing can run. Usually a blocked dependency.
   - `all_done`: the queue is empty.
   - `daemon_exit` (with `reason`): the daemon stopped. Restart it.
3. Handle the event, then start `nsh wait` again. It remembers where it stopped, so
   nothing is missed between runs.
```

- [ ] **Step 2: Commit**

```bash
git add skills/nightshift-babysit/SKILL.md README.md
git commit -m "Babysit skill: follow the daemon with nsh wait and nsh status"
```

---

## Self-review notes

- Spec coverage (Phase 1 slice of the design): budget gate with reset from transcript and
  15-minute fallback (Tasks 1, 2, 5, 6); limited slice keeps worktree and session and is
  not charged an attempt (Tasks 5, 6); `state.json` (Task 4, 6); `events.jsonl` and
  `nsh wait` (Tasks 3, 7); `nsh status` (Tasks 4, 7); log on every exit and survive tick
  errors (Task 6); babysit uses the new commands (Task 8). wmux `report-agent`
  forwarding is left for Phase 2's notifier work.
- Known limitation kept on purpose: a slice limited during Ship (resolver) re-enters at
  Work with `CONTINUE_PROMPT`; the agent sees its committed work and the run is cheap.
