# Nightshift v2 Phase 3: sized slices and the agent contract

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each issue runs the workflow its size calls for, on the right model; the agent reports back through a validated `result.json`; it can ask a question or ask for more files instead of guessing; and it can't silently edit files other slices own.

**Architecture:** Issue parsing (`Slice.section`, `Slice.size`) and a deterministic linter gate pickup. Prompts move to a `prompts` module with one variant per size. `run_slice` reads `.nightshift/result.json` after each agent turn. New outcomes (`question`, `needs_files`, `requeued`) flow through the daemon into the issue (labels, comments, sections) via new `Source` methods, and back via `nsh answer`. The reviewer receives the diff, the issue and the plan.

**Design:** `docs/superpowers/specs/2026-10-11-nightshift-v2-design.md` Steps 1–4. Field notes #6, #23, #25, #29, #30.

---

## Rules for every task

- Work in `C:\Users\aakaa\code\nightshift` on branch `v2-phase3`. Python: `.venv/Scripts/python` (bash). LF line endings; use `write_bytes` where byte offsets matter on Windows.
- Test-first: write the tests, run them and see them fail, implement, see them pass.
- Before each commit: `.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate` must print `CHECK PASSED` (ruff, mypy, pytest, **100% branch coverage**). No `# pragma: no cover` on new logic.
- Existing tests must keep passing. New behaviour that would change existing tests' agent-call counts is **opt-in** (see `result_contract`), so the old fakes keep working.
- If plan code has a bug, fix it minimally and report what and why. No extra features.
- Commit trailer:

```
Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UcNkjNqsVKMUAvGQ1Pates
```

- Do not push.

---

### Task 1: issue sections, size, linter, `needs-detail`

**Files:** modify `src/nightshift/slice.py`, `src/nightshift/github.py`, `src/nightshift/source.py`, `src/nightshift/events.py`, `src/nightshift/daemon.py`; create `src/nightshift/lint.py`; tests `tests/test_lint.py` (+ source tests).

`slice.py`:

```python
SIZES = ("S", "M", "L")

    def section(self, name: str) -> str | None:
        """Text under '## <name>' (case-insensitive) up to the next '## ' heading.

        None if the heading is absent; '' if present but empty.
        """
        lines = self.body.splitlines()
        target = f"## {name}".lower()
        for i, line in enumerate(lines):
            if line.strip().lower() == target:
                out = []
                for nxt in lines[i + 1 :]:
                    if nxt.strip().startswith("## "):
                        break
                    out.append(nxt)
                return "\n".join(out).strip()
        return None

    def size(self) -> str:
        """S, M or L from '## Size'; M when missing or unreadable."""
        raw = (self.section("Size") or "").split()
        token = raw[0].upper() if raw else ""
        return token if token in SIZES else "M"
```

`lint.py`:

```python
"""The issue linter (v2 design, Step 1): cheap, deterministic, no LLM."""

from __future__ import annotations

from .slice import SIZES, Slice


def lint(sl: Slice) -> list[str]:
    problems = []
    if not sl.section("Goal"):
        problems.append("missing '## Goal'")
    if not (sl.section("Acceptance") or sl.section("Acceptance criteria")):
        problems.append("missing '## Acceptance'")
    raw = (sl.section("Size") or "").split()
    if raw and raw[0].upper() not in SIZES:
        problems.append(f"'## Size' must be S, M or L (got {raw[0]!r})")
    if sl.size() == "L" and not sl.section("Subtasks"):
        problems.append("size L needs '## Subtasks'")
    return problems
```

Sources get `set_needs_detail(slice_id, message)`:
- GitHub: `LABELS["needs-detail"] = "nightshift:needs-detail"` (so `_status_of` maps it); `set_needs_detail` adds that label, removes the other nightshift labels (same pattern as `set_blocked`), and comments `❓ Needs detail:\n\n{message}`.
- LocalMd: status `needs-detail`; append `\n\n## Needs detail\n{message}\n` to the body if not already present.
- Add the method to the `Source` Protocol.

