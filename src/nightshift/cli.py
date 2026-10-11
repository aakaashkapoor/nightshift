"""Nightshift command-line interface."""

from pathlib import Path

import typer

from nightshift import __version__

app = typer.Typer(
    help="Nightshift — you slice the work; your machine ships it overnight.",
    add_completion=False,
    no_args_is_help=True,
)


pr_app = typer.Typer(
    help="Babysit helpers: check out, merge and comment on a PR.", no_args_is_help=True
)
app.add_typer(pr_app, name="pr")

issue_app = typer.Typer(help="File and relabel issues deterministically.", no_args_is_help=True)
app.add_typer(issue_app, name="issue")


@app.callback()
def main() -> None:
    """Nightshift CLI."""


@app.command()
def version() -> None:
    """Print the Nightshift version."""
    typer.echo(f"nightshift {__version__}")


@app.command()
def run(
    slice: str = typer.Argument(..., help="Slice id, or a path to a slice .md file"),
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(
        None, "--config", help="Config file (default: ~/.nightshift/config.yaml)"
    ),
) -> None:
    """Run one slice: Work -> check -> one clean commit."""
    from nightshift.pipeline import run_slice_cli

    result = run_slice_cli(slice, repo=repo, config_path=config)
    commit = f" ({result.commit[:8]})" if result.commit else ""
    typer.echo(
        f"{result.status.upper()} {result.slice_id} [{result.branch}] — {result.detail}{commit}"
    )
    raise typer.Exit(code=0 if result.status == "done" else 1)


@app.command()
def init(
    repo: Path = typer.Option(Path("."), "--repo", help="Repo to register (default: current dir)"),
    config: Path | None = typer.Option(
        None, "--config", help="Config file (default: ~/.nightshift/config.yaml)"
    ),
    name: str | None = typer.Option(None, "--name", help="Repo name in the config"),
    check: str | None = typer.Option(
        None, "--check", help="Verification command (auto-detected if omitted)"
    ),
    source: str = typer.Option("local-md", "--source", help="local-md | github-issues"),
    symlink: str | None = typer.Option(
        None,
        "--symlink",
        help="Comma-separated dirs to link into each worktree (e.g. node_modules)",
    ),
    push: bool = typer.Option(
        False, "--push/--no-push", help="Push base to origin after a successful merge"
    ),
    install: str | None = typer.Option(
        None, "--install", help="Per-worktree dependency install (detected from the lockfile)"
    ),
    sync: str | None = typer.Option(
        None, "--sync", help="Post-merge sync in the main checkout (detected from the lockfile)"
    ),
) -> None:
    """Register a repo in the Nightshift config (auto-detects the check)."""
    from nightshift.config import register_repo
    from nightshift.detect import detect_check, detect_install, detect_sync

    repo = repo.resolve()
    name = name or repo.name
    check = check or detect_check(repo) or "echo 'TODO: set your check command'"
    install = install or detect_install(repo)
    sync = sync or detect_sync(repo)
    symlink_dirs = [s.strip() for s in symlink.split(",")] if symlink else None
    target = register_repo(
        config,
        name=name,
        path=repo,
        check=check,
        source=source,
        symlink_dirs=symlink_dirs,
        push=push,
        sync=sync,
        install=install,
    )
    lines = [f"registered '{name}' -> {repo}", f"  check:  {check}"]
    if install:
        lines.append(f"  install: {install}")
    if sync:
        lines.append(f"  sync:    {sync}")
    lines += [f"  source: {source}", f"  config: {target}"]
    typer.echo("\n".join(lines))


@app.command()
def resume(
    slice: str = typer.Argument(..., help="Blocked slice id to resume"),
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(
        None, "--config", help="Config file (default: ~/.nightshift/config.yaml)"
    ),
) -> None:
    """Resume a blocked slice on its preserved worktree (never from scratch)."""
    from nightshift.pipeline import run_resume_cli

    result = run_resume_cli(slice, repo=repo, config_path=config)
    commit = f" ({result.commit[:8]})" if result.commit else ""
    typer.echo(
        f"{result.status.upper()} {result.slice_id} [{result.branch}] — {result.detail}{commit}"
    )
    raise typer.Exit(code=0 if result.status == "done" else 1)


@app.command()
def daemon(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(
        None, "--config", help="Config file (default: ~/.nightshift/config.yaml)"
    ),
    once: bool = typer.Option(False, "--once", help="Run a single tick and exit"),
    interval: float = typer.Option(30, "--interval", help="Poll interval in seconds"),
    log_file: Path | None = typer.Option(
        None, "--log-file", help="Also append timestamped logs to this file"
    ),
    child: bool = typer.Option(False, "--child", hidden=True),
) -> None:
    """Run the Nightshift daemon: drain ready slices (Work -> check -> commit)."""
    from nightshift import supervisor
    from nightshift.daemon import configure_logging, run_daemon_cli

    if not once and not child:
        raise typer.Exit(
            code=supervisor.run_supervised(
                repo, config_path=config, interval=interval, log_file=log_file
            )
        )
    configure_logging(log_file)
    results = run_daemon_cli(repo, config_path=config, once=once, interval=interval, child=child)
    for r in results:
        commit = f" ({r.commit[:8]})" if r.commit else ""
        typer.echo(f"{r.status.upper()} {r.slice_id} [{r.branch}] — {r.detail}{commit}")
    if not results:
        typer.echo("no runnable slices")


@app.command()
def status(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(
        None, "--config", help="Config file (default: ~/.nightshift/config.yaml)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the raw state as JSON"),
) -> None:
    """Show what the daemon is doing: phase and age of every slice, pause state."""
    from nightshift import wait as wait_mod

    typer.echo(wait_mod.run_status_cli(repo, config_path=config, as_json=as_json))


