# Nightshift v2 Phase 5: issue tooling, skills, docs

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Issues are created and relabelled only through deterministic commands, the slicer writes v2 issues (sized, contracts first, Touches, review judgement), babysit works off the new toolkit, and the docs describe v2.

**Design:** `docs/superpowers/specs/2026-10-11-nightshift-v2-design.md` ("The issue template", "The deterministic toolkit", Step 6). Field notes #6, #20, #21, #23, #25, #26.

## Rules for every task

Same as Phase 3/4 (branch `v2-phase5`; `.venv/Scripts/python`; test-first for code;
gate `.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m nightshift._gate`
→ CHECK PASSED at 100% branch coverage; `gh` always faked in tests; trailer lines; don't push).

---

### Task 1: `nsh issue new` and `nsh issue set`

**Files:** `src/nightshift/issues.py` (new), `src/nightshift/cli.py` (`issue` sub-app), tests `tests/test_issues.py`.

`issues.py`:

```python
TEMPLATE_ORDER = ("Size", "Goal", "Decisions", "Contract", "Out of scope", "Acceptance", "Touches", "Subtasks")


def render_issue(*, size, goal, acceptance, touches, decisions=(), owns=(), uses=(),
                 out_of_scope=(), subtasks="", depends=()) -> str:
    """The v2 issue body. Empty optional sections are left out."""
    # Size: "S"/"M"/"L"; lists render as "- item"; Contract renders
    # "Owns: a, b" / "Uses: c" lines; depends renders a final "Depends-on: #1, #2" line.
```

`create_issue(gh, *, title, body, review=False, visual=False) -> str`: build a `Slice`
from the body, run `lint()`; if problems → raise `ValueError("issue not filed:\n- ...")`;
else `gh label create <l> --force` for each label used, then
`gh issue create --title T --body B --label nightshift:ready[,review][,visual]`; return the URL.

`set_issue(gh, number, action)` with actions:
- `ready` → the source's `set_status(id, "ready")` (removes other nightshift labels)
- `review` / `no-review` → `gh issue edit N --add-label review` / `--remove-label review`
- `visual` / `no-visual` → same for `visual`
- `hold` → `gh issue edit N --remove-label nightshift:ready` (taken out of the queue without being blocked)
Unknown action → `ValueError` listing the valid ones.

CLI:
- `nsh issue new --title T --size S|M|L --goal G --acceptance A (repeat) --touches P (repeat) [--decision D] [--owns X] [--uses X] [--out-of-scope X] [--subtasks-file F] [--depends N] [--review] [--visual] [--body-file F]`.
  `--body-file` takes a complete v2 markdown body instead of the section flags (still linted).
  Prints the issue URL. Lint failure → message and exit 1.
- `nsh issue set <number> <action>`; prints what changed.

Tests: `render_issue` full and minimal; lint failure refuses without calling `gh issue create`;
labels; every `set_issue` action and the unknown action; CLI wiring with `--body-file`.

Commit: `nsh issue new and nsh issue set: issues are filed and relabelled deterministically`

---

### Task 2: `nightshift-slice` skill for v2

**File:** `skills/nightshift-slice/SKILL.md` (rewrite). Keep the frontmatter `name`; update the description to mention sizes and `nsh issue new`.

Content it must cover, in this order, plain and short:
1. **Interview** the user until the goal and the shared pieces are clear (grill-me style, one question at a time, recommend an answer).
2. **Contracts first** (field notes #6, #23, #25, #26): list every shared concept (types, tokens, file names, component ownership, registries). If two issues would add to the same concept, first file a small contract issue that defines it; the implementers depend on it and **use** it.
3. **Dependencies:** ask "does B need A's *code*, or only its *concept*?" Concept only → no dependency; the contract issue covers it (field note #20).
4. **Size each issue:** S = a few lines, one file or two, no design choices; M = a feature with a short plan; L = several parts that each need a subagent → must have `## Subtasks`. If an issue is bigger than L, split it.
5. **Write every section** of the template (show it, from the design doc): Decisions settle every name, path and value an agent would otherwise guess; Contract says what it Owns and what it Uses; Out of scope; Acceptance items each have a command or observable result; Touches lists every existing file it will change (directories allowed); new files need not be listed.
6. **Review label:** add `--review` when the change is cross-cutting, visual, risky, or sets a contract others depend on. Add `--visual` when output must be looked at. Otherwise leave it off: the PR merges itself after the check and AI review pass.
7. **Shared instructions** (test-first, registries, render checks) go in the target repo's `AGENTS.md`, never pasted into each issue (field note #28).
8. **Confirm** the batch with the user ("Happy with these?"), then file each with `nsh issue new`, parents first. Never create issues with raw `gh issue create`.

Commit: `Skill: nightshift-slice writes sized, contracts-first v2 issues`

---

### Task 3: `nightshift-babysit` skill for v2

**File:** `skills/nightshift-babysit/SKILL.md` (rewrite).

Must cover:
1. Start: `nsh status`, then `nsh wait --repo <repo>` as a background task; handle each event and re-run it (keep the event table from Phase 1/2, plus `needs_detail`, `question`, `main_red`).
2. `review_ready`: only PRs from issues labelled `review` (or whose auto-merge failed) wait for you. `nsh pr checkout <n>` → verify (run it; look at visual output) → fix small problems on the branch, run the check, push → `nsh pr comment <n> "<what you verified>"` → `nsh pr merge <n>`. Bigger problems → merge and file a follow-up with `nsh issue new`, or close the PR and fix the issue text.
3. `question`: read it on the issue; answer with `nsh answer <id> "<answer>"`. If it's really a slicing gap, also fix the issue's Decisions/Contract.
4. `needs_detail`: fix the issue body (add the missing sections), then `nsh issue set <n> ready`.
5. `blocked`: read the reason; fix the issue or the repo, then `nsh issue set <n> ready` or `nsh resume <id>`.
6. `paused_limit` / `resumed`: nothing to do. `stalled`: find the blocked dependency. `main_red`: fix main first. `daemon_exit`: restart `nsh daemon`.
7. Rules: never edit nightshift labels by hand (use `nsh issue set`); never resolve conflicts on Nightshift's PRs yourself (it rebases its own); keep fixes inside the issue's intent.

Commit: `Skill: nightshift-babysit runs on nsh wait, nsh pr and nsh answer`

---

### Task 4: docs

**Files:** `SPEC.md`, `README.md`, `examples/config.yaml`, `docs/field-notes/2026-10-08-motioncraft.md`.

- `SPEC.md`: add a short note at the top: "v2 (2026-10-11) supersedes parts of this spec; see `docs/superpowers/specs/2026-10-11-nightshift-v2-design.md`", and update the sections that are now wrong: §3 slice format → the v2 issue template; §4 → GitHub-only for now (`local-md` basic); §5 → Touches lock by path prefix, critical path first, worker pool; §6 → sizes, `result.json`, conflict ladder; §9 → `review` label decides auto-merge vs babysit. Keep it concise; don't duplicate the design doc.
- `README.md`: the loop diagram and command list for v2 (`nsh issue new/set`, `nsh answer`, `nsh pr ...`); a short "What happens when you hit your usage limit" paragraph.
- `examples/config.yaml`: `install`, `registries`, `models`, `main_gate` with comments.
- Field notes: a short "Status after v2 (2026-10-11)" section at the end listing which numbered notes are addressed and which remain open (open: #16 UTF-8 scan, #10 screenshots on PR comments, #3 is covered by the pool, etc. — check each against what was built).

Commit: `Docs: v2 spec notes, README, example config, field-notes status`
