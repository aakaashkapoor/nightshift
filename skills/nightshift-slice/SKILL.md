---
name: nightshift-slice
description: Turn a goal into a batch of sized (S/M/L), contracts-first Nightshift issues and file them with `nsh issue new`. Use when the user wants to plan autonomous work for the Nightshift daemon.
---

# Nightshift Slice Designer

You turn a goal into a batch of GitHub issues the Nightshift daemon can build alone,
in parallel, without guessing. Each issue is one **slice**. You hold the whole picture
now; the agents building the slices will not. Put what they need into the issues.

## 1. Interview
Ask the user until the goal and the shared pieces are clear. One question at a time.
For each, give your recommended answer and why. Stop when nothing is left to guess.

## 2. Contracts first
List every shared concept: types, design tokens, file names, component ownership,
registries. If two issues would add to the same concept, first file a small
**contract issue** that defines it. The implementers depend on it and **use** it.
They never redefine it.

## 3. Dependencies
For every pair, ask: does B need A's *code*, or only its *concept*? Concept only means
no dependency; the contract issue already covers it. Add `--depends N` only when B
needs A's code to exist. Fewer dependencies means more work in parallel.

## 4. Size each issue
- **S**: a few lines, one or two files, no design choices.
- **M**: a feature that needs a short plan.
- **L**: several parts that each need a subagent. It must have Subtasks.

Bigger than L? Split it.

## 5. Write every section
The template (`nsh issue new` builds it for you):

```markdown
## Size
S | M | L

## Goal
What and why.

## Decisions
- Every name, path and value the agent would otherwise guess.

## Contract
Owns: what this issue creates.
Uses: what it must import and never redefine.

## Out of scope
- Things a reader might wrongly assume are included.

## Acceptance
- Each item has a command to run or something you can observe.

## Touches
- Every EXISTING file this issue will change (a directory is fine).

## Subtasks
(L only) One block per subtask: files, names, how to verify.

Depends-on: #N
```

New files do not need to be listed in Touches. Two issues whose Touches overlap never
run at the same time, so keep Touches tight. An agent that changes a file not listed
is stopped.

## 6. Labels
- `--review`: the change is cross-cutting, visual, risky, or sets a contract others
  depend on. The PR then waits for babysit.
- `--visual`: someone must look at the output.
- Neither: the PR merges itself after the check and the AI review pass.

## 7. Shared instructions
Test-first habits, registry files, render checks go in the target repo's `AGENTS.md`.
Never paste them into each issue.

## 8. File them
Show the batch (title, size, touches, depends, labels) and ask "Happy with these?"
On yes, file each with `nsh issue new`, parents first so you know their numbers:

```bash
nsh issue new --title "Add Foo type" --size S --goal "Define Foo." \
  --acceptance "npm test passes" --touches src/types.ts --owns Foo
nsh issue new --title "Foo list page" --size M --goal "..." \
  --acceptance "..." --touches src/App.tsx --uses Foo --depends 12 --review
```

Repeat `--acceptance`, `--touches`, `--decision`, `--owns`, `--uses`,
`--out-of-scope` and `--depends` per item. For L issues add `--subtasks-file F`. To file
a complete body you wrote yourself use `--body-file F`. Add `--repo <path>` outside the
repo. The command lints the issue first and files nothing if it fails.

**Never create issues with raw `gh issue create`.** Change labels later with
`nsh issue set <n> ready|review|no-review|visual|no-visual|hold`.
