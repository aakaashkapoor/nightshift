---
name: nightshift-babysit
description: Review, fix and merge the PRs Nightshift opens on babysit repos (label nightshift:babysit). Use by default whenever you are driving Nightshift on a repo with babysit on, unless the user says not to. Run one pass on request, or keep watching with /loop or a monitor.
---

# Nightshift Babysit

Nightshift writes the code. You are the agent driving it, and you hold the context
it doesn't: the user's goal, the plan, and what "right" looks like. Before any of
its work lands, you check it, fix it if needed, and merge it.

## One pass

1. List waiting work: `gh pr list --label nightshift:babysit --state open --json number,title,headRefName,url`
2. For each PR, oldest first:
   1. **Check it out in its own worktree:** `git fetch origin <branch>` then
      `git worktree add ../babysit-<number> origin/<branch>`. Never in the main checkout.
   2. **Verify what this change needs. You decide.** Read the slice (the linked issue)
      and the diff, then ask:
      - Does it do what the slice asked, and does it serve the larger goal?
      - Does it actually work? Run it, not just the tests.
      - If it touches UI or visual output, **look at it**: run the app or render,
        take screenshots, compare against the intent.
      - Is anything missing, wrong, or surprising?
   3. **Decide:**
      - **Good:** `gh pr merge <number> --squash --delete-branch`.
      - **Needs a fix before it can land:** fix it on the branch yourself, run the
        repo's check command, commit, `git push origin HEAD:<branch>`, verify again,
        then merge.
      - **Good enough, with an isolated follow-up:** merge, then file the follow-up
        for Nightshift: `gh issue create --label nightshift:ready --title ... --body ...`
        (with `## Goal` and `## Acceptance criteria`).
      - **Fundamentally wrong:** `gh pr close <number> --comment "<why>"`, and rewrite
        the slice's issue so the next attempt goes better.
   4. **Conflicts with main** (another PR merged first): rebase the branch onto
      `origin/main`, resolve, re-check, push, merge.
   5. Remove your worktree: `git worktree remove ../babysit-<number>`.
3. Report briefly: merged, fixed, follow-ups filed, closed. Link each PR.

## Keep watching

Don't tail the daemon log. Nightshift tells you when it needs you:

1. Run `nsh status` once for the current picture.
2. Start `nsh wait --repo <repo>` as a **background task**. It blocks until something
   needs you, prints one JSON line per event, and exits:
   - `review_ready` (with `pr`): a PR is waiting. Do a pass.
   - `blocked` (with `reason`): read the reason; fix the issue text or file a follow-up.
   - `paused_limit` (with `until`) / `resumed`: the Claude usage limit. Nothing to do.
   - `stalled` (with `waiting`): nothing can run. Usually a blocked dependency.
   - `all_done`: the queue is empty.
   - `daemon_exit` (with `reason`): the daemon stopped. Restart it.
3. Handle the event, then start `nsh wait` again. It remembers where it stopped, so
   nothing is missed between runs.

## Rules

- Never merge something you didn't verify. "The tests pass" is not enough on its own.
- Keep fixes small and inside the slice's intent. Bigger changes go to a follow-up issue.
- The human can always merge, fix or close a PR themselves; respect what they did.
- Nightshift notices merged and closed PRs on its next tick. Don't edit its labels.
