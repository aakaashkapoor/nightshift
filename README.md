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

> ⚠️ **Status: design stage (pre-alpha).** The daemon isn't built yet. This repo
> currently holds the design spec and the step-by-step build plan. Follow along
> as it comes together.

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

## Docs

- **[SPEC.md](./SPEC.md)** — the full design: invariants, architecture, config,
  lifecycle, extensibility, roadmap.
- **[BUILD_PLAN.md](./BUILD_PLAN.md)** — the ordered, task-by-task build plan
  (executed manually until Nightshift can self-host).

## Install

> Coming with v0.4. The intended experience:
>
> ```bash
> pipx install nightshift && nightshift setup
> ```

## License

[MIT](./LICENSE).
