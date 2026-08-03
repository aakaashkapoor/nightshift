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

> ⚠️ **Status: early (v0.3, pre-alpha).** The core engine works and is proven with
> a real agent (see [DEMO.md](./DEMO.md)): parallel Work in isolated worktrees, a
> serial merge-train, agent-driven conflict resolution, always-on AI review,
> escalation + resume, and GitHub-issues/PR adapters. Not yet published to PyPI.
> Follow [BUILD_PLAN.md](./BUILD_PLAN.md) for what's done and what's next.

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
grill / nightshift-slice → slices (local .md OR GitHub Issues)
        → daemon picks up ready slices (DAG-scheduled, up to 5 in parallel)
        → each in its own worktree: Work → check → AI review → Ship
        → conflicts auto-resolved by an agent, serial merge train
        → ONE clean commit, PR closes the issue, code pushed
```

## Commands

```bash
nsh init --repo .          # register a repo (auto-detects the check command)
nsh run <slice>            # run one slice by hand: Work → check → review → commit
nsh daemon [--once]        # drain ready slices (parallel Work + serial merge-train)
nsh resume <slice>         # resume a blocked slice on its preserved worktree
nsh version
```

Config lives in `~/.nightshift/config.yaml` (see [examples/config.yaml](./examples/config.yaml)) —
one central file with global defaults plus a per-repo entry (each repo's own
`check`). Slices are `.slices/*.md` files *or* GitHub issues.

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
