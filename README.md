# Nightshift 🌙

> **You slice the work; your machine ships it overnight.**

Nightshift is an always-running **local** daemon that picks up clean,
one-commit-sized work items, then codes, tests, reviews, and merges each one
using a **local headless Claude** in its own git worktree — resolving its own
conflicts and landing a single clean commit. No hands.

- **No cloud.** Everything runs on *your* machine with the Claude you already
  have — subscription, API key, or even free tier. Nothing to provision.
- **No babysitting.** Agents never block on a human. They auto-decide small
  choices and fail gracefully into a resumable state you can pick back up.
- **Yours to shape.** Sources (local files *or* GitHub Issues), checks,
  review, notifiers, trackers — all pluggable behind small interfaces.

> ⚠️ **Status: early (v2, pre-alpha).** The core engine works and is proven with
> a real agent (see [DEMO.md](./DEMO.md)). v2 (October 2026) adds sized issues,
> usage-limit pause and resume, a worker pool with a supervisor, deterministic
> `nsh` commands for issues, PRs and status, and a conflict ladder that tries
> plain git before an agent. Design: [the v2 spec](./docs/superpowers/specs/2026-10-11-nightshift-v2-design.md).
> Not yet published to PyPI.

## See it

A two-minute technical walkthrough, made with [motioncraft](https://github.com/aakaashkapoor/motioncraft) (which Nightshift built). Source: [`docs/video/storyboard.json`](./docs/video/storyboard.json).

https://github.com/user-attachments/assets/df0c9d40-6091-4d91-b548-dbd9a2aabb2b

## Inspiration & credit

Nightshift is **heavily inspired by [Matt Pocock](https://github.com/mattpocock)**
and his [`mattpocock/skills`](https://github.com/mattpocock/skills) — *"Skills for
Real Engineers"* — together with his talks and videos on doing genuine engineering
with coding agents rather than vibe-coding.

His philosophy of **small, composable, adapt-them-yourself agent skills** is the
foundation this project builds on: Nightshift's namespaced `nightshift-*` skills and
its skills-first workflow are a direct descendant of that idea. Nightshift's own
contribution is wrapping that approach in an **autonomous local loop** — the credit
for the underlying skills-driven engineering approach belongs to him.

If you find Nightshift useful, go star [his repo](https://github.com/mattpocock/skills)
and [subscribe to his work](https://www.aihero.dev/) — this wouldn't exist without it.

## How it works (the loop)

```
nightshift-slice (interview) -> nsh issue new -> GitHub issues (sized S/M/L, linted)
        -> daemon worker pool: best ready slice first (critical path), Touches lock
        -> each in its own worktree: build -> check -> content + file gates -> AI review
        -> rebase (conflict ladder) -> PR
        -> no `review` label: merges itself   |   `review` label: waits for babysit
        -> usage limit hit: pause until reset, resume the same session
```

The driving agent (`nightshift-babysit`) follows the daemon with `nsh wait` and only
steps in for PRs labelled `review`, questions, blocked slices and a red `main`.

## Commands

```bash
nsh init --repo .          # register a repo (auto-detects check, install, sync)
nsh daemon [--once]        # run the worker pool; restarts itself if it crashes
nsh run <slice>            # run one slice by hand
nsh resume <slice>         # resume a blocked slice on its preserved worktree
nsh status [--json]        # phase and age of every slice, waiting PRs, pause state
nsh wait [--timeout N]     # block until something needs you; prints a JSON event line
nsh pause | unpause | stop # stop starting slices / start again / exit after running ones

nsh issue new --title T --size M --goal G --acceptance A --touches PATH [--review] [--visual]
                           # file a linted v2 issue (or --body-file F); prints the URL
nsh issue set <n> ready|review|no-review|visual|no-visual|hold
                           # the only way labels change by hand
nsh answer <slice> "..."   # answer an agent's question; the slice resumes its session

nsh pr checkout <n>        # worktree for a PR with dependencies installed
nsh pr comment <n> "..."   # record what you verified
nsh pr merge <n>           # wait for mergeable, squash, delete branch, clean worktree
nsh version
```

Config lives in `~/.nightshift/config.yaml` (see [examples/config.yaml](./examples/config.yaml)) -
one central file with global defaults plus a per-repo entry (each repo's own
`check`). v2 works from GitHub issues; `.slices/*.md` files still work for basic runs.

## What happens when you hit your usage limit

When a Claude run returns a usage-limit error, Nightshift does not count it as a
failure. The daemon pauses (`paused_limit`, with the reset time), keeps the slice's
worktree and session, and starts nothing new. At the reset time it resumes the same
session where it stopped and emits `resumed`. If the error has no reset time, it
checks again every 15 minutes. You do not need to do anything.

## Install

Straight from GitHub — no PyPI needed:

```bash
pipx install git+https://github.com/aakaashkapoor/nightshift
```

Or for local development:

```bash
git clone https://github.com/aakaashkapoor/nightshift && cd nightshift
pipx install -e .          # or: pip install -e ".[dev]"
```

## Quality gate

Every change is held to a pristine bar — run the full gate locally:

```bash
nsh-check          # or: python -m nightshift._gate
```

It runs **Ruff** (lint + format), **mypy** (types), and **pytest with 100% branch
coverage** (`--cov-fail-under=100`). All three must pass. This is the repo's own
Nightshift `check` command — so Nightshift can eventually gate its own slices on it.

## Docs

- **[SPEC.md](./SPEC.md)** — the full design: invariants, architecture, config,
  lifecycle, extensibility, roadmap.
- **[BUILD_PLAN.md](./BUILD_PLAN.md)** — the ordered, task-by-task build plan.
- **[DEMO.md](./DEMO.md)** — a real end-to-end run (agent → check → clean commit).
- **[skills/](./skills)** — the `nightshift-*` Claude skills (setup concierge, slice designer).

## License

[MIT](./LICENSE).
