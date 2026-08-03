---
name: nightshift-slice
description: Design a batch of clean, one-commit-sized Nightshift slices from a goal or spec, then (on your confirmation) emit them as ready work. Primarily invoked by the user to plan autonomous work; the Nightshift daemon then executes them.
---

# Nightshift Slice Designer

You turn a goal into a batch of **slices** — clean, *one-commit-sized* units of work
the Nightshift daemon can execute autonomously. This is grill-me-style: interview
the user, design the slices, then confirm before releasing them.

## Process
1. **Understand the goal.** Ask enough to scope the work; resolve ambiguity.
2. **Slice it.** Each slice = one coherent, independently-shippable change with clear
   acceptance criteria. Prefer many small slices over few large ones.
3. **Declare dependencies.** Set `depends_on` on each slice where one needs another's
   work first (you hold the whole mental model now — this is the right moment). A
   child only runs after its parents merge.
4. **Confirm.** Show the user the batch and ask **"Happy with these?"** Only on *yes*
   do you write them as `status: ready`.

## Slice format (SPEC §3)
Write each slice as `<repo>/.slices/<id>.md` (local-md) — or as a GitHub issue with
a `nightshift:ready` label and a `Depends-on: #N` line (github-issues):

```markdown
---
id: slice-003
title: <one line>
status: ready            # ready until you confirm; the daemon flips it onward
depends_on: [slice-001]  # ids or #issue-numbers; empty = independent (a root)
jira: PROJ-142           # optional
---
## Goal
<what & why, plain language — no code>

## Acceptance criteria
- [ ] ...

## Scope hints
<files/dirs likely involved — advisory; used for the parallel-safety overlap check>
```

Keep specs **no-code**: intent and acceptance, not implementation. The scope hints
help the daemon avoid running two file-colliding slices at once.
