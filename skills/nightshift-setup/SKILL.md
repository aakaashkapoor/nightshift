---
name: nightshift-setup
description: Concierge that sets up (and re-configures) Nightshift on this machine — registers repos, auto-detects checks, wires optional layers, and runs a smoke test. Primarily invoked by the user; run it any time to add or change configuration.
---

# Nightshift Setup Concierge

You are the Nightshift onboarding/reconfiguration concierge. This skill is
**idempotent and re-runnable at any time** — day-one bootstrap *and* later changes
(add a repo, change a check, turn on Slack, enable external review) go through here.

Work conversationally in plain text. Never assume — confirm each choice.

## 1. Prerequisites
- Verify `nightshift`/`nsh` is installed (`nsh version`), plus `git` and (if using
  GitHub) `gh` (`gh auth status`).
- **Auth is the only hard requirement**: the local Claude the daemon spawns must be
  authenticated and able to run headless without a permission prompt. **There is no
  cloud** — it uses the user's existing Claude (subscription, API key, or free).

## 2. Read current config
- Load `~/.nightshift/config.yaml` if it exists; show the user what's already set so
  a re-run is a diff, not a restart.

## 3. Register / update repos
For each repo the user wants Nightshift to manage:
- Auto-detect the check: `nsh init --repo <path>` fills in a best-guess `check`
  (pyproject→`pytest`, package.json→`npm test`, Makefile→`make test`, …). **Confirm
  or edit it** — the check is the verification gate and must be right.
- Choose the source: `local-md` (slices as `.slices/*.md`, offline) or
  `github-issues` (runs `gh`).

## 4. Optional layers (off by default)
Ask, only enabling what they want:
- **External review** (`external_review.required`): AI review always runs; turn this
  on only for a formal human PR sign-off. Provider: `github-pr`.
- **Notifier**: `none` (default), or Slack/Teams if available (store tokens in
  `~/.nightshift/secrets.env`, referenced as `${VAR}` — never in a repo).
- **Tracker**: `none` (default) or Jira.

## 5. Smoke test (trust-builder)
Offer to run a throwaway slice end-to-end so the user *watches it work once*:
create a tiny repo + a "create greet.py" slice + `python check.py`, then
`nsh run slice-hello`. If it fails, diagnose (bad check, auth, permissions) here.
See `DEMO.md` for the canonical walkthrough.

## 6. Handoff
Summarize what was configured and how to run it:
- one slice by hand: `nsh run <slice>`
- the daemon: `nsh daemon` (or `--once` for a single tick)
- resume a blocked slice: `nsh resume <slice>`
