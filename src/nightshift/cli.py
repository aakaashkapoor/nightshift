"""Nightshift command-line interface."""

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
