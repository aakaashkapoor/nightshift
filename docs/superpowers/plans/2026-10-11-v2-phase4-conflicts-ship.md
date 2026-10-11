# Nightshift v2 Phase 4: fewer conflicts, cleaner ship

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Conflicts are prevented or fixed with plain git before an LLM is ever asked; each worktree has its own dependencies; PRs merge themselves unless the issue asks for review; Nightshift keeps its own open PRs mergeable; babysit gets fail-fast helpers.

**Architecture:** Worktrees get a per-worktree install command and a process-tree kill before teardown. `info/attributes` gives registry files `merge=union`. Rebase conflicts go through a ladder in `prepare_branch`: lockfiles are regenerated, then the resolver agent handles what's left. Every conflict is counted per file. Ship reads the issue's labels: no `review` → PR merged by Nightshift; `review` → PR waits for babysit. Each poll, Nightshift re-rebases its own conflicting PRs. `nsh pr checkout|merge|comment` are the babysit helpers.

**Design:** `docs/superpowers/specs/2026-10-11-nightshift-v2-design.md` ("Avoiding conflicts", Step 5, toolkit). Field notes #7–9, #11–13, #22, #24.

---

## Rules for every task

Same as Phase 3's rules (branch `v2-phase4`; `.venv/Scripts/python`; test-first; gate with
`.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate`
→ CHECK PASSED at 100% branch coverage; no `# pragma: no cover` on new logic, only on real
subprocess/`gh` shims; fix plan bugs minimally and report; trailer lines; don't push).

Real git is fine in tests (existing tests build throwaway repos in `tmp_path`); `gh` is
always faked through the injectable runners.

---

### Task 1: per-worktree install

**Files:** `src/nightshift/config.py`, `src/nightshift/detect.py`, `src/nightshift/pipeline.py`, `src/nightshift/daemon.py`, `src/nightshift/cli.py` (`nsh init`); tests.

