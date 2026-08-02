# Nightshift — Design Spec

> **Working title: `Nightshift`** (CLI `nsh` / `nightshift`). Name may be swapped
> before launch (candidate fallbacks: *Nightshifter*, *Farrier*, *Slipway*) — a
> find-replace, not a design change. See [Naming](#naming).

**One-liner:** *You slice the work; your machine ships it overnight. Nightshift is
an always-running local daemon that picks up clean, one-commit-sized work items,
codes and tests each one with a local headless Claude in its own git worktree,
resolves its own merge conflicts, and lands a single clean commit — no hands.*

---

## 0. Core Invariants (non-negotiable)

These hold everywhere. If any part of the design contradicts one of these, the
invariant wins.

1. **NO CLOUD. EVER.** Every agent — coding, review, conflict-resolution — is
   **local headless Claude Code running on the user's own machine**, spawned by
   the local daemon. It uses whatever Claude access the user already has locally:
   a normal Claude subscription (Pro/Max), an API key, **or even free-tier**.
   There is no hosted service, no Anthropic sandbox, no server, nothing to
   "enable." *Clone, point it at your repos, it runs on your laptop.*
2. **Exactly one commit per completed item.** Interim/WIP commits are allowed
   *during* work as scratch, but a clean run **squashes to a single, informative,
   outcome-focused commit** at the end. No thousand-commit noise.
3. **Agents never block waiting on a human.** They either auto-decide small
   choices, or fail *gracefully into a resumable state* (see §7). Human
   notifiers (Slack/Teams/…) are an *optional convenience layer*, never required
   machinery.
4. **The issue is the source of truth for logical state.** Status lives in the
   work item itself (local `.md` frontmatter or GitHub issue state/labels), not
   hidden in a database.
5. **Runs cleanly with local `.md` files OR GitHub Issues** — both are
   first-class, authoritative sources. The whole pipeline works fully offline
   with zero GitHub dependency.
6. **Never override a user's existing setup.** All shipped skills are namespaced
   `nightshift-*`; config lives outside any repo; existing auth is reused.

---

## 1. Glossary

| Term | Meaning |
|---|---|
| **Slice** | One clean, one-commit-sized unit of work. A low-key, no-code natural-language spec. The atomic thing Nightshift executes. |
| **Daemon** | The always-running local Python process. The orchestrator. Watches for slices, schedules them, manages worktrees and the merge queue. |
| **Executor** | The thing that runs a coding agent. Default (and only, at launch): **local headless Claude Code subprocess**. Behind an interface so it *could* be swapped later. |
| **Check** | A per-repo configurable command that is the verification gate (unit tests + static analysis + whatever). E.g. `pytest && ruff check .`, `make test`, `npm test && tsc`. |
| **Work phase** | The agent implements a slice against its spec in an isolated worktree. |
| **Ship phase** | A loop: commit → rebase onto current `main` → resolve conflicts (agent) → re-run check → merge. Loops until green-and-merged or the escalation cap trips. |
| **Merge train** | The serial integration queue. Parallel Work, **serialized Ship** — only one branch integrates at a time. |
| **Resolver agent** | A fresh local headless Claude spawned to resolve a merge conflict or post-rebase check failure. A real agent that edits code, not a text-merge tool. |
| **Concierge** | The `nightshift-setup` Claude skill — a re-runnable conversational onboarding/reconfiguration agent. |
| **Adapter** | A pluggable implementation behind a small interface (Source, Executor, Reviewer, Notifier, Tracker). Config names which to load. |

---

## 2. The Big Picture

```
  grill-me / nightshift-slice  (interactive, with you)
            │  designs a batch of slices, asks "Happy with these?"
            ▼  on YES → writes slices as status: ready
  ┌──────────────────────────────────────────────┐
  │  SLICES  (local .md  OR  GitHub Issues)       │   ← source of truth for state
  └──────────────────────────────────────────────┘
            │  daemon watches (fs watch) / polls (GitHub, ~30s)
            ▼
  ┌──────────────────────────────────────────────┐
  │  NIGHTSHIFT DAEMON  (always running, local)   │
  │  • builds dependency DAG from depends_on      │
  │  • schedules ready roots (max_parallel: 5)    │
  │  • one worktree per slice                     │
  │  • serial merge train for integration         │
  └──────────────────────────────────────────────┘
       │ parallel Work (≤5)          │ serial Ship (1 at a time)
       ▼                             ▼
  [worktree A] Work → check    Ship: commit → rebase onto main
  [worktree B] Work → check          → conflict? resolver agent edits code
  [worktree C] Work → check          → re-run check → AI review → merge
       (local headless Claude)       → PR closes the issue (if PR enabled)
                                      → squash to ONE clean commit
```

- **Work is parallel** (up to `max_parallel`, default **5**), each in its own
  worktree/branch — this is what makes parallel coding safe.
- **Ship is serial** (single merge queue) — each branch rebases onto a
  *stationary* `main`, so conflicts are deterministic and resolvable one at a
  time.

---

## 3. The Slice (standard format)

One standard format regardless of source. Emitted by `nightshift-slice` (or the
user's own grill-me, as long as it emits this shape). A **low-key, no-code
natural-language spec** — intent + acceptance, no implementation code.

```markdown
---
id: slice-003
title: <one line>
status: ready            # ready | in-progress | done | blocked
depends_on: [slice-001]  # ids or #issue-numbers; empty = independent (a DAG root)
attempts: 0              # incremented by the daemon; meaningful to humans
jira: PROJ-142           # optional tracker link
---
## Goal
<what & why, in plain language>

## Acceptance criteria
- [ ] ...
- [ ] ...

## Scope hints
<files/dirs likely involved — advisory only, not binding, used for the
 parallel-safety overlap check>
```

- **`check` is NOT in the slice** — it's a per-repo setting (see §8). One repo,
  one check, applied to every slice in that repo.
- `status` transitions are **written back into the slice** by the daemon
  (`ready → in-progress → done | blocked`).

---

## 4. Source Adapters (`local-md` / `github-issues`)

Two interchangeable **primary, authoritative** sources. Pick one per repo. The
daemon reads *and writes* status to whichever is configured.

| | `local-md` (default / showcase) | `github-issues` |
|---|---|---|
| Storage | `.slices/*.md` in the repo | GitHub Issues |
| Ready signal | `status: ready` frontmatter | `ready` label |
| State writeback | rewrite frontmatter | flip issue state/labels |
| Detection | filesystem watch (instant) | poll GitHub API (~30s) |
| Requires network | No — fully offline | Yes (`gh` auth) |

- **Default & showcased = `local-md`** so anyone can clone and run fully offline.
- **The author's own setup = `github-issues`** (the visible GitHub story:
  issue → PR → closed → pushed).
- Neither is a second-class mirror. Same daemon, one config line different.

---

## 5. Dependencies & Scheduling

- **Explicit `depends_on` is the single source of truth** for the parallel/serial
  graph. `nightshift-slice` (or grill-me) declares dependencies **at slice time**,
  when it already holds the whole mental model. The daemon builds a **DAG**.
- **Roots (no deps) run in parallel**, up to `max_parallel: 5`.
- **A child never starts until its parent has *merged***, then it branches off the
  fresh `main` — so dependents rarely conflict by construction.
- **Cheap safety net:** before launching two "independent" roots in parallel, the
  daemon does a static **scope-hint overlap check**; if two roots collide on the
  same files, it **warns and serializes them anyway** (belt-and-suspenders against
  the worst conflict case).

---

## 6. Per-Slice Lifecycle: Work → Ship

Two phases. The second is a loop.

### Phase 1 — Work
- Agent (local headless Claude) implements the slice against its no-code spec in
  its **own worktree**.
- Leaves **breadcrumbs for cheap resumption** (see §7): WIP commits (git history
  = "what's been done") + a short progress note (`.nightshift/progress.md` in the
  worktree = "what's left / why blocked", a live checklist vs acceptance criteria).

### Phase 2 — Ship (a loop)
Runs through the **serial merge train** — only one slice in Ship at a time.

1. Commit the work.
2. **Rebase onto current `main`** (which may have moved while working).
3. **Conflict or red check?** → spawn a **resolver agent** (fresh local headless
   Claude) scoped to exactly this job. Its context: the conflicting hunks,
   `main`'s current state, **both slice specs** (its own + whatever landed under
   it), and the `check` command. It **edits code** to produce a semantically
   correct resolution — not just "accept theirs."
