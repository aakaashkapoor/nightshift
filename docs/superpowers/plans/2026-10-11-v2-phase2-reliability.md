# Nightshift v2 Phase 2: daemon reliability

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The daemon never wastes a slot, never sits silently, survives crashes, and can be paused or stopped cleanly.

**Architecture:** `tick()` (start a batch, wait for all of it) becomes a worker pool. `poll()` ships whatever finished, then fills free slots with the most important runnable slices. `run_forever` calls `poll()` and wakes as soon as any slice finishes. `tick()` stays as the synchronous "start, wait, ship" pass used by `--once` and the existing tests. A supervisor process restarts the daemon if it dies. A control file carries `nsh pause` / `nsh unpause` / `nsh stop`.

**Tech Stack:** Python 3.11+, typer, pytest (100% branch coverage), ruff, mypy.

**Design:** `docs/superpowers/specs/2026-10-11-nightshift-v2-design.md` (Step 0, Step 1). Field notes #1–4, #15, #17–19, #27, #28.

---

## Rules for every task

- Work in `C:\Users\aakaa\code\nightshift` on branch `v2-phase2`. Python: `.venv/Scripts/python` (bash). LF line endings. On Windows `write_text("...\n")` writes CRLF; use `write_bytes` where byte offsets matter.
- Test-first: write the tests, run them and see them fail, implement, see them pass.
- Before each commit: `.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate` must print `CHECK PASSED` (ruff, mypy, pytest with **100% branch coverage**). Never add `# pragma: no cover` to new logic; the only allowed ones are real subprocess shims and the `while True` CLI loop, as noted below.
- If plan code has a bug (test can't pass, lint/mypy error, logic flaw), fix it minimally and report what and why. No extra features.
- End each commit message with:

```
Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UcNkjNqsVKMUAvGQ1Pates
```

- Do not push.

## File map

| File | Change | Responsibility |
|---|---|---|
| `src/nightshift/slice.py` | modify | `touches()`: Touches section (falls back to Scope hints), stops at a blank line |
| `src/nightshift/scheduler.py` | modify | `overlaps()`, `Dag.dependant_counts()`, `Dag.waiting_reason()` |
| `src/nightshift/control.py` | create | `Control`: pause / stop flags in `control.json` |
| `src/nightshift/supervisor.py` | create | Restart the daemon child on crash |
| `src/nightshift/__main__.py` | create | `python -m nightshift` |
| `src/nightshift/config.py` | modify | `RepoConfig.main_gate` |
| `src/nightshift/events.py` | modify | `main_red` joins `ATTENTION` |
| `src/nightshift/daemon.py` | modify | Pool, critical path, recovery, main gate, control, reload, why-not-runnable |
| `src/nightshift/cli.py` | modify | `nsh pause`, `nsh unpause`, `nsh stop`; supervised `nsh daemon` |
| `src/nightshift/state.py` | modify | Render `paused` / `stopping` / main red |
| tests | create/modify | `tests/test_touches.py`, `tests/test_control.py`, `tests/test_supervisor.py`, `tests/test_pool.py` |

---

### Task 1: `touches()` and path-prefix overlap

Field notes #4, #21, #28. Issues use either a `## Touches` section (v2 template) or the
older `## Scope hints`. Both may be list items (`- src/a.ts`) or one comma-separated
line. The block ends at the next `## ` heading **or the first blank line after a path
was collected**, so a trailing prose paragraph is never read as paths.

**Files:**
- Modify: `src/nightshift/slice.py` (add `touches`; make `scope_hints` return `touches()` for backward compatibility)
- Modify: `src/nightshift/scheduler.py` (add `overlaps`)
- Test: `tests/test_touches.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_touches.py -q`
Expected: FAIL (`AttributeError: 'Slice' object has no attribute 'touches'`).

- [ ] **Step 3: Implement**

In `src/nightshift/slice.py`, replace `scope_hints` with:

```python
    def touches(self) -> set[str]:
        """Paths this slice will change, for the scheduler's lock (v2 design, Step 1).

        Read from '## Touches', or the older '## Scope hints' if there is no Touches
        section. Accepts list items or comma-separated paths. The block ends at the next
        '## ' heading or at the first blank line after a path, so a closing prose
        paragraph is never read as paths. A token counts only if it looks like a path.
        """
        lines = self.body.splitlines()
        heads = [line.strip().lower() for line in lines]
        if "## touches" in heads:
            start = heads.index("## touches")
        elif "## scope hints" in heads:
            start = heads.index("## scope hints")
        else:
            return set()
        paths: set[str] = set()
        for line in lines[start + 1 :]:
            stripped = line.strip()
            if stripped.startswith("## "):
                break
            if not stripped:
                if paths:
                    break
                continue
            for token in re.split(r"[,\s]+", stripped):
                token = token.strip("`-*() ")
                if token and ("." in token or "/" in token or "\\" in token):
                    paths.add(token)
        return paths

    def scope_hints(self) -> set[str]:
        """Old name for ``touches()``."""
        return self.touches()
```

In `src/nightshift/scheduler.py`, add:

```python
def _norm(path: str) -> str:
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def overlaps(a: set[str], b: set[str]) -> bool:
    """True if any path in ``a`` equals, contains, or is inside any path in ``b``."""
    for x in map(_norm, a):
        for y in map(_norm, b):
            if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False
```

- [ ] **Step 4: Run all tests**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass. If an existing test of `scope_hints` relied on reading past a blank line, update it to the new rule and report it.

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add src/nightshift/slice.py src/nightshift/scheduler.py tests/test_touches.py
git commit -m "Scheduler: Touches section, blank-line-terminated, compared by path prefix"
```

---

### Task 2: critical path and why-not-runnable

**Files:**
- Modify: `src/nightshift/scheduler.py` (`Dag.dependant_counts`, `Dag.waiting_reason`)
- Test: `tests/test_touches.py` (append)

- [ ] **Step 1: Write the failing tests** (append)

```python
from nightshift.scheduler import Dag


def _dag_slice(sid, status="ready", deps=None) -> Slice:
    s = Slice.parse(f"---\nid: {sid}\ntitle: {sid}\nstatus: {status}\n---\n")
    s.depends_on = deps or []
    return s


def test_dependant_counts_are_transitive() -> None:
    # a <- b <- c, a <- d ; e alone
    dag = Dag.build([
        _dag_slice("a"),
        _dag_slice("b", deps=["a"]),
        _dag_slice("c", deps=["b"]),
        _dag_slice("d", deps=["a"]),
        _dag_slice("e"),
    ])
    assert dag.dependant_counts() == {"a": 3, "b": 1, "c": 0, "d": 0, "e": 0}


def test_waiting_reason_names_unfinished_deps() -> None:
    dag = Dag.build([
        _dag_slice("a", status="blocked"),
        _dag_slice("b", status="done"),
        _dag_slice("c", deps=["a", "b"]),
        _dag_slice("d"),
    ])
    assert dag.waiting_reason("c") == "waiting on a (blocked)"
    assert dag.waiting_reason("d") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_touches.py -q`
Expected: FAIL (`AttributeError: 'Dag' object has no attribute 'dependant_counts'`).

- [ ] **Step 3: Implement** (methods on `Dag`)

```python
    def dependant_counts(self) -> dict[str, int]:
        """How many slices (transitively) wait on each slice: the critical-path weight."""
        counts: dict[str, int] = {}
        for sid in self.slices:
            seen: set[str] = set()
            stack = self.children(sid)
            while stack:
                child = stack.pop()
                if child not in seen:
                    seen.add(child)
                    stack.extend(self.children(child))
            counts[sid] = len(seen)
        return counts

    def waiting_reason(self, sid: str) -> str | None:
        """Why a ready slice can't run yet (unfinished dependencies), or None."""
        pending = [d for d in self.slices[sid].depends_on if self.slices[d].status != DONE]
        if not pending:
            return None
        return "waiting on " + ", ".join(f"{d} ({self.slices[d].status})" for d in pending)
```

- [ ] **Step 4: Run all tests** — Expected: all pass.

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add src/nightshift/scheduler.py tests/test_touches.py
git commit -m "Scheduler: critical-path weights and why a slice is waiting"
```

---

### Task 3: control file (`nsh pause`, `nsh unpause`, `nsh stop`)

`control.json` in the repo home: `{"paused": bool, "stop": bool}`. `nsh resume` already
means "resume a blocked slice", so the opposite of pause is `nsh unpause`.

**Files:**
- Create: `src/nightshift/control.py`
- Modify: `src/nightshift/cli.py`
- Modify: `src/nightshift/wait.py` (reuse its `_home` helper; add `run_control_cli`)
- Test: `tests/test_control.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Pause / unpause / stop via control.json."""

from typer.testing import CliRunner

from nightshift.cli import app
from nightshift.control import Control
from nightshift.paths import repo_home
from nightshift.wait import run_control_cli


def test_defaults_when_missing_or_corrupt(tmp_path) -> None:
    c = Control(tmp_path / "control.json")
    assert (c.paused, c.stop) == (False, False)
    (tmp_path / "control.json").write_text("{nope", encoding="utf-8")
    assert (c.paused, c.stop) == (False, False)


def test_set_and_read_back(tmp_path) -> None:
    c = Control(tmp_path / "home" / "control.json")
    c.set(paused=True)
    assert Control(tmp_path / "home" / "control.json").paused is True
    c.set(stop=True)
    assert (c.paused, c.stop) == (True, True)
    c.set(paused=False, stop=False)
    assert (c.paused, c.stop) == (False, False)


def _config(tmp_path, repo):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"repos:\n  demo:\n    path: {repo.as_posix()}\n    check: echo ok\n",
                   encoding="utf-8")
    return cfg


def test_run_control_cli(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _config(tmp_path, repo)
    root = tmp_path / "ns"
    assert run_control_cli(repo, "pause", config_path=cfg, home_root=root) == "paused: no new slices will start"
    assert Control(repo_home("demo", root) / "control.json").paused is True
    assert run_control_cli(repo, "unpause", config_path=cfg, home_root=root) == "unpaused"
    assert run_control_cli(repo, "stop", config_path=cfg, home_root=root) == (
        "stopping: the daemon exits after the running slices finish"
    )
    c = Control(repo_home("demo", root) / "control.json")
    assert (c.paused, c.stop) == (False, True)


def test_cli_commands(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr("nightshift.wait.run_control_cli",
                        lambda repo, action, **k: seen.append(action) or action)
    runner = CliRunner()
    for cmd in ("pause", "unpause", "stop"):
        res = runner.invoke(app, [cmd])
        assert res.exit_code == 0 and cmd in res.output
    assert seen == ["pause", "unpause", "stop"]
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError: nightshift.control`.

- [ ] **Step 3: Implement**

`src/nightshift/control.py`:

```python
"""Operator controls for a running daemon: pause, unpause, stop after current work.

The CLI writes ``control.json`` in the repo home; the daemon reads it on every poll.
A file (not a signal) works the same on Windows and survives daemon restarts.
"""

from __future__ import annotations

import json
from pathlib import Path


class Control:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def paused(self) -> bool:
        return bool(self._read().get("paused", False))

    @property
    def stop(self) -> bool:
        return bool(self._read().get("stop", False))

    def set(self, **flags: bool) -> None:
        data = self._read()
        data.update(flags)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")
```

In `src/nightshift/wait.py` add:

```python
from .control import Control

_CONTROL = {
    "pause": ({"paused": True}, "paused: no new slices will start"),
    "unpause": ({"paused": False}, "unpaused"),
    "stop": ({"stop": True}, "stopping: the daemon exits after the running slices finish"),
}


def run_control_cli(repo, action: str, *, config_path=None, home_root=None) -> str:
    flags, message = _CONTROL[action]
    Control(_home(repo, config_path, home_root) / "control.json").set(**flags)
    return message
```

In `src/nightshift/cli.py` add three commands sharing one helper:

```python
def _control(action: str, repo: Path, config: Path | None) -> None:
    from nightshift import wait as wait_mod

    typer.echo(wait_mod.run_control_cli(repo, action, config_path=config))


@app.command()
def pause(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(None, "--config", help="Config file"),
) -> None:
    """Stop starting new slices (running ones finish). Undo with `nsh unpause`."""
    _control("pause", repo, config)


@app.command()
def unpause(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(None, "--config", help="Config file"),
) -> None:
    """Let the daemon start new slices again."""
    _control("unpause", repo, config)


@app.command()
def stop(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(None, "--config", help="Config file"),
) -> None:
    """Exit the daemon once the running slices finish."""
    _control("stop", repo, config)
```

- [ ] **Step 4: Run all tests** — all pass.

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add src/nightshift/control.py src/nightshift/wait.py src/nightshift/cli.py tests/test_control.py
git commit -m "Control: nsh pause, nsh unpause and nsh stop via control.json"
```

---

### Task 4: the worker pool

This is the main refactor of `src/nightshift/daemon.py`. Read the whole file first.

**New constructor parameters** (all optional, defaults keep existing tests working):
- `control: Control | None = None`
- `reload: Callable[[], RepoConfig] | None = None`
- `exit_event: str = "daemon_exit"` (the supervised child uses `"child_exit"`)

**New config field** `RepoConfig.main_gate: bool = False` (dataclass default, so tests
are unaffected). In `Config._resolve` it defaults to **on**: `main_gate=bool(entry.get("main_gate", True))`.
Add `"main_red"` to `ATTENTION` in `events.py` (update `test_attention_kinds`).

**New state on the daemon:**

```python
        self._pool: ThreadPoolExecutor | None = None
        self._running: dict[str, Future[SliceResult]] = {}  # slice id -> work, in start order
        self._held: dict[str, set[str]] = {}  # slice id -> its touches, while running
        self._recovered = False
        self._green: str | None = None  # last base commit whose check passed
        self._red: str | None = None  # last base commit whose check failed (already reported)
        self._waiting: dict[str, str] = {}  # last logged why-not-started map
        self._mode = "running"  # running | paused | stopping, mirrored into state.json
```

**Public API:**

```python
    def tick(self) -> list[SliceResult]:
        """One synchronous pass (``--once``, tests): start work, wait for all of it, ship it."""
        self._start_ready()
        return self._settle(wait=True)

    def poll(self) -> list[SliceResult]:
        """Continuous mode: ship whatever finished, then fill the free slots."""
        results = self._settle(wait=False)
        self._start_ready()
        return results
```

**`_start_ready()`** (returns nothing; replaces the top half of the old `tick`). In order:

1. `slices = self.source.list_all()`; if empty: log `idle — no slices` and return.
2. `self._reload_config()` (below).
3. If `self.repo_cfg.babysit`: `self._watch_reviews(slices)`.
4. `self._recover(slices)` (below), once per daemon life.
5. Usage-limit pause: same block as today (`paused_until` check, `resumed` event). Return while paused.
6. Control: if `self.control` and `self.control.stop`: set mode `stopping`, return (no new work). If `self.control.paused`: set mode `paused`, return. Otherwise set mode `running`.
   `_set_mode(mode)`: if it changed, store it, log it, and `self._daemon_state(status=mode)`.
7. Main gate: `if self.repo_cfg.main_gate and not self._main_green(): return`.
8. Runtime reconcile (as today).
9. Pick:

```python
        dag = Dag.build(slices)
        weight = dag.dependant_counts()
        order = {s.id: i for i, s in enumerate(slices)}
        runnable = sorted(
            (s for s in dag.runnable() if s.id not in self._running),
            key=lambda s: (-weight[s.id], order[s.id]),
        )
        free = self.max_parallel - len(self._running)
        waiting: dict[str, str] = {}
        started: list[Slice] = []
        for sl in runnable:
            touches = sl.touches()
            holder = next((h for h, t in self._held.items() if overlaps(touches, t)), None)
            if holder is not None:
                waiting[sl.id] = f"touches overlap with {holder}"
            elif len(started) >= free:
                waiting[sl.id] = "no free slot"
            else:
                started.append(sl)
                self._held[sl.id] = touches
        for s in slices:
            if s.status == "ready" and s.id not in self._running and s.id not in waiting:
                reason = dag.waiting_reason(s.id)
                if reason and not any(x.id == s.id for x in started):
                    waiting[s.id] = reason
        self._log_waiting(waiting)
```

   Note: `_held` is added to as slices are picked, so two picks in the same pass can't overlap each other either.
10. If nothing was started and nothing is running: `self._note_idle(slices)` and return. If something started: `self._idle = None`.
11. For each started slice (in order): `source.set_status(IN_PROGRESS)`, emit `slice_started` with `weight=weight[sl.id]`, phase `working`, and `self._running[sl.id] = self._executor_pool().submit(self._run_one, sl)`. Log `starting <id> (blocks N)`.

`_executor_pool()` lazily creates `ThreadPoolExecutor(max_workers=self.max_parallel)`.

**`_settle(wait)`** (replaces the bottom half of the old `tick`):

```python
    def _settle(self, *, wait: bool) -> list[SliceResult]:
        """Ship finished work serially, in start order (the merge train)."""
        if wait and self._running:
            futures_wait(list(self._running.values()))
        done = [sid for sid, fut in self._running.items() if fut.done()]
        results = []
        for sid in done:
            work = self._running.pop(sid).result()
            self._held.pop(sid, None)
            results.append(self._integrate(work))
        self._log_results(results)  # the existing per-result log lines
        self._record_runtime(results)  # the existing runtime bookkeeping
        return results
```

(`futures_wait` is `concurrent.futures.wait`. Move the existing logging and runtime
bookkeeping loops from `tick` into `_log_results` / `_record_runtime` unchanged.)

**`_recover(slices)`**: runs once (`self._recovered`). Any slice in `in-progress` that this
daemon isn't running was orphaned by a crash or restart:

```python
        for sl in slices:
            if sl.status == IN_PROGRESS and sl.id not in self._running:
                self.source.set_status(sl.id, "ready")
                sl.status = "ready"
                entry = self.runtime.get(sl.id) if self.runtime is not None else {}
                if self.runtime is not None and entry.get("session_id") and self.worktrees.exists(sl.id):
                    self.runtime.record(sl.id, interrupted=True)
                log.warning("%s was in progress when the daemon stopped; requeued", sl.id)
                self._emit("requeued", sl.id)
```

If the worktree exists but there's no session to resume, tear it down
(`self.worktrees.teardown(sl.id)`) so `create` can start fresh.

**`_main_green()`**:

```python
    def _main_green(self) -> bool:
        """Run the check on the base branch once per new base commit (field note #15)."""
        cfg = self.repo_cfg
        try:
            head = _git(cfg.path, "rev-parse", cfg.base_branch)
        except RuntimeError as exc:
            log.warning("main gate: can't read %s (%s)", cfg.base_branch, exc)
            return True  # don't stall the queue on a git hiccup
        if head == self._green:
            return True
        if head == self._red:
            return False
        result = run_check(cfg.check, cfg.path)
        if result.passed:
            if self._red is not None:
                self._emit("main_green", commit=head)
                log.info("%s is green again", cfg.base_branch)
            self._green, self._red = head, None
            self._daemon_state(main="green")
            return True
        self._red = head
        tail = result.output[-500:].strip()
        log.warning("%s is red at %s; not starting slices until it's fixed", cfg.base_branch, head[:8])
        self._emit("main_red", commit=head, output=tail)
        self._daemon_state(main="red")
        return False
```

Import `_git` from `.pipeline` and `run_check` from `.check`. After a successful **local**
merge in `_ship`, set `self._green = result.commit` (that commit was checked before merging).

**`_reload_config()`**:

```python
    def _reload_config(self) -> None:
        if self.reload is None:
            return
        try:
            fresh = self.reload()
        except Exception as exc:
            log.warning("config reload failed, keeping the old one: %s", exc)
            return
        if fresh != self.repo_cfg:
            log.info("config reloaded")
            self.repo_cfg = fresh
            self.executor.model = fresh.model
            self.executor.effort = fresh.effort
```

(`max_parallel` and the pool size only change on restart.)

**`_log_waiting(waiting)`**: if `waiting != self._waiting`: log each `"<id>: <reason>"` at
INFO, and for each set state phase `waiting` with `detail=reason`; store it.

**`run_forever`** now polls and wakes early when a slice finishes:

```python
        try:
            while max_ticks is None or ticks < max_ticks:
                try:
                    self.poll()
                except Exception:
                    log.exception("poll failed; continuing")
                ticks += 1
                if self._mode == "stopping" and not self._running:
                    reason = "stop requested"
                    if self.control is not None:
                        self.control.set(stop=False)
                    break
                if max_ticks is not None and ticks >= max_ticks:
                    break
                self._idle_wait(interval, sleep)
        ...
        finally:
            ...
            self._emit(self.exit_event, reason=reason)
            if self._pool is not None:
                self._pool.shutdown(wait=True)
```

```python
    def _idle_wait(self, interval: float, sleep: Callable[[float], None]) -> None:
        """Sleep until the next poll, waking early when a running slice finishes."""
        if self._running:
            futures_wait(list(self._running.values()), timeout=interval, return_when=FIRST_COMPLETED)
        else:
            sleep(interval)
```

**Existing tests to update** (report each): `test_run_forever_ticks_n_times_then_stops`
and the Phase 1 `run_forever` tests patch `d.tick`; change them to patch `d.poll`. Tests
that call `tick()` keep working: a tick still starts everything runnable (up to
`max_parallel`, non-overlapping) and ships it before returning.

**Files:**
- Modify: `src/nightshift/daemon.py`, `src/nightshift/config.py`, `src/nightshift/events.py`
- Test: `tests/test_pool.py` (create), plus updates noted above

- [ ] **Step 1: Write the failing tests** (`tests/test_pool.py`)

Reuse the fixtures pattern from `tests/test_daemon_limits.py` (a `repo` fixture with a git
repo on `main`, `_write_slice`, `_ok`, a `Clock`, a `_daemon(...)` builder that passes
`EventLog`/`StateStore`/`Runtime` under `tmp_path / "home"`). Write `_write_slice` so it can
add a `## Touches` block. Tests:

```python
def test_critical_path_starts_first(repo, tmp_path):
    # leaf-1, leaf-2 independent; root has two dependants. max_parallel=1.
    _write_slice(repo, "slice-001")                      # leaf
    _write_slice(repo, "slice-002")                      # root
    _write_slice(repo, "slice-003", deps="[slice-002]")
    _write_slice(repo, "slice-004", deps="[slice-003]")
    d = _daemon(repo, tmp_path, Agent(), max_parallel=1)
    results = d.tick()
    assert [r.slice_id for r in results] == ["slice-002"]


def test_poll_starts_new_work_without_waiting_for_the_batch(repo, tmp_path):
    # A gate agent: slice-001 blocks until released; slice-002 is filed later and must
    # start on the next poll while slice-001 is still running.
    ...  # use threading.Event in the fake agent; d.poll(); write slice-002; d.poll();
         # assert both ids in d._running; release; d.tick() drains; both done.


def test_overlapping_touches_wait_and_say_why(repo, tmp_path, caplog):
    _write_slice(repo, "slice-001", touches=["src/kit/"])
    _write_slice(repo, "slice-002", touches=["src/kit/Card.tsx"])
    d = _daemon(repo, tmp_path, Agent())
    with caplog.at_level(logging.INFO, logger="nightshift"):
        results = d.tick()
    assert [r.slice_id for r in results] == ["slice-001"]
    assert "slice-002: touches overlap with slice-001" in caplog.text
    snap = StateStore(tmp_path / "home" / "state.json").snapshot()
    assert snap["slices"]["slice-002"] == {"phase": "waiting", "since": ..., "detail": "touches overlap with slice-001"}
    # (compare phase and detail only)


def test_waiting_on_deps_is_logged(repo, tmp_path, caplog): ...
    # slice-002 blocked, slice-003 depends on it: log has "slice-003: waiting on slice-002 (blocked)"


def test_recover_requeues_orphaned_in_progress(repo, tmp_path):
    # slice-001 status in-progress with a runtime session + existing worktree -> it is
    # set ready, runtime interrupted=True, then resumed with CONTINUE_PROMPT on the tick.
    # slice-002 in-progress with a worktree but no session -> worktree torn down, runs fresh.


def test_main_gate_red_blocks_then_green_resumes(repo, tmp_path):
    # repo_cfg.main_gate=True, check = "python -c \"import pathlib,sys; sys.exit(pathlib.Path('RED').exists())\""
    # commit a RED file on main -> tick() returns [], events has main_red once (second tick: no new event)
    # remove RED, commit -> tick runs the slice; events has main_green.
    # state daemon main == "red" then "green".


def test_main_gate_survives_git_error(repo, tmp_path):
    # base_branch="nope" -> rev-parse fails -> gate returns True -> slice still runs?
    # (rev-parse of a missing branch fails; worktree create would also fail -> the
    # slice is blocked with an error, which is fine: assert the gate didn't stall: a result exists.)


def test_pause_and_stop_controls(repo, tmp_path):
    # control.set(paused=True) -> tick() starts nothing; state daemon status "paused".
    # unpause -> runs. stop=True + run_forever(max_ticks=5) exits after 1 poll with
    # daemon_exit reason "stop requested" and control.stop reset to False.


def test_reload_applies_new_config(repo, tmp_path, caplog):
    # reload returns a RepoConfig with model="sonnet"; after tick executor.model == "sonnet"
    # reload raising -> warning logged, config kept.


def test_run_forever_wakes_when_a_slice_finishes(repo, tmp_path):
    # interval=3600 and a sleep that raises if called while work is running:
    # run_forever(max_ticks=2) must finish quickly because _idle_wait waits on the future.


def test_child_exit_event_name(repo, tmp_path):
    # exit_event="child_exit" -> the last event kind is child_exit, not daemon_exit.
```

Write each `...` test fully. Every new branch in `daemon.py` must be covered.

- [ ] **Step 2: Run to verify failure** — `AttributeError: 'Daemon' object has no attribute 'poll'` (and others).

- [ ] **Step 3: Implement** as specified above.

- [ ] **Step 4: Run all tests** — all pass; update the `run_forever` tests as noted.

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add -A src tests
git commit -m "Daemon: worker pool, critical path first, crash recovery, main gate, controls, config reload"
```

---

### Task 5: supervisor and `python -m nightshift`

`nsh daemon` (without `--once`) runs a supervisor: it starts the real loop as a child
process (`python -m nightshift daemon --child ...same options`) and restarts it when it
exits non-zero, with backoff, up to 5 restarts per hour. A clean exit (code 0, e.g. after
`nsh stop`) ends the supervisor. The child emits `child_exit`; only the supervisor emits
the final `daemon_exit`, so `nsh wait` doesn't wake on every restart. Each restart
emits `daemon_restarted` (history, not attention).

**Files:**
- Create: `src/nightshift/supervisor.py`
- Create: `src/nightshift/__main__.py`
- Modify: `src/nightshift/cli.py` (`daemon` gets hidden `--child`; supervises by default)
- Modify: `src/nightshift/daemon.py` (`run_daemon_cli(..., child=False)` passes `exit_event="child_exit"` and `reload=lambda: Config.load(config_path).repo(str(repo))` and `control=Control(home / "control.json")`)
- Test: `tests/test_supervisor.py`

- [ ] **Step 1: Write the failing tests**

```python
"""The supervisor restarts a crashed daemon child."""

from nightshift.events import EventLog
from nightshift.supervisor import supervise


class Runs:
    def __init__(self, *codes):
        self.codes = list(codes)
        self.calls = 0

    def __call__(self, argv) -> int:
        self.calls += 1
        return self.codes.pop(0)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _kinds(path):
    return [e["kind"] for e in EventLog(path).read_from(0)[0]]


def test_clean_exit_ends_supervisor(tmp_path) -> None:
    log = EventLog(tmp_path / "e.jsonl")
    assert supervise(["x"], run=Runs(0), sleep=lambda s: None, clock=Clock(), events=log) == 0
    assert _kinds(tmp_path / "e.jsonl") == ["daemon_exit"]


def test_crash_restarts_with_backoff(tmp_path) -> None:
    log = EventLog(tmp_path / "e.jsonl")
    sleeps = []
    runs = Runs(1, 3, 0)
    assert supervise(["x"], run=runs, sleep=sleeps.append, clock=Clock(), events=log) == 0
    assert runs.calls == 3
    assert sleeps == [5, 10]
    assert _kinds(tmp_path / "e.jsonl") == ["daemon_restarted", "daemon_restarted", "daemon_exit"]


def test_gives_up_after_too_many_crashes_in_an_hour(tmp_path) -> None:
    log = EventLog(tmp_path / "e.jsonl")
    clock = Clock()
    runs = Runs(*([1] * 10))
    code = supervise(["x"], run=runs, sleep=lambda s: None, clock=clock, events=log, max_restarts=3)
    assert code == 1
    assert runs.calls == 4
    events, _ = log.read_from(0)
    assert events[-1]["kind"] == "daemon_exit"
    assert "gave up" in events[-1]["reason"]


def test_old_crashes_fall_out_of_the_window(tmp_path) -> None:
    clock = Clock()
    runs = Runs(1, 1, 1, 0)

    def sleep(s):
        clock.t += 4000  # each wait pushes past the 1-hour window

    assert supervise(["x"], run=runs, sleep=sleep, clock=clock, events=EventLog(tmp_path / "e"),
                     max_restarts=1) == 0


def test_child_argv_round_trips_options() -> None:
    from nightshift.supervisor import child_argv

    argv = child_argv(repo="r", config=None, interval=60.0, log_file="d.log")
    assert argv[1:4] == ["-m", "nightshift", "daemon"]
    assert argv[4:] == ["--child", "--repo", "r", "--interval", "60.0", "--log-file", "d.log"]
    argv = child_argv(repo="r", config="c.yaml", interval=30.0, log_file=None)
    assert "--config" in argv and "--log-file" not in argv
```

Also add to `tests/test_cli.py`: `nsh daemon` without `--once` calls
`nightshift.supervisor.run_supervised` (monkeypatched) and exits with its code;
`nsh daemon --child` calls `run_daemon_cli(..., child=True)` (monkeypatched).
And a test that `python -m nightshift version` works via `runpy.run_module("nightshift", run_name="__main__")`
with `sys.argv` patched (catch `SystemExit`).

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError: nightshift.supervisor`.

- [ ] **Step 3: Implement**

`src/nightshift/supervisor.py`:

```python
"""Keep the daemon alive (field note #27: it died overnight and nobody knew).

``nsh daemon`` runs this supervisor. It starts the real loop as a child process and
restarts it when it exits with an error, backing off 5s, 10s, 20s ... up to 60s. More
than ``max_restarts`` crashes within an hour means something is persistently wrong:
it gives up and emits ``daemon_exit`` so the master agent hears about it.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable

from .events import EventLog

WINDOW = 3600.0


def child_argv(*, repo, config, interval: float, log_file) -> list[str]:
    argv = [sys.executable, "-m", "nightshift", "daemon", "--child", "--repo", str(repo)]
    if config is not None:
        argv += ["--config", str(config)]
    argv += ["--interval", str(interval)]
    if log_file is not None:
        argv += ["--log-file", str(log_file)]
    return argv


def _run_child(argv: list[str]) -> int:  # pragma: no cover - real subprocess
    return subprocess.call(argv)


def supervise(
    argv: list[str],
    *,
    events: EventLog,
    run: Callable[[list[str]], int] = _run_child,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    max_restarts: int = 5,
) -> int:
    crashes: list[float] = []
    while True:
        code = run(argv)
        if code == 0:
            events.emit("daemon_exit", reason="stopped")
            return 0
        now = clock()
        crashes = [t for t in crashes if now - t < WINDOW] + [now]
        if len(crashes) > max_restarts:
            reason = f"supervisor gave up: {len(crashes)} crashes within an hour (last exit code {code})"
            events.emit("daemon_exit", reason=reason)
            return code
        delay = min(60, 5 * 2 ** (len(crashes) - 1))
        events.emit("daemon_restarted", code=code, delay=delay)
        sleep(delay)
```

Note `test_crash_restarts_with_backoff` expects delays `[5, 10]` with 2 crashes in the
window. `test_old_crashes_fall_out_of_the_window` keeps the list at length 1 each time.

```python
def run_supervised(repo, *, config_path=None, interval: float = 30, log_file=None,
                   home_root=None) -> int:  # pragma: no cover - wires the real child
    from .config import Config
    from .paths import repo_home

    name = Config.load(config_path).repo(str(repo)).name
    events = EventLog(repo_home(name, home_root) / "events.jsonl")
    return supervise(child_argv(repo=repo, config=config_path, interval=interval,
                                log_file=log_file), events=events)
```

`src/nightshift/__main__.py`:

```python
"""`python -m nightshift` (the supervisor starts the daemon child this way)."""

from nightshift.cli import app

app()
```

In `cli.py`, the `daemon` command gains
`child: bool = typer.Option(False, "--child", hidden=True)`. Behaviour:
- `--once`: as today (`run_daemon_cli(..., once=True)`).
- `--child`: `configure_logging(log_file)` then `run_daemon_cli(repo, config_path=config, interval=interval, child=True)`.
- otherwise: `raise typer.Exit(code=supervisor.run_supervised(repo, config_path=config, interval=interval, log_file=log_file))`.

In `run_daemon_cli`, add `child: bool = False`; pass `exit_event="child_exit" if child else "daemon_exit"`,
`control=Control(home / "control.json")`, and
`reload=lambda: Config.load(config_path).repo(str(repo))`.

- [ ] **Step 4: Run all tests** — all pass.

- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add -A src tests
git commit -m "Supervisor: nsh daemon restarts a crashed loop and reports when it gives up"
```

---

### Task 6: status rendering, docs, live smoke

- [ ] **Step 1:** `render_status` (`state.py`): show `daemon: paused (nsh unpause to resume)`
  for `paused`, `daemon: stopping (after running slices)` for `stopping`, and append
  ` · main RED` to the daemon line when `daemon.main == "red"`. Tests in `tests/test_state.py`.
- [ ] **Step 2:** `skills/nightshift-babysit/SKILL.md` "Keep watching": add `main_red`
  (with `output`): `main` is failing its check, so no slice will start; fix `main` first.
  Add one line: use `nsh pause` / `nsh unpause` / `nsh stop` instead of killing the process.
- [ ] **Step 3:** README command list: add `nsh pause`, `nsh unpause`, `nsh stop`; note
  that `nsh daemon` restarts itself if it crashes.
- [ ] **Step 4: Live smoke (no Claude needed).** In a temp dir: `git init` a repo with one
  commit, register it with `nsh init --repo <tmp> --check "echo ok" --config <tmp>/c.yaml`,
  run `.venv/Scripts/nsh.exe daemon --repo <tmp> --config <tmp>/c.yaml --interval 2` in the
  background, then `nsh stop --repo <tmp> --config <tmp>/c.yaml`; within ~5 s the
  daemon must exit with code 0 and `nsh status` must show `daemon: stopped`. Report the output.
- [ ] **Step 5: Gate and commit**

```bash
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate
git add -A src tests skills README.md
git commit -m "Status shows pause/stop/main-red; docs for the new controls"
```