`events.py`: add `"needs_detail"` and `"question"` to `ATTENTION` (update the test).

Daemon `_start_ready`: right after computing `runnable` (before the touches loop), lint each runnable slice:

```python
        clean = []
        for sl in runnable:
            problems = lint(sl)
            if problems:
                text = "\n".join(f"- {p}" for p in problems)
                self.source.set_needs_detail(sl.id, text)
                self._emit("needs_detail", sl.id, problems=problems)
                self._phase(sl.id, "needs-detail", detail="; ".join(problems))
                log.warning("%s needs detail: %s", sl.id, "; ".join(problems))
            else:
                clean.append(sl)
        runnable = clean
```

Existing daemon tests write slices with only `## Goal`. Lint would flag them (no Acceptance). Make linting opt-in: `Daemon(..., lint_issues: bool = False)`; `run_daemon_cli` passes `True`. Test both.

Tests: `section` (present/absent/empty/stops at next heading, case-insensitive), `size` (S/M/L, lowercase, missing → M, junk → M), `lint` (each problem; clean issue; `Acceptance criteria` alias), both sources' `set_needs_detail` (GitHub via a fake `gh` recording calls, like `tests/test_github_source.py`), `_status_of` maps the new label, daemon with `lint_issues=True` (a bad issue is labelled and skipped, a good one runs, events contain `needs_detail`).

Commit: `Issues: size, sections and a linter that labels incomplete issues needs-detail`

---

### Task 2: models per role

**Files:** modify `src/nightshift/config.py`, `src/nightshift/executor.py`, `src/nightshift/resolver.py`, `src/nightshift/daemon.py`; tests in `tests/test_config.py`, `tests/test_executor.py`, `tests/test_coverage_gaps.py` or a new `tests/test_models.py`.

`config.py`:

```python
DEFAULT_ROLE_MODELS = {"S": "sonnet", "review": "sonnet", "resolve": "sonnet"}

# RepoConfig field:
    models: dict = field(default_factory=dict)  # role -> model; roles: S, M, L, review, resolve

    def model_for(self, role: str) -> str | None:
        """The model for a role: per-repo override, then the default, then `model`."""
        if role in self.models:
            return self.models[role]
        return DEFAULT_ROLE_MODELS.get(role, self.model)
```

`Config._resolve` reads `models=entry.get("models", self.defaults.get("models", {}))`.

`executor.py`:

```python
    def with_model(self, model: str | None) -> Executor:
        """A copy that runs a different model (same runner, effort, permissions)."""
        return Executor(
            runner=self.runner,
            permission_mode=self.permission_mode,
            model=model,
            claude_bin=self.claude_bin,
            effort=self.effort,
        )
```

`resolver.py`: `Resolver(executor, escalate: Executor | None = None)`. The first call
uses `executor`; every later call on the same instance uses `escalate` if given. (One
`Resolver` instance is shared across slices, so track calls per worktree path:
`self._calls: dict[str, int]`.)

Daemon `_run_one`: `executor = self.executor.with_model(self.repo_cfg.model_for(sl.size()))`
and pass it to `run_slice`. `run_daemon_cli`: `reviewer=AgentReviewer(ex.with_model(repo_cfg.model_for("review")))`,
`resolver=Resolver(ex.with_model(repo_cfg.model_for("resolve")), escalate=ex)`.
Log the role models at start.

Tests: `model_for` defaults, overrides, fallback to `model`; `with_model` keeps runner/effort; resolver escalates on the second call for the same path but not for a different path; the daemon passes `--model sonnet` for an S slice and the repo model for an M slice (a fake runner recording argv).

Commit: `Models per role: Sonnet for S slices, review and conflict resolution; escalate the resolver`

---

### Task 3: prompts per size, `result.json`, check output on retry, no-change detection

**Files:** create `src/nightshift/prompts.py`, `src/nightshift/result.py`; modify `src/nightshift/executor.py` (`build_prompt` delegates to `prompts.build_prompt`), `src/nightshift/pipeline.py`, `src/nightshift/worktree.py`; tests `tests/test_prompts.py`, `tests/test_result.py`, additions to `tests/test_pipeline.py`.

