# Nightshift v2: design

Status: agreed in a grill session on 2026-10-11, not built yet.
Input: `docs/field-notes/2026-10-08-motioncraft.md` (30 findings from building
motioncraft). Note numbers below (#N) refer to that file.

## Goals

1. Code itself properly from issues we create.
2. Be token efficient.
3. Survive the usage limit: pause until it resets, then continue where it stopped.
4. Work, review and recover without a human babysitting the process itself.
5. Work cleanly with babysit: a master agent driving many issues sees clean,
   exact status at all times.

## Principles

- **Deterministic tools first.** The agent does judgment work only: writing code,
  reviewing code, making in-slice decisions. Everything mechanical (labels, issue
  status, git, merging, conflict handling, rate limits, status reporting) is a
  fixed `nsh` command or daemon step with validated inputs. Agents never edit
  labels, frontmatter or git state by hand.
- **Don't over-engineer.** Each rule below exists because a field note showed the
  failure. No speculative machinery.
- **GitHub only for now.** `local-md` is dropped from scope; babysit, the `review`
  label and PR flows are GitHub-shaped.

## Root causes being fixed

| Cause | Notes |
|---|---|
| Usage limit not detected; it burns the queue as fake git errors | #29 |
| Token waste: thin prompt, retries without check output, reviewer rediscovers the diff, Opus everywhere, retrying against a red `main` | #15, #30 |
| Parallel slices can't see each other; names and components built twice | #6, #23, #25, #26 |
| Batch scheduler, no critical path, silent stalls, hint parsing bugs | #1-4, #21, #27, #28 |
| Fragile babysit handoff: no signal, conflicts land on the reviewer, hand-made scripts | #5, #8-10, #22 |
| Environment: shared `node_modules` junction, locked folders, no pause/status | #11-14, #17-19 |

## The pipeline, step by step

### Step 0: the daemon (always on)

- `nsh daemon` runs under a small supervisor that restarts it if it dies and emits
  a `daemon_died` event.
- **Worker pool, not batches.** A slot that frees up takes the best ready slice
  immediately. "Best" = most transitive dependants (critical path first).
- **Budget gate (reactive).** When any agent run returns a usage-limit error, the
  daemon enters `PAUSED_LIMIT until <reset>` (reset time parsed from the error; if
  absent, probe cheaply every 15 minutes). The interrupted slice keeps its worktree
  and session and is **not** charged an attempt. On reset it resumes that session.
  An agent run that produced no changes is its own failure type carrying the
  agent's last message, never a git error.
- **Main-green gate.** Before running slices against a new `main` commit, run the
  check once on clean `main`. If red: stop, emit `main_red`, don't burn attempts.
- Config reloads every tick. The daemon logs why each ready slice isn't runnable
  whenever that set changes, and logs on every exit.
- Writes all state to `~/.nightshift/<repo>/state.json` and every event to
  `~/.nightshift/<repo>/events.jsonl`.

### Step 1: pick up the issue

- Issue linter (deterministic): Goal and Acceptance present. Missing Size defaults
  to **M**. Failing issues get `nightshift:needs-detail` and an event; they don't run.
- Dependencies (`Depends-on: #N`) must be merged.
- **Touches lock:** never run two slices whose `## Touches` overlap (path-prefix
  comparison, read only from list items).
- Create the worktree and install dependencies **per worktree** (`npm ci` /
  `pnpm install --frozen-lockfile` / `uv sync`, detected by `nsh init`). The
  `node_modules` junction stays only as an opt-in for slow-install repos.

### Step 2: build (one agent session)

The workflow depends on the issue's Size:

| Size | Build agent | Plan file | Model |
|---|---|---|---|
| S | Does the task directly. No spec, no subagents. | none | Sonnet |
| M | Writes `.nightshift/plan.md` (superpowers writing-plans), then implements it in the same session. May use subagents. | short | Opus, medium effort |
| L | Subagent-driven development. Sub-tasks come from the issue's `## Subtasks`, not invented. | detailed, mostly from the issue | Opus, medium effort |

Size also sets attempt caps and timeouts. Models are per-repo config with these
defaults.

The prompt tells the agent:
- Which **existing** files it may modify (from Touches). It may create any new file.
- Decision rule: choices that stay inside the slice are decided and recorded;
  choices that touch the Contract (shared names, types, APIs other slices use)
  mean stop and ask.
- Never touch git or the issue. Finish by writing `.nightshift/result.json`.

`result.json` (schema-validated; one repair round in the same session if invalid):

```json
{
  "outcome": "done | question | needs_files",
  "decisions": ["..."],
  "question": "...",
  "needs_files": ["..."],
  "notes": "..."
}
```

- `question` posts to the issue, labels it `needs-detail`, emits an event.
  `nsh answer` re-queues the slice into the same session.
- `needs_files` adds the files to Touches; the slice re-runs once they're free.

### Step 3: deterministic gates (no LLM)

- The check passes. On red, the same session is resumed with the **trimmed
  failing output**, not the same prompt. Capped by size.
- No conflict markers; text files are UTF-8.
- **Strict file list:** the diff modifies no existing file outside Touches.
  Violation means the slice doesn't ship; the files are recorded as `needs_files`.

### Step 4: AI review

- Fresh Sonnet session, handed the diff, the issue and `plan.md` (no rediscovery).
- Blocking findings must cite file and line. They go back to the build session as
  one fix round, then the check runs again. Still failing means `blocked` with the
  reason.

### Step 5: ship

1. Rebase onto `main` using the conflict ladder (below).
2. Open the PR (body: summary, recorded decisions, review findings).
3. **No `review` label:** merge automatically.
   **`review` label:** leave the PR open and emit `review_ready`.
   The label is read at ship time, so babysit can add or remove it until then.
4. While a PR is open, Nightshift **rebases it itself** every time `main` moves.

### Step 6: babysit (the master agent)

- Runs `nsh wait` as a background task. It blocks until an event needs the master
  agent, prints one JSON line and exits: `review_ready`, `blocked`, `needs_detail`,
  `question`, `paused_limit`, `resumed`, `main_red`, `daemon_died`, `all_done`.
  It remembers its read position in `events.jsonl`, so nothing is missed.
  With wmux present, the same events go to `wmux report-agent`.
- Uses `nsh status` for the full picture and the `nsh pr` helpers to review,
  fix and merge. Labels which issues need `review` when it files them.

### Step 7: settle

Close the issue, kill the worktree's process tree and remove the worktree, record
tokens spent, start the dependants immediately.

## Avoiding conflicts

1. **Prevent.** The Touches lock in the scheduler, the strict file list on the
   diff, and `npm ci` as sync so `main`'s checkout is never dirtied.
2. **Git, no LLM.**
   - Registry/barrel files marked in config get `merge=union` in
     `.gitattributes` (line-level union of append-only lines).
   - Lockfile conflict: take `main`'s lockfile and regenerate it in the worktree.
   - Nightshift keeps its own open PRs rebased (GitHub's merge button ignores
     merge drivers, so this happens locally).
3. **LLM last.** Sonnet resolver, escalating to Opus only if its result fails the
   check. Every conflict is logged per file; `nsh status` shows the counts so a
   repeat offender becomes a registry or gets locked.

## The issue template

`nsh issue new` produces it; the slicer skill emits it.

```markdown
## Size
S | M | L

## Goal
What and why, linked to the design doc section.

## Decisions
Names, APIs, file paths and values already chosen. No guessing.

## Contract
Owns: what this slice creates.
Uses: what it must import and never redefine.

## Out of scope

## Acceptance
- Each item has a verification command or an observable result.

## Touches
- path/one
- path/two

## Subtasks
(L only) One block per sub-task: files, names, verification.

Depends-on: #N
```

Labels: `nightshift:ready`, `review` (optional), `visual` (optional, a hint for
babysit). Shared instructions (test-first, registries, render checks) live in
the target repo's `AGENTS.md`, not pasted into every issue (#28).

The slicer skill gains the rules from #20 and #26: slice the shared contract
first and make implementers depend on it; ask "does B need A's code or only
its concept?" before adding a dependency.

## The deterministic toolkit

| Command | Purpose |
|---|---|
| `nsh status [--json]` | Every slice's phase and age, waiting PRs, blocked reasons, budget/reset time, per-file conflict counts |
| `nsh wait` | Blocks until the next event for the master agent |
| `nsh issue new --size M --touches ... [--review]` | Create an issue from the template, linted before filing |
| `nsh issue set <id> ready / review / needs-detail` | The only way labels change |
| `nsh answer <id> "..."` | Answer an agent's question; re-queues the slice |
| `nsh pr checkout <n>` | Worktree with dependencies installed |
| `nsh pr merge <n>` | Wait for mergeability, squash, delete branch, clean worktree |
| `nsh pr comment <n> --verified "..." [--screenshot f]` | Record what babysit verified |
| `nsh pause` / `nsh resume` / `nsh stop --after-current` | Daemon control |

## Where the LLM runs

Build (Step 2), review (Step 4), and the last-resort conflict resolver. Nothing else.

## Out of scope for now

- `local-md` source parity.
- Proactive token budgeting (revisit once token logging gives real numbers).
- Generated registries/codegen (union merge first; revisit if it isn't enough).