- `RepoConfig.install: str | None = None`; `Config._resolve` reads `install`; `register_repo(..., install=None)` writes it.
- `detect.py`: `detect_install(repo) -> str | None`: `pnpm-lock.yaml` → `pnpm install --frozen-lockfile`; `package-lock.json` → `npm ci`; `yarn.lock` → `yarn install --frozen-lockfile`; `uv.lock` → `uv sync --frozen`; else None. `detect_sync` returns the same command (post-merge sync in the main checkout; `npm ci` never dirties the lockfile, field note #11).
- `nsh init`: fills `install` and `sync` from detection unless `--install` / `--sync` given; prints them. `--symlink` stays as the opt-in for slow installs.
- `run_slice(..., install_cmd: str | None = None)`: after a fresh `create` (not when resuming), run it in the worktree with `run_check`; on failure return blocked `install failed: <last 300 chars>`. Daemon passes `self.repo_cfg.install`.

Tests: detection table; init writes them; install runs once on create, not on resume; install failure blocks with the output tail.

Commit: `Worktrees: per-worktree dependency install, detected by nsh init`

---

### Task 2: teardown kills leftover processes

**Files:** `src/nightshift/worktree.py`; tests.

`WorktreeManager(..., killer: Callable[[Path], None] = kill_processes_in, sleep=time.sleep)`.
`teardown` becomes: unlink shared dirs → `git worktree remove --force` → on failure
`killer(path)`, wait 1 s, retry up to 3 times → then the existing `_force_rmtree` fallback.
`kill_processes_in(path)` (the real shim, `# pragma: no cover`): on Windows run
PowerShell `Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*<path>*' -or $_.ExecutablePath -like '<path>*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`;
elsewhere `pkill -f <path>`. Never raises.

Tests: a fake `_git` that fails the first two removes → killer called twice → success; always failing → `_force_rmtree` fallback reached (monkeypatch it).

Commit: `Teardown: kill processes holding the worktree, then retry`

---

### Task 3: union merge for registry files

**Files:** `src/nightshift/config.py` (`registries: list[str] = []`), `src/nightshift/worktree.py`; tests.

`WorktreeManager(..., registries: list[str] = ())`. In `create`, ensure the shared
`<git-common-dir>/info/attributes` contains `<path> merge=union` for each registry
(idempotent, same helper style as the `.nightshift/` exclude from Phase 3; factor a
`_ensure_line(file, line)` helper and use it for both). `info/attributes` is local to
the clone and never committed. Daemon / `run_daemon_cli` pass `repo_cfg.registries`.

Tests: the attribute lines are written once; with a real repo, two branches that each
append a different export line to `src/index.ts` rebase cleanly onto each other (no
conflict, both lines present) when the registry is configured, and conflict when not.

Commit: `Registries: append-only files merge with git's union driver`

---

### Task 4: the conflict ladder and conflict counts

**Files:** `src/nightshift/pipeline.py` (`prepare_branch`), `src/nightshift/conflicts.py` (new), `src/nightshift/daemon.py`, `src/nightshift/state.py` (render); tests.

`conflicts.py`:

```python
LOCKFILES = {
    "package-lock.json": "npm install --package-lock-only --ignore-scripts",
    "pnpm-lock.yaml": "pnpm install --lockfile-only",
    "yarn.lock": "yarn install --mode update-lockfile",
    "uv.lock": "uv lock",
    "poetry.lock": "poetry lock --no-update",
    "Cargo.lock": "cargo generate-lockfile",
}


def conflicted_files(worktree) -> list[str]:  # git diff --name-only --diff-filter=U


def regenerate_lockfiles(worktree, files) -> list[str]:
    """For each conflicted lockfile: take the base's version, regenerate, stage it.

    Returns the lockfiles it fixed. During a rebase, 'ours' is the base being rebased onto.
    """
    fixed = []
    for f in files:
        cmd = LOCKFILES.get(Path(f).name)
        if cmd is None:
            continue
        git checkout --ours -- f
        if run_check(cmd, Path(worktree) / Path(f).parent).passed:
            git add -- f
            fixed.append(f)
    return fixed


class ConflictLog:  # counts per file in <repo home>/conflicts.json
    def record(self, files: list[str], how: str) -> None   # how: "lockfile" | "agent" | "unresolved"
    def counts(self) -> dict[str, int]
```

`prepare_branch(..., on_conflict: Callable[[list[str], str], None] | None = None)`. When
`git rebase` stops:
1. `files = conflicted_files(wt)`.
2. `fixed = regenerate_lockfiles(wt, files)`; if every file is fixed → `git -c core.editor=true rebase --continue`; report `on_conflict(fixed, "lockfile")`; loop (the rebase may stop again on the next commit).
3. Otherwise the resolver agent runs as today on the rest; report `on_conflict(rest, "agent")` or `(rest, "unresolved")`.

(Registry files never reach here when configured: the union driver merged them.)

Daemon: `ConflictLog(home / "conflicts.json")` passed as `conflicts=`; `on_conflict` emits a
`conflict` event (`files`, `how`) and records the counts. `state.json` daemon field
`conflicts` = top 5 files by count; `render_status` prints a `conflicts:` line with them.

Tests: real-git lockfile conflict resolved with a fake regenerate command (monkeypatch
`LOCKFILES` to `{"package-lock.json": "python -c \"open('package-lock.json','w').write('{}')\""}`),
mixed lockfile + code conflict reaches the resolver with only the code file, counts persist
and render.

Commit: `Conflict ladder: regenerate lockfiles before asking an agent; count conflicts per file`

---

### Task 5: ship merges unless the issue asks for review

**Files:** `src/nightshift/slice.py` (`labels: set[str]`), `src/nightshift/github.py` (populate labels), `src/nightshift/pr.py`, `src/nightshift/daemon.py` (`_open_for_babysit` → `_ship_pr`); tests.

- `Slice.labels: set[str] = field(default_factory=set)`; the GitHub source fills it from the issue labels.
- `REVIEW_LABEL = "review"` in `pr.py`.
- `GitHubPR.merge(branch, *, wait=60.0, poll=3.0, sleep=time.sleep, clock=time.monotonic)`:
  poll `gh pr view <branch> --json mergeable` until it isn't `UNKNOWN` (field note #8) or
  the wait runs out; if `MERGEABLE` → `gh pr merge <branch> --squash --delete-branch`;
  otherwise raise `RuntimeError(f"not mergeable: {state}")`.
- `build_pr_body(sl, closes_issue, decisions=())` adds a `## Decisions` list when given.
- Daemon ship for babysit repos, after a successful `prepare_branch` and PR open:
  - issue has `review` → as today (babysit label, `in-review`, `review_ready`).
  - otherwise → `pr.merge(branch)`; success → `source.set_status(id, DONE)` is not needed
    for GitHub (the PR's `Closes #N` closes it), but call it anyway for consistency; emit
    `merged` with `pr`; drop state; teardown. Failure → fall back to the review path with
    `review_ready` carrying `reason="auto-merge failed: ..."`.
- Rename `_open_for_babysit` → `_ship_pr`; the `babysit` config flag keeps its meaning
  "ship through PRs" (field rename is out of scope).

Tests: no-review issue → PR opened and merged, event `merged`; review issue → PR left
open, `review_ready`; merge raises → falls back to review with the reason; `merge()`
waits through `UNKNOWN`, raises on `CONFLICTING`, times out → raises; decisions in the body.

Commit: `Ship: PRs merge themselves unless the issue is labelled review`

---

### Task 6: Nightshift keeps its own PRs mergeable

**Files:** `src/nightshift/pr.py` (`mergeable(branch)`), `src/nightshift/worktree.py` (`checkout_remote(slice_id)`), `src/nightshift/daemon.py` (`_watch_reviews`); tests.

In `_watch_reviews`, for each `in-review` slice whose PR is `OPEN` and `mergeable` is
`CONFLICTING`:
1. `worktrees.checkout_remote(id)`: `git fetch origin <branch>`, then
   `git worktree add -B <branch> <path> origin/<branch>` (tear down a stale worktree first). Run the install command.
2. `prepare_branch` (the full ladder + check).
3. ok → `git push --force-with-lease origin HEAD:<branch>`; emit `pr_rebased`; log it.
   not ok → emit `blocked` with `reason="PR conflicts with main and could not be rebased: <detail>"`
   and leave the PR open (babysit decides); remember the base commit in runtime
   (`rebase_failed_on`) so it doesn't retry until `main` moves again.
4. Tear the worktree down.

Tests (real git with a bare `origin` remote in `tmp_path`, `gh` faked): a PR branch that
conflicts after `main` moves is rebased and force-pushed (remote branch now contains main's
commit); an unresolvable one emits `blocked` once per base commit.

Commit: `Babysit: Nightshift rebases its own conflicting PRs when main moves`

---

### Task 7: `nsh pr checkout | merge | comment`

**Files:** `src/nightshift/prtools.py` (new), `src/nightshift/cli.py` (a `pr` sub-app: `pr_app = typer.Typer()`; `app.add_typer(pr_app, name="pr")`); tests.

- `nsh pr checkout <n>`: `gh pr view <n> --json headRefName`; `git fetch origin <head>`;
  `git worktree add <repo>/../<repo-name>-pr-<n> origin/<head>` (fail if it exists);
  run the repo's `install`; print the path.
- `nsh pr merge <n>`: `GitHubPR.merge` by number (wait for mergeability, squash, delete
  branch); then remove the `-pr-<n>` worktree if present (with the Task 2 teardown).
- `nsh pr comment <n> "<what was verified>"`: `gh pr comment <n> --body "✅ Verified:\n\n<text>"` (field note #10).
- Every step fails fast with a clear message and a non-zero exit (field note #9).

Tests with fake `gh`/git runners for each command and each failure.

Commit: `Babysit helpers: nsh pr checkout, merge and comment`

---

### Task 8: deterministic content gate (UTF-8, conflict markers)

**Files:** `src/nightshift/gates.py` (new), `src/nightshift/pipeline.py`; tests `tests/test_gates.py`.

Design Step 3 / field note #16. After the check passes in `run_slice` (before review):

```python
def content_problems(worktree) -> list[str]:
    """Changed or new text files that aren't UTF-8 or still hold conflict markers."""
    # files: `git add -A -N` then `git diff --name-only HEAD` + committed changes vs merge-base
    # skip binaries: a file whose first 8 KB contain b"\0"
    # problem strings: "<path>: not UTF-8 (byte 0xA9 at offset 1234)"
    #                  "<path>:<line>: conflict marker"
    # markers: lines starting with "<<<<<<< ", "=======" exactly, ">>>>>>> "
```

Problems are treated like a red check: the same session is resumed with
`check_failed_prompt("nightshift content gate", "\n".join(problems))`, counting as an
attempt. Tests: a cp1252 file is flagged with its offset; a marker is flagged with its line;
a binary file and a clean UTF-8 file pass; the retry prompt carries the problems.

Commit: `Content gate: changed files must be UTF-8 with no conflict markers`
