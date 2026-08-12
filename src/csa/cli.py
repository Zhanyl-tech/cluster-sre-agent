"""Command line interface."""

from __future__ import annotations

import sys

import click

from csa import __version__
from csa.graph import Component, DependencyGraph, Propagation
from csa.mcp.readonly import ToolDenied, guard, tool_manifest

GRAPH = DependencyGraph()


@click.group()
@click.version_option(__version__)
def main() -> None:
    """csa — Slurm cluster diagnosis, measured rather than asserted."""


@main.command()
@click.argument("symptom_at")
@click.option(
    "--severity",
    type=click.Choice(["halts", "degrades"]),
    default="halts",
    show_default=True,
    help="What you actually observed. Severity composes along the path.",
)
def causes(symptom_at: str, severity: str) -> None:
    """Rank the components that could explain a symptom."""
    try:
        target = Component(symptom_at)
    except ValueError:
        raise click.ClickException(
            f"unknown component {symptom_at!r}; one of: " + ", ".join(c.value for c in Component)
        ) from None

    found = GRAPH.causes_of(target, severity=Propagation(severity))
    click.echo(f"\n  symptom: {target.value} ({severity})\n")
    if not found:
        click.secho("  no component in the graph can produce that symptom", fg="yellow")
    for h in found:
        colour = {"measured": "green", "documented": "cyan", "inferred": "yellow"}[h.weakest.value]
        mark = click.style(f"{h.weakest.value:<10}", fg=colour)
        click.echo(
            f"  {mark} {h.cause.value:<24} {h.hops} hop(s)  {' → '.join(p.value for p in h.path)}"
        )

    refuted = GRAPH.refutations_for(target)
    if refuted:
        click.echo("\n  ruled out by measurement:")
        for edge in refuted:
            click.echo(f"    {edge.source.value:<24} {edge.symptom}")
    click.echo()


@main.command()
def tools() -> None:
    """Show the read-only tool surface the agent is limited to."""
    click.echo()
    for entry in tool_manifest():
        click.echo(f"  {entry['name']!s:<12} {entry['description']!s}")
        forbidden = entry["forbidden"]
        if isinstance(forbidden, list) and forbidden:
            denied = click.style("denied: ", fg="red")
            click.echo(f"               {denied}{', '.join(str(f) for f in forbidden)}")
    click.echo()


# ignore_unknown_options: the whole point is to pass a real command through
# verbatim, and `csa check sinfo -N` must not have -N eaten as a click flag.
@main.command(name="check", context_settings={"ignore_unknown_options": True})
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
def check_command(command: tuple[str, ...]) -> None:
    """Ask whether a command would be permitted. Never executes it."""
    try:
        guard(list(command))
    except ToolDenied as exc:
        click.secho(f"  DENIED  {' '.join(command)}", fg="red")
        click.echo(f"          {exc}")
        sys.exit(1)
    click.secho(f"  ALLOWED {' '.join(command)}", fg="green")


if __name__ == "__main__":
    sys.exit(main())
