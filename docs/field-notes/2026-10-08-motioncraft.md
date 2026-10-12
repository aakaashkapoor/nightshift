# Field notes: building motioncraft with Nightshift + babysit

A live log of Nightshift's limitations and problems, found while it built
[motioncraft](https://github.com/aakaashkapoor/motioncraft) from scratch on
2026-10-08 (about 45 issues, with babysit review on every PR). The driving
agent appends to it as it works. Each entry: **what happened → impact → suggested fix**.
Severity: 🔴 high (cost hours or broke things) · 🟡 medium · 🟢 low.

---

## Throughput and scheduling

### 🔴 1. No continuous intake: a batch blocks new work until all of it finishes
`tick()` picks a batch, runs Work for all of it in parallel, then waits for the
whole batch before taking anything new. Issues filed while a batch runs (even
ones with no dependencies) wait for the slowest slice in it.
- Seen: #19 (MP4) ran alone; #20–22, filed seconds later, waited about 6 minutes.
  Wave 2's 7 ready slices ran as 5 + 2 because of max_parallel, and the 2
  (including the critical-path #31) wait for the whole first 5.
- **Fix:** a worker pool: start a new ready slice the moment a slot frees up.
  Prioritise slices on the critical path (the most dependants downstream).

### 🔴 2. No awareness of the critical path
Batch selection is in issue order, not by importance. #31 (transitions, which
unblocks shared elements, the guide and the showcase) was left out of the first
Wave 2 batch while leaf components ran.
- **Fix:** score runnable slices by the number of transitive dependants
  (or the longest path to the end) and pick the highest first.

### 🟡 3. Batch selection can't see work created during the batch
New issues are only seen at the next tick, which comes after the batch.
Together with #1, a quick fix-up or follow-up issue waits a full batch.
- **Fix:** comes for free with #1 (a pool polling the source).

### 🟡 4. Scope-hint overlap check is token-exact and coarse
Slices conflict only if a scope-hint token matches exactly. Three component
slices that all listed `src/kit/` would have been serialised, while slices that
edit the same file under different hints (`src/index.ts`, `src/kit/index.ts`)
run in parallel and conflict at merge.
- **Fix:** compare by path prefix, and allow declaring "shared registry files"
  (e.g. `src/index.ts`) whose edits are expected and auto-resolved (see #9).

## Babysit (PR review) workflow

### 🔴 5. The driving agent is a single point of delay, and it was missed
Every babysit PR waits for the driving agent. When the agent is busy (fixing
something else, or its monitor fails), the pipeline stalls.
- Seen: three component PRs sat about 90 minutes because the agent's log
  monitor (`tail -F | grep`) silently stopped delivering lines. Dependants
  waited too.
- **Fix:** have Nightshift emit a reliable notification for "PR waiting for
  babysit" (a notifier event: wmux `report-agent`, Slack, a file the agent can
  poll), plus a reminder if a PR waits longer than N minutes. Babysit's skill
  should poll `gh pr list` as the primary signal, not the log.

### 🔴 6. Parallel slices only see `main`, so they can't see each other
Each slice works against `main` as it was when it started, so siblings
duplicate or contradict each other. Each PR passes alone; only the combination breaks.
- Seen:
  - #7/#8: both defined `Aspect`/`ASPECTS`.
  - #16/#17: both rewrote `Caption.tsx` and the whole `kit.test.tsx`.
  - #43/#45: both defined `SpringPreset`.
  - #44/#45: the theme referenced `"Geist"` while the fonts slice registered
    `mc-sans`, so all text silently fell back to a system font. Tests passed;
    only a render showed it.
- **Fix ideas:**
  1. Slicing should assign each shared concept (types, tokens, names) to exactly
     one slice, with the others depending on it.
  2. When siblings run in parallel, give each a short "sibling contract" (what
     the others are adding, with names) in its prompt.
  3. An integration check: merge all open sibling PRs into a scratch branch,
     run `check`, and flag failures to babysit before review.

### 🟡 7. Merge conflicts in registry/export files on almost every wave
Every new component adds one line to `src/kit/index.ts` and `src/index.ts`, so
every parallel PR conflicts with the next. They're always trivially resolved by
taking both sides, but each one costs the driving agent a rebase, push and wait.
- **Fix:** an "append-only regions" resolver for known registry files (union
  merge), or a generated index (codegen at check time) so slices never edit
  shared lists.

### 🟡 8. After a babysit force-push, GitHub's mergeability lags
`gh pr merge` failed with "not mergeable" right after a correct force-push,
because GitHub hadn't recomputed. The agent has to poll `mergeable` until it
isn't `UNKNOWN`.
- **Fix:** the babysit skill (or an `nsh babysit-merge` helper) should wait for
  mergeability and retry.

### 🟡 9. Babysit fixes are hand-made, and error-prone
Resolving conflicts by hand-written scripts went wrong twice in one session:
- A Python resolver failed but the following `git commit` still ran, committing
  conflict markers to a local branch (caught before push).
- A rebase replayed a broken original commit before the fix commit, so the
  conflict hit the broken file first.
- **Fix:** ship babysit helpers: `babysit checkout <pr>` (worktree plus deps),
  `babysit combine <prs...>` (merge open PRs together, union-resolve registry
  files, run check), `babysit push-fix`, `babysit merge` (wait for mergeable,
  squash, delete branch, clean worktree). All fail-fast.

### 🟢 10. No record of what babysit verified
Babysit's checks (renders looked at, probes run) live only in the agent's
transcript, not on the PR.
- **Fix:** babysit posts a short review comment (what it checked, with
  screenshots for visual work) before merging.

## Environment and worktrees

### 🔴 11. `npm install` as the post-merge `sync` deletes native optional deps
npm/cli#4828: a second `npm install` dropped `@rolldown/binding-win32-x64-msvc`,
which broke `vitest` (the `check`) for every slice. It also left
`package-lock.json` modified on `main`.
- **Fix:** default `sync` to `npm ci` for npm repos (`nsh init` detection), and
  warn when the main checkout is dirty after sync.

### 🔴 12. Shared `node_modules` via a junction is fragile on Windows
- Slices that add a dependency replace the junction with a real directory, or
  write through it into the main checkout.
- Deleting a review folder containing a junction with `rm -rf` (Git Bash)
  deleted the **real** `node_modules` in the main checkout. That broke the next
  check and needed a full reinstall.
- It also breaks the other way: the post-merge `npm ci` in the main checkout
  (2026-10-09) ran while a slice was checking through its junction. The
  install was left half-done (8 packages, no `.bin`), so the slice's check
  failed and it was marked BLOCKED for a reason that had nothing to do with
  its code.
- **Fix:** don't share `node_modules` by default. Use `npm ci` per worktree
  (with npm's cache it's fast), or pnpm's store. If junctions stay, always
  remove the junction itself first (`rmdir`), never recurse through it, and
  give babysit the same helper.

### 🟡 13. Leftover processes lock worktree folders on Windows
`tsx`/esbuild service processes survive after scripts finish and keep handles
open, so `git worktree remove` fails with "Permission denied" and later
`npm ci` fails with EPERM on `esbuild.exe`.
- **Fix:** teardown kills child process trees started in a worktree. Babysit
  helpers run scripts in a job object or process group. Retry teardown later.

### 🟡 14. A slice's check must test that slice's code (Python repos)
When Nightshift built itself, the check imported the installed (editable,
main-checkout) package, not the worktree's code, so every slice tested `main`.
Fixed by `PYTHONPATH=src` in the check command.
- **Fix:** `nsh init` for Python repos should generate a worktree-safe check,
  and docs should warn about editable installs.

### 🟡 15. Repo content can break the shared quality gate
Committing a plan document with Python code blocks made `ruff format` (0.16
formats Markdown code blocks) fail the gate for every slice. All 5 parallel
slices were blocked after 3 attempts each, with the agents' work intact but
red.
- **Fix:** when every slice in a batch fails the check the same way, suspect the
  base, not the slices. Run `check` on clean `main` first and stop with
  "main is red" rather than burning 3 attempts per slice.

### 🟢 16. Text encoding of generated files
An agent wrote `licenses/README.md` in cp1252 (the © sign as byte 0xA9),
breaking UTF-8 tooling later.
- **Fix:** a check (or the agent rules) enforcing UTF-8 for text files, e.g. a
  pre-merge scan.

## Daemon behaviour

### 🟡 17. Config is read only at start
Changing `sync` (npm install → npm ci) needed a daemon restart.
- **Fix:** reload config each tick, or on file change.

### 🟡 18. No clean pause or stop
Restarting meant killing the background process; it was idle by luck.
- **Fix:** `nsh pause` / `nsh stop --after-current` with a state file.

### 🟢 19. Runtime state and labels aren't visible
Seeing what's in flight meant reading the daemon log or GitHub labels.
- **Fix:** `nsh status`: running slices, their age, waiting PRs, blocked items
  with reasons.

## Slicing (task design)

### 🟡 20. The slicer over-chains dependencies
The first motioncraft batch was a chain (#1 → #2 → … → #6), so it ran one at a
time. Later batches built a real dependency graph by hand and ran 4–5 in
parallel.
- **Fix:** `nightshift-slice` should ask "does B need A's *code*, or just its
  concept?" and prefer a shared-contract slice up front (types, names) followed
  by parallel implementers.

### 🟢 21. Scope hints double as conflict hints, which is confusing
Hints are written for the agent ("look here") but used by the scheduler for
overlap. Hand-filed issues had shared directory hints that would serialise
work.
- **Fix:** separate `## Touches` (scheduler) from `## Scope hints` (agent).

## Integration across waves

### 🔴 22. With babysit on, merge conflicts land on the reviewing agent
When `main` moves while a PR waits, Nightshift leaves the PR conflicting and
the reviewing agent has to resolve it. PR #53 (VideoClip) predated #52
(transitions) and conflicted in 7 files; resolving them took a full session
handoff.
- **Fix:** Nightshift should rebase its own open PRs (re-running the slice's
  agent on the conflict) whenever `main` moves, before asking for review.

### 🔴 23. Parallel PRs built the same thing twice
Two sibling slices both created a `windows.ts` barrel, and two window-chrome
implementations (`AppWindow` and `WindowChrome`) shipped side by side. Each PR
was fine; together they duplicate.
- **Fix:** same root cause as #6. A shared "contracts" slice (types, file
  names, which component owns what) should run first, and siblings depend on it.

### 🟡 24. Union-merging registry files duplicated whole files
Taking both sides of `src/index.ts` / `src/kit/index.ts` conflicts, hunk by
hunk, duplicated a whole file's contents twice.
- **Fix:** rebuild registries from `main` plus each branch's added lines
  (or generate them), never union whole hunks. See #7.

### 🟡 25. An integration contract existed but wasn't shared
`Section` passes an `area` slot to its child, but most kit components ignore
it, so a Card overlapped the Section headline in 16:9 (now issue #54).
- **Fix:** when a slice introduces a contract that other components must
  honour, the slicer should file follow-ups for each consumer, or add a check
  that every component accepts it.

### 🟡 26. Siblings need a contracts-first rule written down
Notes #6, #23 and #25 have one cause. A rule in the target repo's AGENTS.md
(or in `nightshift-slice`) would prevent them: "if two slices add to the same
concept, first slice the shared contract; implementers depend on it."
- **Fix:** add that rule to the slicer skill and to the AGENTS.md template.

### 🔴 27. The daemon stalled, then died, without saying why
After #59 merged (02:50), the last open slice (#41, deps #40 and #59, both
closed) stayed "none runnable" for 95 ticks. Running the same code by hand
(`GitHubIssuesSource.list_all()` + `Dag.runnable()`) returned `issue-41`. The
process then exited around 04:27 with nothing in the log. A restart at 09:52
picked #41 up on its first tick. About seven hours were lost overnight, which
is exactly when Nightshift is supposed to work.
- **Fix:** log *why* each ready slice isn't runnable (which dependency, with
  its status) at least when that set changes; log on exit (including signals
  and uncaught errors); and run the daemon under a supervisor that restarts it
  and notifies the driver.