4. **Re-run the same `check`.** (The one configured gate guards both commit-time
   and integration-time — no second config.)
5. Green → **always-on AI review** (§9) → merge.
6. **Squash to exactly ONE clean commit** with an informative, outcome-focused
   message. If PR enabled, generate a proper PR description; the PR **closes the
   issue** on merge.

**Escalation cap:** the resolver gets **N attempts (default 2)** to reach
green-and-merged. On failure: slice → `blocked`, worktree **preserved** for
inspection, branch **ejected** from the queue (so it doesn't wedge everyone
behind it), a note written into the issue, human notified via configured notifier
(if any). The next queued slice proceeds. **The daemon never deadlocks and never
corrupts `main`.**

---

## 7. Autonomy & Escalation (agents never block)

The headless agent has no terminal to prompt. So:

- **Small decisions → auto-decide.** Hit a fork with no channel to ask? Pick the
  best option, record the choice, proceed. No Slack needed for the pipeline to
  flow.
- **Hard blockers → fail *into resumable state*.** For things it must not auto-do
  (mutating `main` directly, a user-defined guardrail, a check failing past the
  cap): the agent **writes what it's blocked on into the issue** (status →
  `blocked` + a note), **preserves its worktree**, logs the error to the daemon
  log, and **stops**. It does not hang.