`prompts.py`:

```python
"""What the build agent is told (v2 design, Step 2). One variant per issue size."""

from __future__ import annotations

COMMON = """You are Nightshift, implementing ONE GitHub issue in this repository. This directory is a git worktree just for this issue.

Rules:
- Read AGENTS.md or CLAUDE.md first if the repo has one.
- Do NOT commit, push, create branches, or edit the issue. The harness does all git and status.
- Existing files you may modify: {files}. You may create new files anywhere. If you must modify another existing file, stop and report it in result.json as needs_files.
- If the issue doesn't settle a choice: decide it yourself when it only affects this issue's own files, and list it under decisions. If it affects the issue's Contract (names, types, APIs or files other issues use), stop and ask (outcome "question").
- Finish by writing .nightshift/result.json:
  {{"outcome": "done" | "question" | "needs_files", "decisions": ["..."], "question": "...", "needs_files": ["path"], "notes": "..."}}
"""

BY_SIZE = {
    "S": "This is a SMALL change. Implement it directly. No plan file, no subagents.",
    "M": (
        "This is a MEDIUM change. First write .nightshift/plan.md: the approach, the files "
        "you will touch, and a short task checklist (use the superpowers writing-plans "
        "skill if you have it). Then implement the plan in this session, test-first. You "
        "may use subagents for independent tasks."
    ),
    "L": (
        "This is a LARGE change. Write .nightshift/plan.md from the issue's '## Subtasks' "
        "(one section per subtask: files, names, how it is verified). Then execute it with "
        "subagent-driven development (superpowers:subagent-driven-development if you have "
        "it): one subagent per subtask, and review each one before starting the next."
    ),
}

CHECK_FAILED = """The repository's check failed after your change. Fix the cause; don't weaken or skip tests.

Command: {command}
Output (last part):
{output}

Update .nightshift/result.json when you're done."""

REPAIR = """.nightshift/result.json is missing or invalid: {error}
Write it now (valid JSON, the schema from your instructions). Do nothing else."""


def build_prompt(sl) -> str:
    touches = sorted(sl.touches())
    files = ", ".join(touches) if touches else "any (the issue lists no Touches)"
    return (
        COMMON.format(files=files)
        + "\n"
        + BY_SIZE[sl.size()]
        + f"\n\n# {sl.title}\n\n{sl.body.strip()}\n"
    )


def trim_output(output: str, max_lines: int = 80, max_chars: int = 6000) -> str:
    """The end of a check's output, where the failures are."""
    tail = "\n".join(output.strip().splitlines()[-max_lines:])
    return tail[-max_chars:]


def check_failed_prompt(command: str, output: str) -> str:
    return CHECK_FAILED.format(command=command, output=trim_output(output))


def repair_prompt(error: str) -> str:
    return REPAIR.format(error=error)
```

`Executor.build_prompt(sl)` returns `prompts.build_prompt(sl)`. Update the existing
executor test that checks the prompt mentions the task and "do not commit" (the new text
still says "Do NOT commit").

`result.py`:

```python
"""The build agent's report (.nightshift/result.json), validated (v2 design, Step 2)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

OUTCOMES = ("done", "question", "needs_files")
RESULT_PATH = Path(".nightshift") / "result.json"


@dataclass
class AgentResult:
    outcome: str
    decisions: list[str] = field(default_factory=list)
    question: str = ""
    needs_files: list[str] = field(default_factory=list)
    notes: str = ""


def read_result(worktree: Path | str) -> tuple[AgentResult | None, str]:
    """(result, "") or (None, why it's invalid)."""
    path = Path(worktree) / RESULT_PATH
    if not path.exists():
        return None, "file not found"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"not valid JSON ({exc})"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    outcome = data.get("outcome")
    if outcome not in OUTCOMES:
        return None, f"outcome must be one of {', '.join(OUTCOMES)}"
    decisions = data.get("decisions") or []
    needs = data.get("needs_files") or []
    if not (isinstance(decisions, list) and all(isinstance(d, str) for d in decisions)):
        return None, "decisions must be a list of strings"
    if not (isinstance(needs, list) and all(isinstance(n, str) for n in needs)):
        return None, "needs_files must be a list of strings"
    question = data.get("question") or ""
    if outcome == "question" and not (isinstance(question, str) and question.strip()):
        return None, "outcome question needs a question"
    if outcome == "needs_files" and not needs:
        return None, "outcome needs_files needs needs_files"
    return AgentResult(outcome, decisions, str(question), needs, str(data.get("notes") or "")), ""


def clear_result(worktree: Path | str) -> None:
    (Path(worktree) / RESULT_PATH).unlink(missing_ok=True)
```

