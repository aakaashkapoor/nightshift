"""Nightshift command-line interface."""

from pathlib import Path
from typing import Optional

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
    config: Optional[Path] = typer.Option(
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
