# Nightshift v0.1 — the single-slice loop, live

This is the first working slice of Nightshift: **one slice → a real local headless
Claude writes the code → the repo's check gates it → exactly one clean commit.**
No daemon, no parallelism, no GitHub yet — just the core engine, run by hand with
`nsh run`.

> ⚠️ Pre-alpha. This walkthrough is the proof-of-life for the [build plan](./BUILD_PLAN.md)
> Phase 1. Everything here runs **locally** with the Claude you already have.

## Try it yourself

**1. A throwaway target repo** with a verification `check` and a task:

```
smoke-repo/               # git repo, on `main`
├── check.py              # imports greet, asserts greet("World") == "Hello, World!"
├── .slices/
│   └── slice-hello.md    # the task (below)
└── .gitignore            # __pycache__/
```

`.slices/slice-hello.md` — the no-code spec:

```markdown
---
id: slice-hello
title: Add a greet() function
status: ready
---
## Goal
Create a file `greet.py` in the repository root containing a function
`greet(name)` that returns the string `Hello, <name>!`.

## Acceptance criteria
- [ ] `greet.py` exists at the repository root
- [ ] `greet("World")` returns exactly `Hello, World!`
```

**2. A central config** (`~/.nightshift/config.yaml`) registering the repo and its check:

```yaml
repos:
  smoke:
    path: /path/to/smoke-repo
    check: "python check.py"
    base_branch: main
```

**3. Run one slice:**

```console
$ nsh run slice-hello --repo /path/to/smoke-repo --config ~/.nightshift/config.yaml
DONE slice-hello [nightshift/slice-hello] — check passed (49bfdd9b)
```

## What happened under the hood

1. **Worktree** — created an isolated worktree on a fresh branch `nightshift/slice-hello`.
2. **Work** — spawned a local headless Claude (`claude -p`, permissions bypassed,
   prompt on stdin) with the slice spec; it wrote `greet.py`.
3. **Check** — ran `python check.py` in the worktree; it passed.
4. **Ship** — squashed the work into **one clean, informative commit**:

```
49bfdd9 Add a greet() function

    Implements slice slice-hello.

 greet.py | 3 +++
 1 file changed, 3 insertions(+)
```

The code the agent wrote:

```python
def greet(name):
    """Return a greeting for the given name."""
    return f"Hello, {name}!"
```

If the check had failed, Nightshift would retry (resuming the agent) up to a cap,
then mark the slice `blocked` and **preserve the worktree** so you can pick it back
up — never from scratch.

## Next

The daemon that watches for slices, runs many in parallel worktrees, serializes
merges, and closes GitHub issues is [Phases 2–4](./BUILD_PLAN.md). This page is
just the engine, proven.