`worktree.py` `create`: make sure `.nightshift/` is in the repo's shared exclude file so
plan and result files are never committed:

```python
        common = Path(self._git("rev-parse", "--git-common-dir").strip())
        if not common.is_absolute():
            common = self.repo / common
        exclude = common / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".nightshift/" not in current.splitlines():
            exclude.write_text(current.rstrip("\n") + ("\n" if current.strip() else "") + ".nightshift/\n", encoding="utf-8")
```

`SliceResult` gains `decisions: list[str] = field(default_factory=list)`,
`question: str = ""`, `needs_files: list[str] = field(default_factory=list)` (after
`session_id`; import `field`).

`run_slice` gains `result_contract: bool = False` and `resume_prompt: str | None = None`
(used instead of `CONTINUE_PROMPT` for the first call when resuming). New Work loop:

```python
    green = False
    attempts = 0
    last_session = resume_session if resuming else None
    prompt: str | None = (resume_prompt or CONTINUE_PROMPT) if resuming else None
    agent: AgentResult | None = None
    while attempts < max_attempts:
        attempts += 1
        clear_result(wt.path)
        if prompt is None:
            exec_result = executor.execute(wt.path, sl, resume_session=last_session)
        else:
            exec_result = executor.execute_prompt(wt.path, prompt, last_session)
        last_session = exec_result.session_id or last_session
        if exec_result.usage_limited:
            raise UsageLimitReached(last_session)
        if result_contract:
            agent, error = read_result(wt.path)
            if agent is None:
                fix = executor.execute_prompt(wt.path, repair_prompt(error), last_session)
                last_session = fix.session_id or last_session
                if fix.usage_limited:
                    raise UsageLimitReached(last_session)
                agent, error = read_result(wt.path)
            if agent is None:
                return _blocked(f"agent wrote no valid result.json: {error}")
            if agent.outcome == "question":
                worktrees.preserve(sl.id)
                return SliceResult(sl.id, "question", attempts, None, wt.branch,
                                   agent.question, last_session, question=agent.question,
                                   decisions=agent.decisions)
            if agent.outcome == "needs_files":
                worktrees.preserve(sl.id)
                return SliceResult(sl.id, "needs_files", attempts, None, wt.branch,
                                   "needs " + ", ".join(agent.needs_files), last_session,
                                   needs_files=agent.needs_files, decisions=agent.decisions)
        check = run_check(check_cmd, wt.path)
        if check.passed:
            green = True
            break
        prompt = check_failed_prompt(check_cmd, check.output)
```

(`_blocked` must be defined before the loop now; move it up. It references `attempts`
and `last_session` via closure, which still works.)

After the review gate, before committing, when **not** resuming:

```python
    if not resuming and not _has_changes(wt.path):
        note = (exec_result.result_text or "").strip()[:300]
        return _blocked(f"agent made no changes: {note}")
```

with `_has_changes(path)` = `git status --porcelain` is non-empty. (Field note #29: an
empty run used to surface as a git error.)

Commit message: `_commit_message(sl, decisions)` appends `"\n\nDecisions:\n- ..."` when
there are decisions. Return `decisions` on the done `SliceResult`.

Daemon: `Daemon(..., result_contract: bool = False)`, passed to `run_slice`;
`run_daemon_cli` passes `True`.