@app.command()
def wait(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(
        None, "--config", help="Config file (default: ~/.nightshift/config.yaml)"
    ),
    timeout: float | None = typer.Option(None, "--timeout", help="Give up after N seconds"),
    poll: float = typer.Option(2.0, "--poll", help="Seconds between checks"),
) -> None:
    """Block until something needs the driving agent; print it as JSON lines."""
    import json

    from nightshift import wait as wait_mod

    events = wait_mod.run_wait_cli(repo, config_path=config, timeout=timeout, poll=poll)
    for event in events:
        typer.echo(json.dumps(event))
    if not events:
        typer.echo(json.dumps({"kind": "timeout"}))
        raise typer.Exit(code=2)


@app.command()
def answer(
    slice: str = typer.Argument(..., help="Slice id the agent asked about"),
    text: str = typer.Argument(..., help="Your answer"),
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(None, "--config", help="Config file"),
) -> None:
    """Answer an agent's question; the slice resumes its session on the next poll."""
    from nightshift import wait as wait_mod

    try:
        typer.echo(wait_mod.run_answer_cli(repo, slice, text, config_path=config))
    except LookupError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc


def _control(action: str, repo: Path, config: Path | None) -> None:
    from nightshift import wait as wait_mod

    typer.echo(wait_mod.run_control_cli(repo, action, config_path=config))


@app.command()
def pause(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(None, "--config", help="Config file"),
) -> None:
    """Stop starting new slices (running ones finish). Undo with `nsh unpause`."""
    _control("pause", repo, config)


@app.command()
def unpause(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(None, "--config", help="Config file"),
) -> None:
    """Let the daemon start new slices again."""
    _control("unpause", repo, config)


@app.command()
def stop(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(None, "--config", help="Config file"),
) -> None:
    """Exit the daemon once the running slices finish."""
    _control("stop", repo, config)


def _pr_step(step) -> None:
    """Run one `nsh pr` step; any failure is one clear line and a non-zero exit."""
    from nightshift.prtools import PrError

    try:
        typer.echo(step())
    except (PrError, RuntimeError) as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(code=1) from exc


@pr_app.command("checkout")
def pr_checkout(
    number: int = typer.Argument(..., help="PR number"),
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
    config: Path | None = typer.Option(None, "--config", help="Config file"),
) -> None:
    """Check the PR out into a sibling worktree, install dependencies, print its path."""
    from nightshift import prtools

    _pr_step(lambda: prtools.checkout_cli(number, repo, config_path=config))


@pr_app.command("merge")
def pr_merge(
    number: int = typer.Argument(..., help="PR number"),
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
) -> None:
    """Squash-merge the PR once GitHub says it is mergeable; remove its worktree."""
    from nightshift import prtools

    _pr_step(lambda: prtools.merge_cli(number, repo))


@pr_app.command("comment")
def pr_comment(
    number: int = typer.Argument(..., help="PR number"),
    text: str = typer.Argument(..., help="What was verified"),
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
) -> None:
    """Comment on the PR with what was verified."""
    from nightshift import prtools

    _pr_step(lambda: prtools.comment_cli(number, text, repo))


def _issue_step(step) -> None:
    """Run one `nsh issue` step; any failure is one clear line and a non-zero exit."""
    try:
        typer.echo(step())
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(code=1) from exc


@issue_app.command("new")
def issue_new(
    title: str = typer.Option(..., "--title", help="Issue title"),
    size: str | None = typer.Option(None, "--size", help="S | M | L"),
    goal: str | None = typer.Option(None, "--goal", help="What and why"),
    acceptance: list[str] = typer.Option([], "--acceptance", help="Repeat per item"),
    touches: list[str] = typer.Option([], "--touches", help="Existing file or dir; repeat"),
    decision: list[str] = typer.Option([], "--decision", help="Repeat per decision"),
    owns: list[str] = typer.Option([], "--owns", help="What this issue creates; repeat"),
    uses: list[str] = typer.Option([], "--uses", help="What it must import, not redefine"),
    out_of_scope: list[str] = typer.Option([], "--out-of-scope", help="Repeat per item"),
    subtasks_file: Path | None = typer.Option(None, "--subtasks-file", help="Subtasks markdown"),
    depends: list[str] = typer.Option([], "--depends", help="Issue number; repeat"),
    review: bool = typer.Option(False, "--review", help="Wait for babysit instead of auto-merge"),
    visual: bool = typer.Option(False, "--visual", help="Output must be looked at"),
    body_file: Path | None = typer.Option(None, "--body-file", help="A complete v2 body"),
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
) -> None:
    """File a linted v2 issue labelled nightshift:ready; prints its URL."""
    from nightshift import issues

    def step() -> str:
        if body_file is not None:
            body = body_file.read_text(encoding="utf-8")
        elif size is None or goal is None:
            raise ValueError("give --size and --goal (and --acceptance/--touches), or --body-file")
        else:
            body = issues.render_issue(
                size=size.upper(),
                goal=goal,
                acceptance=acceptance,
                touches=touches,
                decisions=decision,
                owns=owns,
                uses=uses,
                out_of_scope=out_of_scope,
                subtasks=subtasks_file.read_text(encoding="utf-8") if subtasks_file else "",
                depends=depends,
            )
        return issues.new_cli(repo, title=title, body=body, review=review, visual=visual)

    _issue_step(step)


@issue_app.command("set")
def issue_set(
    number: int = typer.Argument(..., help="Issue number"),
    action: str = typer.Argument(
        ..., help="ready | review | no-review | visual | no-visual | hold"
    ),
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo (default: current dir)"),
) -> None:
    """Change an issue's labels; the only supported way."""
    from nightshift import issues

    _issue_step(lambda: issues.set_cli(repo, number, action))
