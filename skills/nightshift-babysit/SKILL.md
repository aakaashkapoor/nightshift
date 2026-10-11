---
name: nightshift-babysit
description: Drive a running Nightshift daemon: react to its events, review and merge the PRs that wait for you, answer agent questions and unblock slices. Use whenever you are supervising Nightshift on a repo.
---

# Nightshift Babysit

Nightshift writes the code. You are the agent driving it. You hold what it lacks: the
user's goal and what "right" looks like. Most PRs merge themselves. You handle the
rest, and every event that needs a person.

## Start
1. `nsh status` for the full picture (use `--json` to parse it).
2. Start `nsh wait --repo <repo>` as a **background task**. It blocks until something
   needs you, prints one JSON line and exits. Handle it, then run it again. It
   remembers where it stopped, so nothing is missed between runs.

## Events

| `kind` | What to do |
|---|---|
| `review_ready` (`pr`) | Review it, see below. |
| `question` | Read the question on the issue. Answer: `nsh answer <slice> "<answer>"`. If it shows the issue was under-specified, also add the answer to the issue's Decisions or Contract. |
| `needs_detail` (`problems`) | The issue failed the linter. Edit the issue body to add what is missing, then `nsh issue set <n> ready`. |
| `blocked` (`reason`) | Read the reason. Fix the issue text or the repo, then `nsh issue set <n> ready`, or `nsh resume <slice>` to continue on its existing worktree. |
| `paused_limit` / `resumed` | The Claude usage limit. Nothing to do. |
| `stalled` | Nothing can run. Find the blocked dependency and unblock it. |
| `main_red` | `main` fails its check, so no slice starts. Fix `main` first. |
| `daemon_exit` | Restart `nsh daemon --repo <repo>`. |
| `all_done` | The queue is empty. Report and stop. |

Slice ids look like `issue-12`; `<n>` is the issue number.

## Reviewing a PR
Only PRs from issues labelled `review` wait for you (or ones whose auto-merge failed).
1. `nsh pr checkout <n>` makes a worktree next to the repo, installs dependencies, and
   prints its path.
2. Verify. Read the issue and the diff. Run it, do not trust the tests alone. If
   the issue is labelled `visual`, or touches UI, look at the real output.
3. Small problem: fix it on the branch, run the repo's check, commit and
   `git push origin HEAD:<branch>`.
4. Record it: `nsh pr comment <n> "<what you verified>"`.
5. `nsh pr merge <n>` waits until the PR is mergeable, squash-merges, and removes the
   worktree.

Bigger problem: merge anyway and file a follow-up with `nsh issue new`, or close the
PR (`gh pr close <n> --comment "<why>"`) and fix the issue text so the next attempt
goes better.

## Rules
- Never change nightshift labels by hand. Use `nsh issue set <n> <action>`.
- Never resolve conflicts on Nightshift's PRs yourself. It rebases its own PRs when
  `main` moves.
- Keep fixes inside the issue's intent. Anything bigger is a follow-up issue.
- Control the daemon with `nsh pause`, `nsh unpause` and `nsh stop`, not by killing it.
- The human can always merge, fix or close a PR. Respect what they did.
