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

- [x] **2.5 Serial merge-train (Ship proper)** — done: `pipeline.integrate_branch`
  (rebase onto base → re-check → ff-merge; `IntegrationResult`) + daemon `tick` now
  does parallel Work then **serial** integration; merged → `done` + teardown,
  conflict/red → `blocked` + preserve. 4 merge tests (lands on main, 2 commits,
  dependent-sees-parent across ticks, rebase-conflict blocks collider). 66 total.
  This closes the 2.3 cross-branch-dependency gap.

- [x] **2.6 Resolver agent (`nightshift-resolve`)** — done: `resolver.py`
  `Resolver` (runs headless Claude in the conflicted worktree with a resolve
  prompt), `Executor.execute_prompt`, `integrate_branch(resolver=...)` with a
  resolve loop (edit → detect leftover markers via `diff --cached --check` matching
  "conflict marker", ignoring whitespace/CRLF → `rebase --continue`), daemon +
  `run_daemon_cli` wire a real resolver. Test: induced conflict now resolves &
  merges (both slices done). 67 total.

- [x] **2.7 Escalation cap + blocked/notify** — done: resolver cap = `resolve_attempts`
  (N=2); `notifier.py` `Notifier` protocol + `NullNotifier` (Slack/Teams later);
  `source.set_blocked` writes a `## Blocked` reason into the issue; daemon `_block`
  = preserve worktree + note + notify; blocked slice is ejected (no longer `ready`
  so the queue keeps moving). 2 tests (note+notify, resolver-cap blocks). 69 total.

- [x] **2.8 Resumability + runtime.json** — done: `runtime.py` `Runtime`
  (record/get/forget/reconcile; rebuildable on restart), `WorktreeManager.attach`,
  `pipeline.resume_slice` (resumes agent session on the preserved worktree) +
  `run_resume_cli` (resume → integrate) + `nsh resume`, `SliceResult.session_id`
  threaded through, daemon records/forgets/reconciles runtime. 6 tests. 75 total.

---

### ✅ PHASE 2 COMPLETE (v0.2) — the full autonomous daemon works.
Parallel Work + serial merge-train + agent conflict-resolution + escalation/notify
+ resumable blocked slices. Next: **Phase 3** (AI review + GitHub).

---

## Phase 3 — AI review + GitHub (v0.3)

- [x] **3.1 AI review step (always-on)** — done: `review.py` `Reviewer`/`ReviewResult`/
  `Finding` + `AgentReviewer` (headless Claude → JSON findings) + `parse_findings`/
  `fix_prompt`. `run_slice` refactored: Work-until-green → review-until-clean
  (blocking → fix round + re-check; advisory logged) → commit. Daemon +
  `run_daemon_cli` wire a real `AgentReviewer`. 5 tests. 80 total.

- [x] **3.2 `github-issues` source adapter** — done: `github.py`
  `GitHubIssuesSource` (list_all/list_ready/get/set_status/set_blocked) over an
  injectable `gh` runner; status ↔ `nightshift:*` labels (done = closed),
  `Depends-on: #N` parsed to deps, unmanaged issues skipped. 5 tests (mapping,
  ready-filter, label edits, close-on-done, blocked+comment). 85 total. Live gh
  calls untested (need a real remote).

- [x] **3.3 PR flow** — done: `pr.py` `GitHubPR` (push + `gh pr create` +
  `gh pr merge --auto`), `build_pr_body` (`Closes #N` for github slices),
  `open_pr_for_slice` (auto-merge vs leave-open for human review). 6 tests with
  fake git+gh runners. 91 total. Live PR flow needs a real remote.

---

### ✅ PHASE 3 COMPLETE (v0.3, code) — AI review + GitHub adapters + PR flow.
Live gh/PR paths await a real remote. Next: **Phase 4** (concierge + distribution).

---

## Phase 4 — Concierge + distribution (v0.4) → LAUNCH

- [x] **4.1 Check auto-detection + `nsh init`** — `detect.py` `detect_check`
  (pyproject→pytest, package.json→npm test, Makefile→make test, …), `config.register_repo`,
  `nsh init` command. 5 tests.
- [x] **4.2 Concierge + slice skills** — `skills/nightshift-setup/SKILL.md`
  (re-runnable concierge) and `skills/nightshift-slice/SKILL.md` (slice designer).
- [x] **4.3 Example config + docs** — `examples/config.yaml`; README with commands,
  quickstart, install-from-source; DEMO.md linked.
- [ ] **4.4 Smoke-test command** — a `nsh smoke` that runs a throwaway slice
  end-to-end (folds the manual DEMO harness into one command). *(nice-to-have)*
- [ ] **4.5 PyPI publish** — ⚠️ **needs you**: a PyPI account + token, then
  `python -m build && twine upload`. Packaging metadata is ready in pyproject.toml.
- [ ] **4.6 🚀 Launch** — LinkedIn etc. (your call, when you're ready).

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
