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

- [ ] **1.5 Check runner**
  - **Goal:** run a repo's `check` command in a worktree, capture pass/fail +
    output.
  - **Seed:** *"Implement a check runner that executes the configured check command
    in a given worktree, returns (passed: bool, output: str), with a timeout.
    Cross-platform shell handling."*
  - **Done when:** returns True on a passing check, False + output on a failing one.

- [ ] **1.6 Headless Claude executor** *(the core)*
  - **Goal:** spawn a local headless Claude in a worktree, hand it the slice spec,
    let it implement, without stalling on prompts.
  - **Seed:** *"Implement an executor that launches headless Claude Code in a
    worktree with the slice's Goal/Acceptance as the task, permission-bypass on so
    it never blocks. Capture logs. Return when the agent finishes or errors.
    Confirm the exact non-interactive invocation (SDK vs `claude -p`) — ask the
    claude-code-guide agent if unsure."*
  - **Done when:** on a toy repo + toy slice, the agent actually edits files.

- [ ] **1.7 Work→Ship for a single slice (no rebase yet)**
  - **Goal:** wire it together: create worktree → run executor (Work) → run check →
    squash to ONE clean, informative commit (Ship, simplest form).
  - **Seed:** *"Compose 1.4–1.6 into `run_slice(slice)`: worktree → executor → check
    (loop back to executor up to a cap if red) → squash WIP to one clean commit
    with an outcome-focused message → mark slice done. No rebase/merge-train yet."*
  - **Done when:** one command turns a ready slice into one clean commit on its
    branch, green check.

- [ ] **1.8 `nsh run <slice>` end-to-end demo**
  - **Goal:** the tweetable v0.1 moment — `nsh run slice-001` on a real toy repo.
  - **Seed:** *"Wire `nsh run <slice_id>` to load config + slice and call
    run_slice. Write a short DEMO.md walking through it on a sample repo."*
  - **Done when:** you can watch a slice go from spec → clean commit by hand.
  - 🎯 **This is the v0.1 milestone. Record a GIF here — it's the proof.**

---

## Phase 2 — Autonomy & parallelism (v0.2)

Expand into granular tasks when we reach it. High-level:

- [ ] Always-running daemon (filesystem watch on `.slices/`, ~30s poll hook).
- [ ] DAG builder from `depends_on`; schedule roots; `max_parallel: 5`.
- [ ] Scope-hint overlap safety net (serialize colliding "independent" roots).
- [ ] Serial **merge train**: rebase onto `main`, re-check, merge, one at a time.
- [ ] `nightshift-resolve` conflict/resolver agent (edits code, both specs in ctx).
- [ ] Escalation cap (N=2) → blocked + preserve worktree + eject + note in issue.
- [ ] Resumability: `nsh resume <slice>` attaches to preserved worktree.
- [ ] `runtime.json` ephemera + restart reconciliation.

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
