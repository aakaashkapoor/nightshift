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

- [ ] **1.1 Project skeleton**
  - **Goal:** a `nightshift` Python package with `pyproject.toml`, a CLI entry
    point (`nsh` / `nightshift`), and a `--version` command.
  - **Seed:** *"Create a Python package `nightshift` with pyproject.toml (Python
    3.11+), a Click/Typer CLI exposing `nsh` with a `version` command, and a
    `tests/` dir with one passing smoke test. Installable with `pipx install -e .`"*
  - **Done when:** `nsh version` prints a version; `pytest` passes.

- [ ] **1.2 Slice parser**
  - **Goal:** read/write the standard slice format (SPEC §3) — YAML frontmatter +
    markdown body — to/from a Python object.
  - **Seed:** *"Implement a `Slice` model + parser that reads a `.md` file with the
    SPEC §3 frontmatter (id, title, status, depends_on, attempts, jira) and body
    sections. Support writing status back into the file. Add a sample slice and
    round-trip tests."*
  - **Done when:** parse→modify status→write round-trips; tests pass.

- [ ] **1.3 Config loader**
  - **Goal:** load `~/.nightshift/config.yaml` (SPEC §8) with global defaults +
    per-repo entries; resolve the `check` command for a given repo path.
  - **Seed:** *"Implement config loading from ~/.nightshift/config.yaml with the
    SPEC §8 shape. Given a repo path, return its resolved settings (check,
    base_branch, source, pr). Support ${VAR} env interpolation. Sensible errors if
    a repo isn't registered."*
  - **Done when:** given a sample config + repo, returns the right `check` string.

- [ ] **1.4 Worktree manager**
  - **Goal:** create an isolated worktree + branch for a slice; tear it down;
    preserve it on failure.
  - **Seed:** *"Implement a worktree manager: `create(slice_id, base_branch)` makes
    a git worktree on a new branch `nightshift/<slice_id>`; `teardown` removes it;
    `preserve` leaves it for inspection. Cross-platform (Windows + POSIX). Tests
    against a throwaway repo."*
  - **Done when:** can create/list/teardown worktrees on a test repo, on Windows.

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
