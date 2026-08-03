# Adapters — extending Nightshift

Every optional capability is an **adapter behind a small interface**; config names
which to load. Built-ins ship in the repo; add a new one by implementing the
interface and referencing it in config — **without touching the core daemon**. This
is the contribution path (Teams, Linear, Discord, GitLab, Gerrit, …).

| Interface | Module | Built-in at launch | Contribute |
|---|---|---|---|
| `SourceAdapter` | `source.py`, `github.py` | `local-md`, `github-issues` | Linear, Jira-as-source |
| `Executor` | `executor.py` | `local-headless` | (future) remote box |
| `Reviewer` | `review.py` | `AgentReviewer`; `github-pr` external | GitLab MR, Gerrit, MS internal |
| `Notifier` | `notifier.py` | `none`, `slack` | Teams, Discord, email |
| `Tracker` | `tracker.py` | `none` | Jira, Linear |

## The contracts

**Source** — discover and persist slice state:
```python
list_all() -> list[Slice]
list_ready() -> list[Slice]
get(slice_id) -> Slice | None
set_status(slice_id, status) -> None
set_blocked(slice_id, reason) -> None
```

**Notifier** — optional out-of-band alerts (never required):
```python
notify(event: str, message: str) -> None
```
See `SlackNotifier` for the pattern: take config (a webhook URL), keep the network
call behind an injectable `poster` so it's unit-testable. Register it in
`build_notifier`.

**Tracker** — optional external issue-tracker sync:
```python
on_start(slice_id) -> None
on_done(slice_id) -> None
on_blocked(slice_id, reason) -> None
```

**Reviewer** — the review gate:
```python
review(worktree_path, slice) -> ReviewResult   # findings: blocking | advisory
```

**Executor** — runs a headless agent; the injectable `runner(argv, cwd, stdin)` seam
is what makes every agent path unit-testable without spending tokens.

## How to add an adapter
1. Implement the interface in a new module.
2. Add it to the relevant `build_*` factory (keyed by a `type` in config).
3. Add a unit test with an **injected** transport (fake `poster`/`gh`/`runner`) —
   never hit the network in tests.
4. Reference it in `~/.nightshift/config.yaml` (e.g. `notifier: {type: slack, webhook_url: ...}`).

Secrets (webhook URLs, tokens) live in `~/.nightshift/secrets.env` and are referenced
as `${VAR}` — never commit them.
