# Babysit: the driving agent reviews Nightshift's work before it merges

Status: design, approved in conversation 2026-10-08. Extends SPEC §9 (Review).

## Why

Nightshift's built-in gate (`check` + always-on AI review) only judges *code*:
the tests pass and the diff looks right. It can't tell whether a UI looks right,
whether a rendered video frame is legible, or whether the change actually serves
the larger goal the human had in mind. The agent that is *driving* Nightshift
(the interactive Claude/Copilot/Codex session that sliced the work) has that
context. Babysit lets that agent inspect each finished slice, fix it if needed,
and merge it.

First real user: the `motioncraft` video engine, where most work is visual.

## Roles

- **Nightshift writes the code.** Daemon, worktrees, `check`, AI review: all
  unchanged.
- **The driving agent babysits.** It isn't part of the daemon. It's whatever agent
  the human is talking to, using the `nightshift-babysit` skill.

## Behaviour

### 1. Babysit is on by default

New per-repo config key, default `true`:

```yaml
repos:
  my-app:
    babysit: true        # default. false → today's behaviour (auto-merge)
```

`babysit: true` implies the PR flow: it requires `source: github-issues` (or at
least a GitHub remote) and `pr.enabled`. If a repo has babysit on but no GitHub
remote, config loading fails with a clear message ("babysit needs PRs; set
`babysit: false` or add a remote"). Local-only babysit is deferred.

This is the agent-driven form of the existing `external_review.required: true`
(§9). It's the same sanctioned async gate: a PR sitting open, so no agent ever
blocks (invariant 3).

### 2. Daemon: open a PR and stop (wire up the existing PR flow)

`pr.py` exists but the daemon doesn't use it yet. With babysit on, after the
slice's `check` and AI review pass, the Ship step:

1. squashes to one commit (invariant 2) and rebases onto `base_branch`,
2. pushes the branch and opens a PR (`pr.py`, body `Closes #N`) with
   **automerge off**, labelled `nightshift:babysit`,
3. marks the slice `in-review` (issue label), **tears down the local worktree**
   (the branch lives on the remote now), and frees its parallel slot,
4. moves on. It does **not** merge.

With babysit off, the same path runs but with automerge on (today's intent in §9).

### 3. Daemon: notice the merge

On each tick the daemon checks its `in-review` slices. When a PR has merged, it:
pulls `base_branch`, runs the repo's `sync`, marks the slice `done`, and unblocks
dependants. If a PR is **closed without merging**, the slice goes to `blocked` with
a note (the human or babysit decides what happens next).

Dependants of an `in-review` slice wait until it merges. (They can't build on code
that isn't on `main`.)

### 4. The `nightshift-babysit` skill

A skill for the driving agent, not a daemon agent. It's agent-neutral (Agent Skills
format: works in Claude Code, Copilot CLI, Codex).

**One pass:**
1. List open PRs labelled `nightshift:babysit` (`gh pr list`).
2. For each, oldest first:
   1. Check the branch out into its **own** worktree.
   2. **Verify whatever this change needs.** The agent decides each time: does
      the code match the slice's goal and the larger plan? Does the UI actually
      look right (run it, screenshot it)? Does the render work? The skill prompt
      gives general guidelines, not a per-repo checklist.
   3. Decide:
      - **Good:** merge it (`gh pr merge --squash`).
      - **Needs a fix it can't ship without:** fix it on the branch, re-run
        `check`, commit, push, re-verify, merge.
      - **Has an isolated issue that can wait:** merge as is, and file a new
        `nightshift:ready` issue describing the follow-up.
      - **Fundamentally wrong:** close the PR with a note explaining why, and
        reopen or rewrite the slice's issue.
   4. If the PR conflicts with `main` (because another babysat PR merged first),
      the babysitting agent resolves the conflict itself.
   5. Remove its worktree.
3. Report a short summary: merged / fixed / follow-ups filed / closed.

**Continuous mode:** the agent sets up its own watcher (Claude Code: `/loop` or
a Monitor on `gh pr list`). Nightshift ships no watcher; that's the agent's job.

**Default:** an agent that is driving Nightshift on a babysit repo babysits by
default (the skill's description says so), unless the human says not to.

### 5. Human override

The human can always merge, fix or close a babysit PR themselves. Nightshift only
cares about the end state (merged / closed).

## Out of scope (for now)

- Babysit for `local-md`-only repos with no remote.
- A retry cap or rework loop back into Nightshift: babysit fixes things itself,
  or files follow-ups.
- Per-repo verification files. Guidelines live in the skill prompt only.

## Testing

- Unit (fake git/gh runners, as `pr.py` does today): babysit config validation;
  Ship opens a PR with automerge off and marks `in-review`; worktree teardown and
  slot release; tick detects merged → `done` + sync + dependants unblocked; closed
  unmerged → `blocked` with note; babysit off → automerge on.
- `nsh-check` stays green with 100% branch coverage.
- Live acceptance: one real slice on a scratch GitHub repo goes PR → babysit pass
  (a Claude session using the skill) → merged → daemon marks it done.