### Resumption is cheap — never from scratch
The worktree is right there on disk, accessible locally. When you (or
`nsh resume <slice>`) pick a blocked slice back up, a **fresh agent attaches to
the existing worktree** and reads the progress record — it sees *"I already built
X, Y, Z; only this last check is failing"* and continues from that exact point.
It re-derives context from the preserved code + progress notes, not by redoing the
whole task. (Doesn't have to be the same agent process — just continues the work.)

**Optional permission gate** (`permissions.mode: ask`, for cautious adopters):
same *notifier* abstraction carries the question out-of-band (Slack button, CLI,
wmux `report-agent`). Pauses **just that one agent**; on timeout applies a
configurable default (`deny`). Off by default — the showcased flow is fully
autonomous within a sandboxed denylist.

**Default sandbox denylist** (never auto-approved even in full-auto): force-push
to `main`, `rm -rf` outside the worktree, `sudo`, editing `~/.nightshift` config/
secrets, network calls to non-allowlisted hosts.

---

## 8. Configuration

- **One central config, held by the tool, in `~/.nightshift/` (user home)** —
  *not* inside the tool repo or any target repo, so it's structurally impossible
  to accidentally commit.
- **Layered: global daemon defaults + a per-repo entry for each managed repo.**
- **`check` is per-repo, never global** — the daemon applies each repo's own
  check, never cross-contaminating.
- The **concierge's main job** is to interview you and generate/maintain this
  file. An **example config** ships in the public repo so forkers see the shape.

```yaml
# ~/.nightshift/config.yaml
defaults:
  max_parallel: 5
  executor: local-headless        # pluggable; only default at launch
  permissions:
    mode: auto                    # auto | ask
    denylist: ["force-push main", "sudo", "rm -rf outside worktree"]
  notifier: { type: none }        # none | slack | teams | wmux | ...  (optional)

repos:
  my-app:
    path: ~/code/my-app
    source: github-issues         # | local-md
    check: "make test"            # THE per-repo verification gate
    base_branch: main
    pr: { enabled: true, template: default, automerge: true }
    external_review: { required: false, provider: github-pr }
    tracker: { type: none }       # none | jira (+ creds via env)
  other-app:
    path: ~/code/other
    source: local-md
    check: "pytest && ruff check ."
    base_branch: main
    pr: { enabled: false }        # commit straight to main
```

**Machine-local ephemera** (worktree paths, running PIDs, attempt counters, merge
lock) live in a **disposable `~/.nightshift/runtime.json`** — NOT a source of
truth, rebuildable from scratch by scanning worktrees + issues on daemon restart.

---

## 9. Review

Two independent things (previously conflated):

- **AI review is ALWAYS ON.** A reviewer agent (`nightshift-review`) inspects the
  completed work as a **built-in Ship step**, every time, for everyone. **Blocking**
  findings loop back into Work to be fixed; **advisory** nits are logged and don't
  stop the merge. Non-negotiable, free quality gate.
- **External/human review is the optional layer** — `external_review.required`:
  - **`false`** (solo/local, showcase default): AI review passes → autonomously
    finish (PR + auto-merge, or straight to main). No hands.
  - **`true`** (corporate, formal process): AI review runs *first*, **then** the
    pipeline opens the PR and **stops** for human sign-off. Terminal state =
    *"PR open, human notified, NOT merged."* This is the only sanctioned human
    gate, and it's **async** (a PR sitting open), so it never hangs an agent.
- **Provider = GitHub Pull Requests at launch**, behind a **`Reviewer` interface**
  so the community can add others later (GitLab MRs, Gerrit, Microsoft's internal
  review system, …).

---

## 10. Skills (all namespaced `nightshift-*`)

**Rule:** every shipped skill is prefixed `nightshift-` so it (a) never collides
with or overrides the user's existing skills, (b) has clear provenance, and (c)
declares in its description: *"Primarily invoked autonomously by Nightshift
agents; invoke manually only when the user explicitly asks."*

| Skill | Role |
|---|---|
| `nightshift-slice` | grill-me-derived: interviews you, designs a batch of slices, asks *"Happy with these?"*, on YES writes them as `status: ready`. |
| `nightshift-review` | The always-on AI reviewer (Ship step). |
| `nightshift-resolve` | The merge-conflict / post-rebase resolver agent. |
| `nightshift-setup` | The concierge (re-runnable onboarding + reconfiguration). |

> The author's personal `grill-me` stays untouched. A user with their own slicing
> skill keeps it — it just needs to emit the §3 slice format.

---

## 11. Concierge (`nightshift-setup`)

A **Claude skill** (requires Claude), **idempotent and re-runnable at ANY time** —
not a one-shot installer. Run it day one to bootstrap; run it again any time to
add Slack you didn't have before, add a repo, flip on external review, change a
check, add Jira. It reads the current central config, shows what's set, lets you
add/modify any layer, re-validates, and can re-run the smoke test for whatever it
touched.

**What it walks a user through:**
1. **Prereqs** — daemon, `gh`, git; verify versions.
2. **Auth** — the *only* hard requirement: local Claude auth present + headless
   permission-bypass configured. **No cloud, no provisioning.** Works with
   subscription or free.
3. **Register repos** — for each, **auto-detect the check** (`pyproject.toml` →
   `pytest`; `package.json` → `npm test && tsc`; `Makefile` → `make test`) and let
   the user confirm/edit. Writes the central `repos:` config.
4. **Source choice** — `local-md` vs `github-issues` (runs `gh auth` if needed).
5. **Optional layers** — external review? Jira? notifier? Off by default; a couple
   of questions each if yes.
6. **Smoke test** — the trust-builder: creates a **throwaway dummy slice** and runs
   it through the whole loop (worktree → tiny edit → check → commit → clean up) so
   the user *watches it work once* before trusting real work. Diagnoses failures
   (bad check, auth, permissions) on the spot.

---

## 12. Secrets & Credentials

- **Reuse existing auth wherever possible** — GitHub via the user's existing
  **`gh` CLI**; Claude via the **local Claude** login. Nightshift stores **zero**
  GitHub/Claude secrets itself.
- Slack/Jira secrets live in **`~/.nightshift/secrets.env`** (0600, never in any
  repo), referenced from config via `${VAR}` interpolation.
- Central config + secrets live in **`~/.nightshift/`** (user home) — never inside
  the tool repo or a target repo, so nothing sensitive can be committed.

---

## 13. Distribution & Install

- Publish the CLI to **PyPI** → **`pipx install nightshift`** (isolated, clean).
- **`nightshift setup`** launches the concierge, which installs the `nightshift-*`
  skills into `~/.claude/skills`.
- Clone-and-run stays supported for hackers.
- Tweetable moment: **`pipx install nightshift && nightshift setup`**.

## 14. License

**MIT.** Maximum permissiveness → maximum adoption → maximum stars; frictionless
for corporate forkers. (Apache-2.0 only if an explicit patent grant is later
wanted.)

---

## 15. Extensibility (adapter interfaces)

Each optional capability is an **adapter behind a small interface**; config names
which to load. Built-ins ship in the repo; a third party adds support by
implementing the interface + naming it in config — **without touching the core
daemon.** This is the community-contribution growth loop (Teams, Linear, Discord,
GitLab, Gerrit, …).

| Interface | Built-in at launch | Community can add |
|---|---|---|
| `SourceAdapter` | `local-md`, `github-issues` | Linear, Jira-as-source, … |
| `Executor` | `local-headless` | (future) remote box |
| `Reviewer` | `github-pr` | GitLab MR, Gerrit, MS internal, … |
| `Notifier` | `none` (+ optional slack/wmux) | Teams, Discord, email, … |
| `Tracker` | `none` | Jira, Linear, … |

---

## 16. Naming

- **Working title: `Nightshift`.** Story: *your slices get worked overnight; you
  wake to shipped commits.* Clear niche, **no dev-tool collision found** (contrast:
  "Cobble" collides with two existing build tools + Cobbler + Cobblemon SEO).
- **Caveat:** the bare word is owned by Apple's "Night Shift" display feature in
  unqualified search — targeted searches (`nightshift claude`) work, but it's not
  pristine SEO. If that matters at launch, bump to a coinage (**Nightshifter**) or
  another clean candidate (**Farrier**, **Slipway**) — a find-replace.
- CLI: `nsh` (short) / `nightshift` (full).

---

## 17. Phased Roadmap (build slowly — it doesn't all ship day one)

**v0.1 — Prove the loop (single-repo, single-slice, local).**
- Daemon skeleton + `local-md` source + one worktree + Work→Ship (no parallelism).
- Per-repo `check`. One clean commit. `nightshift-slice` emits the format.
- Manual `nsh run <slice>`. This is the demo that proves the core idea.

**v0.2 — Autonomy & parallelism.**
- Always-running daemon (watch), DAG from `depends_on`, `max_parallel: 5`,
  serial merge train, `nightshift-resolve` conflict agent, escalation cap,
  resumability (`nsh resume`).

**v0.3 — Always-on AI review + GitHub.**
- `nightshift-review` in the Ship loop. `github-issues` source. PR + auto-merge
  that closes the issue. The full visible GitHub story.

**v0.4 — Concierge + distribution.**
- `nightshift-setup` (re-runnable) with auto-detected checks + smoke test.
- PyPI / `pipx` install. Example config. README with the origin story. **Launch.**

**v0.5+ — Community extensibility.**
- External review (`external_review: true`, GitHub PR), optional notifiers
  (Slack/Teams), optional Jira tracker, `Reviewer`/`Notifier`/`Tracker` interfaces
  documented for contributions.

---

## 18. Open / Deferred

- **Final name** — lock or swap `Nightshift` before launch.
- **Exact headless-Claude invocation** (permission-bypass flags, SDK vs `-p`) —
  implementation detail for v0.1.
- **Notifier reply mechanism** for the optional permission gate — defer to when
  a notifier is actually built (v0.5+).
- **Windows/macOS/Linux parity** for the daemon (author is on Windows; worktrees
  + headless Claude must work cross-platform) — validate during v0.1/v0.2.