Tests (pipeline, with fake agents that write `.nightshift/result.json`): each prompt size
variant and the Touches list; `trim_output`; `read_result` for every error branch and a
good file; `.nightshift/` lands in `info/exclude` once (idempotent) and the commit doesn't
contain it; with `result_contract=True`: done → green; missing file → one repair call →
done; still missing → blocked with the error; question → `question` result with worktree
kept; needs_files → `needs_files` result; red check → the second call's prompt contains
the check output (use a check command that fails once via a marker file); no changes →
blocked "agent made no changes"; decisions land in the commit message. Without
`result_contract`, existing behaviour is unchanged except that a red check's retry now
sends the check output (update any test asserting the retry prompt).

Commit: `Build agent: prompts by size, result.json contract, check output on retry, no-change detection`

---

### Task 4: questions, extra files, and `nsh answer`

**Files:** modify `src/nightshift/source.py`, `src/nightshift/github.py`, `src/nightshift/daemon.py`, `src/nightshift/cli.py`, `src/nightshift/wait.py` (or a new `answer.py`); tests.

Sources get `append_to_section(slice_id, heading, line)`: insert `line` at the end of
the `## <heading>` section, creating the section at the end of the body if missing.
Shared pure helper in `slice.py`:

```python
def append_to_section(body: str, heading: str, line: str) -> str:
    lines = body.rstrip("\n").splitlines()
    target = f"## {heading}".lower()
    for i, existing in enumerate(lines):
        if existing.strip().lower() == target:
            end = i + 1
            while end < len(lines) and not lines[end].strip().startswith("## "):
                end += 1
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            lines.insert(end, line)
            return "\n".join(lines) + "\n"
    return "\n".join([*lines, "", f"## {heading}", line]) + "\n"
```

GitHub: read the body (`gh issue view N --json body`), rewrite with
`gh issue edit N --body <new>`. LocalMd: update `sl.body` and save.

Daemon `_integrate` handles the new statuses before `_ship`:
- `question`: `source.set_needs_detail(id, "Question from the agent:\n\n{q}\n\nAnswer with: nsh answer {id} \"...\"")`; runtime `record(id, session_id=..., branch=..., interrupted=False, awaiting_answer=True)`; emit `question` with `question`; phase `question` with detail. Return the result.
- `needs_files`: for each file `source.append_to_section(id, "Touches", f"- {f}")`; `source.set_status(id, "ready")`; runtime `record(id, session_id=..., interrupted=True, resume_prompt=FILES_PROMPT.format(files=...))`; emit `needs_files` with `files`; phase `queued` with detail. The scheduler now holds the new files, so it waits for any slice changing them.

`FILES_PROMPT` (in `prompts.py`): "You may now also modify: {files}. Continue the task from where you stopped, then update .nightshift/result.json."
`ANSWER_PROMPT`: "The owner answered your question:\n\n{answer}\n\nContinue the task from where you stopped, then update .nightshift/result.json."

`_record_runtime` must not overwrite these: skip `question` and `needs_files` results there (they were recorded in `_integrate`).

`_run_one` passes `resume_prompt=entry.get("resume_prompt")` alongside `resume_session`, and the runtime entry's `resume_prompt` is cleared when the slice finishes (`forget` on done already drops it; on blocked, record `resume_prompt=""` is not allowed since `record` drops None — add `Runtime.clear(slice_id, *keys)`).

`nsh answer <slice> "<text>"` (`run_answer_cli` in `wait.py`): `source.append_to_section(id, "Answers", f"- {text}")`; `source.set_status(id, "ready")`; runtime `record(id, interrupted=True, awaiting_answer=False, resume_prompt=ANSWER_PROMPT.format(answer=text))`; emit `answered`. Prints `answered <id>; it will resume on the next poll`. Errors clearly if the slice doesn't exist.

