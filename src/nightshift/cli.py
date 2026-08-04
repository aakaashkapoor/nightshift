"""Nightshift command-line interface."""

from pathlib import Path

import typer

from nightshift import __version__

app = typer.Typer(
    help="Nightshift — you slice the work; your machine ships it overnight.",
    add_completion=False,
    no_args_is_help=True,
)


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
) -> None:
    """Register a repo in the Nightshift config (auto-detects the check)."""
    from nightshift.config import register_repo
    from nightshift.detect import detect_check

    repo = repo.resolve()
    name = name or repo.name
    check = check or detect_check(repo) or "echo 'TODO: set your check command'"
    symlink_dirs = [s.strip() for s in symlink.split(",")] if symlink else None
    target = register_repo(
        config,
        name=name,
        path=repo,
        check=check,
        source=source,
        symlink_dirs=symlink_dirs,
        push=push,
    )
    typer.echo(
        f"registered '{name}' -> {repo}\n  check:  {check}\n  source: {source}\n  config: {target}"
    )


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
) -> None:
    """Run the Nightshift daemon: drain ready slices (Work -> check -> commit)."""
    from nightshift.daemon import configure_logging, run_daemon_cli

    configure_logging(log_file)
    results = run_daemon_cli(repo, config_path=config, once=once, interval=interval)
    for r in results:
        commit = f" ({r.commit[:8]})" if r.commit else ""
        typer.echo(f"{r.status.upper()} {r.slice_id} [{r.branch}] — {r.detail}{commit}")
    if not results:
        typer.echo("no runnable slices")