### 🟡 28. Text after "## Scope hints" silently becomes hints
`scope_hints()` collects every path-like token until the next `## ` heading. The
v3 issues ended with a shared paragraph after the hints ("read
`docs/design-v3.md` and `AGENTS.md` ... `src/index.ts` ... `scripts/stills.ts`"),
so all nine parallel issues "overlapped" and the daemon ran them one at a time.
Nothing in the log said why.
- **Fix:** log the overlap that kept a runnable slice out of a batch; only read
  hints from list items or a code span per line; warn when two issues share
  every hint.

### 🔴 29. A usage limit looks like a git error, and burns the queue
At 14:07 the account hit its usage limit mid-batch. Every `claude -p` run
stopped before writing anything, so each slice failed at `git commit` with an
empty error ("nothing to commit" goes to stdout). Five slices were marked
BLOCKED with "git commit ... failed:", and the daemon then started the next
ready slice, which failed the same way in 45 seconds. Nothing said "usage
limit". Recovery meant tearing down six empty worktrees and relabelling.
- **Fix:** read the executor's JSON (`is_error`, the result text) and recognise
  rate/usage-limit errors; then pause the daemon until the reset time instead of
  blocking slices. Treat "no changes" as its own failure with the agent's last
  message, not a git error.

### 📝 30. Owner's planned change to the work prompt (noted 2026-10-09, not done yet)
The work prompt is a three-line wrapper around the issue body, so each run
jumps straight into code. The owner wants it to say:
- first write a **technical spec** for the slice using the **superpowers**
  skills (brainstorming / writing-plans), and
- then do the work with **subagent-driven development** (split the plan into
  small tasks, dispatch subagents, wait for them, review).
The owner will update this himself later. Related gap found the same day: on a
red check, the retry resumes the session with the same prompt and never passes
the check's output, so the agent has to rediscover what failed.

## Good things worth keeping

- `nsh resume` reattached to preserved worktrees and finished slices that had
  failed only because `main` was red. No work was lost.
- The worktree-per-slice model and one-commit-per-slice made babysit review easy.
- Babysit caught every real integration bug before it reached `main`. Rendering
  the output was essential: two of the bugs passed all tests.

## Status after v2 (2026-10-11)

Each note was checked against the code on branch `v2-phase5`.

### Addressed

| # | How |
|---|---|
| 1, 3 | Worker pool: a freed slot takes the next ready slice; new issues are seen on the next poll. |
| 2 | Ready slices are started by transitive-dependant count (critical path first). |
| 4 | Touches overlap is a path-prefix comparison; `registries` in config marks append-only files. |
| 5 | Events go to `events.jsonl`; `nsh wait` blocks on them. (The "waiting too long" reminder is not built, see open.) |
| 7, 24 | Registry files get `merge=union` (git's line-level union driver) in each worktree. Not yet seen to fix the whole-file duplication in #24; watch for it. |
| 8 | `nsh pr merge` waits out `UNKNOWN` mergeability before merging. |
| 12 | `npm ci` / `pnpm` / `uv sync` run per worktree. `symlink_dirs` is empty unless set. |
| 13 | Worktree teardown kills processes holding the folder and retries. |
| 15 | `main_gate` runs the check on clean `main` first; red means `main_red`, no attempts burned. |
| 16 | Content gate: changed text files must be UTF-8 with no conflict markers. |
| 17 | Config is reloaded every tick. |
| 18 | `nsh pause`, `nsh unpause`, `nsh stop` (exits after running slices finish). |
| 19 | `nsh status [--json]`. |
| 20 | `nightshift-slice` asks "code or only concept?" and files contract issues first. |
| 21 | `## Touches` (scheduler) is separate from agent-facing sections. |
| 22 | Nightshift rebases its own open PRs when `main` moves. |
| 23 | Contracts-first rule and the `## Contract` section (Owns / Uses) in the issue template and slicer skill. |
| 27 | A supervisor restarts the daemon; it logs why slices are not runnable and logs every exit. |
| 29 | Usage limits are detected; the daemon pauses until the reset and the slice is not charged an attempt. |
| 30 | Prompts are per size; a red check resumes the session with the trimmed failing output. |

### Partly addressed (the rest is open)

- **#6** Contracts-first slicing is in. Still open: a "sibling contract" in each prompt, and an integration check that merges all open PRs into a scratch branch.
- **#9** `nsh pr checkout`, `merge` and `comment` exist. Open: `combine` and `push-fix`.
- **#10** `nsh pr comment` records what was verified. Open: attaching screenshots.
- **#11** `sync` defaults to `npm ci`. Open: warning when the main checkout is dirty after sync.
- **#25** The slicer rule exists. Open: filing follow-ups for each consumer of a new contract, or a check that consumers honour it.
- **#26** The rule is in the slicer skill. Open: an `AGENTS.md` template that carries it.
- **#28** Touches is read only from list items and stops at a blank line. Open: warning when two issues share every hint.
- **#5** Open: a reminder when a PR waits longer than N minutes, and wmux `report-agent` forwarding.

### Open

- **#14** `nsh init` does not generate a worktree-safe check for Python repos (editable installs still test `main`).

## Found after v2 (2026-10-11)

### 🔴 31. A missing GitHub label wedged a new issue (fixed)
The first issue filed after v2 (motioncraft #96) never started. `set_status`
removed `nightshift:needs-detail`, a label the repo had never had, so `gh` failed
after it had already added `in-progress`. The issue kept both labels, and the daemon
kept a Touches lock for a slice that wasn't running. Only a restart cleared it.
- **Fixed in 31e625c:** the source creates its labels on first write, label
  precedence is fixed, and a failed start releases its lock.

### 🟡 32. The main gate dirtied the user's checkout (fixed)
The real-Claude smoke test found that running the check in the main checkout left
`__pycache__` files that blocked the next fast-forward merge.
- **Fixed in d85b6da:** the gate runs in its own `_main-gate` worktree.

### 🟡 33. Subagent edits wrote CRLF files (fixed)
A Windows subagent's edits converted files to CRLF and swept an untracked cache
into a commit.
- **Fixed in 98d6e97:** `.gitattributes` enforces LF. Commits should add named
  files, never `git add -A`, when other work is in the tree.

### 🟢 34. Closed issues keep their in-flight label (open)
motioncraft #98 auto-merged (PR #99) and closed, but still carries
`nightshift:in-progress`. #75 (merged by hand) still carries `nightshift:in-review`.
The issue is closed by the PR's "Closes #N", and nothing removes Nightshift's
label afterwards, so the issue list shows finished work as in flight.
- **Fix:** when a slice is marked done (auto-merge or `nsh pr merge`), clear the
  Nightshift status labels on its issue.

### 🟢 35. The README video came out full-range colour (open, motioncraft #102)
motioncraft renders `yuvj420p` (full range, sRGB transfer). The ffmpeg re-encode
kept that, and the README video wouldn't play on the user's device until it was
re-encoded as `yuv420p` limited range. Re-encodes now pass
`-vf scale=in_range=full:out_range=tv,format=yuv420p -color_range tv`.