Tests: `append_to_section` (existing section, missing section, trailing blank lines, next heading), both sources, daemon question flow end to end (agent writes a question → issue labelled needs-detail with the question, event `question`, worktree kept → `run_answer_cli` → next tick resumes the same session with the answer prompt → done), needs_files flow (Touches gains the file, slice requeued with the files prompt, resumes), CLI command wiring.

Commit: `Agent questions and file requests flow through the issue; nsh answer resumes the slice`

---

### Task 5: strict file list at ship time

**Files:** modify `src/nightshift/daemon.py`; tests `tests/test_pool.py` or a new `tests/test_strict_files.py`.

In `_integrate`, for a `done` result whose slice has a non-empty `touches()` (re-read the
slice with `source.get`), before `_ship`:

```python
    def _outside_touches(self, work: SliceResult, touches: set[str]) -> list[str]:
        """Existing files the slice modified that its Touches doesn't cover."""
        out = _git(self.worktrees.path_for(work.slice_id), "diff", "--name-only",
                   "--diff-filter=MDR", f"{self.repo_cfg.base_branch}...HEAD")
        return [f for f in out.splitlines() if f and not overlaps({f}, touches)]
```

- None outside → ship.
- Outside files that no **other running** slice holds (`self._held`) → append each to the
  issue's Touches (`append_to_section`), log `"<id> also modified <files>; added to Touches"`, ship.
- Any outside file held by another running slice → don't ship: append them to Touches,
  `source.set_status(id, "ready")`, runtime `record(..., interrupted=True, resume_prompt=REQUEUE_PROMPT)`,
  emit `requeued` with `files` and `holder`, phase `queued`, return a `SliceResult` with
  status `REQUEUED = "requeued"` (skipped by `_record_runtime`). When it resumes, its
  committed work is reattached (`_commit_if_changes`), and Ship rebases onto the new main.
  `REQUEUE_PROMPT` (prompts.py): "Your change also modifies {files}, which another issue was changing at the same time. That issue has landed now. Check that your change still fits, adjust if needed, then update .nightshift/result.json."

Tests: no Touches → no check; inside → ships; outside but free → ships and Touches gains
the file; outside and held by a running slice (put an entry in `d._held` manually) →
requeued with the event and runtime entry.

Commit: `Strict file list: edits outside Touches are added, or wait for the slice that owns them`

---

### Task 6: the reviewer sees the diff, the issue and the plan

**Files:** modify `src/nightshift/review.py`, `src/nightshift/daemon.py` (`run_daemon_cli` passes `base_branch`); tests `tests/test_review.py`.

```python
REVIEW_PROMPT = """You are Nightshift's code reviewer. Below are the issue, the agent's plan (if any) and the full diff. Review the diff against the issue's goal, acceptance and contract. Look for correctness bugs, missed acceptance items, and duplicated or conflicting definitions.

Respond with ONLY a JSON object: {{"findings": [{{"severity": "blocking"|"advisory", "message": "..."}}]}}.
A blocking finding MUST name the location as path:line in its message; one without a location is treated as advisory. An empty list means the work is good.

# Issue: {title}

{body}

# Plan

{plan}

# Diff

{diff}
"""
```

`AgentReviewer(executor, base_branch: str = "main", max_diff: int = 80_000)`. `review(path, sl)`:
1. `git add -A -N` in the worktree (intent-to-add, so new files appear in the diff), then `git diff <merge-base>` where `<merge-base>` = `git merge-base <base_branch> HEAD` (working tree against where the branch started, so already-committed work from a resumed slice is included); truncate to `max_diff` with a `\n[diff truncated]` marker.
2. Plan: `.nightshift/plan.md` text or `(none)`.
3. Run the prompt; usage limit raises as today; parse findings.
4. Downgrade: a `blocking` finding whose message has no `path:line` (`re.search(r"[\w./\\-]+\.\w+:\d+", msg)`) becomes advisory.

Tests: prompt contains issue, plan, diff of both a modified and a new file; truncation;
the downgrade rule; existing parse tests still pass (update any that assert the old prompt).

Commit: `Reviewer: gets the diff, issue and plan; blocking findings must cite path:line`
