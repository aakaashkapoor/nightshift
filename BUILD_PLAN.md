# Nightshift — Build Plan

The **ordered, task-by-task** checklist for building Nightshift. Each task is
small and self-contained: paste it to Claude, get it done, tick the box, move on.
This is how we bootstrap until Nightshift can build itself.

**How to use:** work top-to-bottom. Don't start a task until the ones above it are
done (later tasks assume earlier ones exist). Each task lists a **Goal**, a
**seed prompt** you can hand to Claude, and **Done when** (acceptance).

Design reference: **[SPEC.md](./SPEC.md)**. Phases map to SPEC §17.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## Phase 0 — Repo bootstrap

- [x] **0.1 Scaffold the folder** — SPEC.md, README.md, LICENSE (MIT), .gitignore.
- [x] **0.2 Initialize git + first commit** — done: one commit on `main`.
- [x] **0.3 Create the public GitHub repo + push** — done:
  [github.com/aakaashkapoor/nightshift](https://github.com/aakaashkapoor/nightshift) (public), `main` pushed.

---

## Phase 1 — Prove the loop (v0.1)

Goal of the phase: **one slice, one repo, run by hand, produces one clean commit.**
No parallelism, no daemon, no GitHub, no review yet. Just prove Work→check→Ship
works with a local headless Claude.

- [x] **1.1 Project skeleton** — done: src-layout `nightshift` package, Typer CLI
  with `nsh`/`nightshift` entry points + `version` command, one passing pytest
  (TDD), Python 3.11+ (`hatchling` build). Editable-installed in `.venv`.
  Verify: `nsh version` → `nightshift 0.0.1`; `pytest` → 1 passed.

- [x] **1.2 Slice parser** — done: `nightshift/slice.py` `Slice` dataclass +
  `parse`/`to_markdown`/`load`/`save`. Format-preserving write-back via
  `ruamel.yaml` (flips `status`/`attempts` without reformatting the file). 5 tests
  (fields, defaults, status round-trip preserves body, file load/save, rejects
  missing frontmatter). Built test-first.

- [x] **1.3 Config loader** — done: `nightshift/config.py` `Config` +
  `RepoConfig`. Loads central `~/.nightshift/config.yaml` (SPEC §8), resolves a
  repo by **name or filesystem path**, merges global defaults + per-repo settings
  (check, source, base_branch, pr, max_parallel…), `${VAR}` env interpolation,
  `UnknownRepoError`/missing-check/missing-file errors. 9 tests, test-first.

- [x] **1.4 Worktree manager** — done: `nightshift/worktree.py` `WorktreeManager`
  + `Worktree`. `create` (worktree on branch `nightshift/<id>`), `teardown`
  (remove worktree + delete branch), `preserve` (leave for a blocked slice),
  `list_slices`, `exists`. 6 tests against a real throwaway repo (create/isolate/
  teardown/preserve/list/branch-from-tip) — pass on Windows. Test-first.

- [x] **1.5 Check runner** — done: `nightshift/check.py` `run_check` +
  `CheckResult`. Runs the per-repo check through the shell in a given cwd,
  captures combined stdout+stderr, pass/fail via returncode, timeout →
  `timed_out`. 4 tests (pass+output, fail+stderr+returncode, runs-in-cwd,
  timeout) — cross-platform. Test-first.

- [x] **1.6 Headless Claude executor** *(the core)* — logic done & tested;
  live smoke deferred to 1.8. `nightshift/executor.py` `Executor` + `ExecResult`.
  Invocation confirmed for CC 2.1.220: `claude -p --output-format json
  --permission-mode bypassPermissions [--model X] [--resume <session_id>]`,
  **prompt via stdin** (no Windows quoting), cwd = worktree. Parses JSON →
  `session_id` (for resume, SPEC §7) + result. Subprocess call behind an injectable
  `runner`; 7 unit tests (prompt guardrails, argv defaults/model/resume, cwd+stdin
  passing, JSON parse, failure, non-JSON). Default runner handles Windows `.cmd`
  shim. **Chose CLI subprocess over `claude-agent-sdk`** (testability + version
  robustness; SDK is a clean future swap). *Not yet run against a live Claude.*

- [x] **1.7 Work→Ship for a single slice (no rebase yet)** — done:
  `nightshift/pipeline.py` `run_slice` + `SliceResult`. Composes worktree →
  executor (Work) → check → ONE clean commit on green (marks slice `done`), retry/
  resume up to `max_attempts` on red, else `blocked` + preserve worktree. 3 e2e
  tests with real git + a fake agent (success=1 commit beyond main containing the
  agent's file; retry-then-succeed; block+preserve). Test-first.

- [x] **1.8 `nsh run <slice>` end-to-end demo** — DONE & PROVEN LIVE. 🎯
  `nsh run <slice> [--repo] [--config]` → `run_slice_cli`. **Live smoke passed:** a
  real headless Claude wrote `greet.py`, `python check.py` gated it, one clean
  commit `49bfdd9` (only greet.py, informative message) landed on
  `nightshift/slice-hello`. Walkthrough in [DEMO.md](./DEMO.md).

---

### ✅ v0.1 MILESTONE REACHED — the single-slice engine works with a real agent.
Next up: **Phase 2** (always-running daemon + parallelism + merge-train).

---

## Phase 2 — Autonomy & parallelism (v0.2)

- [x] **2.1 Slice source / discovery** — done: `nightshift/source.py`
  `LocalMdSource`. `list_all`/`list_ready` (scans `.slices/*.md`, skips non-slices),
  `get`, `path_for`, `save`, `set_status` (write-back). 4 tests. Test-first.

- [x] **2.2 Dependency DAG + scheduler** — done: `nightshift/scheduler.py` `Dag`.
  `build` validates deps exist + acyclic (Kahn's; `CycleError`), `runnable()`
  returns `ready` slices whose parents are all `done`, `parents`/`children`/
  `topo_order`. 8 tests. Test-first.

- [x] **2.3 Daemon loop skeleton (sequential)** — done: `nightshift/daemon.py`
  `Daemon` (`tick` = scan source → `Dag` → run each runnable via `run_slice` →
  persist status; `run_forever` polls) + `run_daemon_cli` + `nsh daemon
  [--once] [--interval]`. 5 tests (runs ready, only-runnable, blocked, run_forever
  N ticks, CLI once). Test-first. NOTE: cross-branch deps need the merge-train (2.5).

- [x] **2.4 Parallel worker pool + scope-overlap safety net** — done:
  `gitutil.py` (shared git lock so parallel threads are safe; wired into worktree.py
  + pipeline.py), `Slice.scope_hints()` (path tokens from the Scope-hints section),
  Daemon `_select_batch` (≤`max_parallel`, non-overlapping — colliders serialize) +
  `ThreadPoolExecutor` in `tick`. 7 new tests (parallel run, cap, overlap
  serialize, non-overlap together, scope-hint parsing). Test-first. 62 total.

- [ ] **2.5 Serial merge-train (Ship proper)**
  - **Goal:** integrate branches one at a time — rebase onto `main`, re-run check,
    merge; children start only after parents merge.
  - **Done when:** two slices land as two clean commits on `main`, no corruption.

- [ ] **2.6 Resolver agent (`nightshift-resolve`)**
  - **Goal:** on rebase conflict/red-check, spawn a resolver agent (both specs in
    context) that edits code to a clean, green merge.
  - **Done when:** an induced conflict is resolved by the agent and merges green.

- [ ] **2.7 Escalation cap + blocked/notify**
  - **Goal:** cap resolver attempts (N=2) → `blocked` + preserve worktree + eject
    from queue + write note into the issue + optional notifier hook.
  - **Done when:** an unresolvable slice blocks cleanly and the queue keeps moving.

- [ ] **2.8 Resumability + runtime.json**
  - **Goal:** `nsh resume <slice>` re-attaches to a preserved worktree and continues
    (resume the agent's session); `runtime.json` ephemera rebuilt on restart.
  - **Done when:** a blocked slice resumes and finishes without redoing prior work.

---

## Phase 3 — AI review + GitHub (v0.3)

- [ ] `nightshift-review` always-on Ship step (blocking vs advisory findings).
- [ ] `github-issues` source adapter (labels for state, `gh` auth).
- [ ] PR flow: open PR, auto-merge after AI review, PR closes the issue.
- [ ] The full visible GitHub story end-to-end.

---

## Phase 4 — Concierge + distribution (v0.4) → LAUNCH

- [ ] `nightshift-setup` concierge skill (re-runnable; auto-detect checks).
- [ ] Smoke-test dummy slice through the whole loop.
- [ ] PyPI package + `pipx install nightshift`.
- [ ] Example config, polished README with origin story, docs.
- [ ] 🚀 Launch: LinkedIn + wherever the stars are.

---

## Phase 5+ — Community extensibility

- [ ] External review (`external_review: true`, GitHub PR provider).
- [ ] Optional notifiers (Slack, Teams) + the optional permission gate wiring.
- [ ] Optional Jira tracker.
- [ ] Document `Source`/`Executor`/`Reviewer`/`Notifier`/`Tracker` interfaces for
      contributors.
- [ ] **Self-hosting:** Nightshift starts building Nightshift. 🌙

---

## Cross-cutting reminders (SPEC §0 invariants)

- No cloud — ever. Local headless Claude only.
- Exactly one clean commit per completed slice.
- Agents never block on a human.
- The issue is the source of truth for state.
- Works fully with local `.md` OR GitHub Issues.
- Never override a user's existing skills/setup (`nightshift-*` prefix).
- Cross-platform from day one (author is on Windows).
