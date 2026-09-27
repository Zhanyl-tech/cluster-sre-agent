"""Command line interface."""

from __future__ import annotations

import os
import sys

import click

from csa import __version__
from csa.graph import Component as C
from csa.graph import Confidence, DependencyGraph, Edge, Propagation, Refutation
from csa.mcp.readonly import (
    ALLOWED,
    ToolDenied,
    guard,
    resolve_executable,
    trusted_dirs,
)

GRAPH = DependencyGraph()

_COLOUR = {"measured": "green", "documented": "cyan", "inferred": "yellow"}


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
    """Rank the components that could explain a symptom, and those ruled out."""
    try:
        target = C(symptom_at)
    except ValueError:
        raise click.ClickException(
            f"unknown component {symptom_at!r}; one of: " + ", ".join(c.value for c in C)
        ) from None

    level = Propagation(severity)
    found = GRAPH.causes_of(target, severity=level)
    click.echo(f"\n  symptom: {target.value} ({severity})\n")
    if not found:
        click.secho("  no component in the graph can produce that symptom", fg="yellow")
    for h in found:
        click.echo(
            f"  {_mark(h.weakest)} {h.cause.value:<24} {h.hops} hop(s)  "
            f"{' → '.join(p.value for p in h.path)}"
        )
        # A hypothesis that exists only through a failure mode nobody
        # measured says so where it is offered, not in a footnote.
        modes = [e.evidence.untested_mode for e in h.untested_modes if e.evidence.untested_mode]
        for mode in modes:
            click.echo(f"{'':13}only through an untested failure mode: {mode.description}")

    refuted = GRAPH.refutations_for(target, severity=level)
    measured = [r for r in refuted if r.by_measurement]
    weaker = [r for r in refuted if not r.by_measurement]
    if measured:
        click.echo(f"\n  ruled out at {severity} by measurement, within this graph:")
        for r in measured:
            click.echo(f"    {r.component.value:<24} {_limit(r)}")
    if weaker:
        click.echo(f"\n  ruled out at {severity} by documentation or inference only:")
        for r in weaker:
            click.echo(f"    {_mark(r.confidence)} {r.component.value:<24} {_limit(r)}")

    # Every measured refutation stops where its measurements stopped. It rests
    # on the cap of every path from the component, not only the limiting one,
    # so the caveats of all of them are printed.
    limits: dict[Edge, None] = {cap: None for r in measured for cap in r.caps}
    caveats = [(e, e.evidence.untested) for e in limits if e.evidence.untested]
    if caveats:
        click.echo("\n  not covered by those measurements:")
        for edge, untested in caveats:
            click.echo(f"    {edge.source.value} → {edge.target.value}:")
            for item in untested:
                click.echo(f"      - {item}")
    click.echo()


def _mark(confidence: Confidence) -> str:
    return click.style(f"{confidence.value:<10}", fg=_COLOUR[confidence.value])


def _limit(r: Refutation) -> str:
    edge = r.limiting_edge
    effect = (
        "does not propagate"
        if edge.propagation is Propagation.NONE
        else f"only {edge.propagation.value}"
    )
    return f"{edge.source.value} → {edge.target.value} {effect}"


@main.command()
def stats() -> None:
    """Count the graph's edges by evidence, computed from csa.graph.EDGES."""
    counts = GRAPH.evidence_counts
    total = len(GRAPH.edges)
    measured = counts[Confidence.MEASURED]
    click.echo(f"  {total} edges, {measured} measured ({round(100 * GRAPH.measured_fraction)}%)")
    click.echo(
        f"  documented {counts[Confidence.DOCUMENTED]}, inferred {counts[Confidence.INFERRED]}"
    )


@main.command()
def tools() -> None:
    """Show the read-only tool surface the agent is limited to."""
    click.echo()
    for tool in sorted(ALLOWED.values(), key=lambda t: t.binary):
        click.echo(f"  {tool.binary:<12} {tool.description}")
        if tool.verbs is not None:
            click.echo(f"               subcommands: {', '.join(tool.verbs)}")
        for what, why in tool.excluded.items():
            click.echo(f"               {click.style('excluded:', fg='red')} {what}: {why}")
    click.echo(f"\n  Anything not on these lists is refused. Binaries run from: {_where()}\n")


def _where() -> str:
    try:
        return os.pathsep.join(str(d) for d in trusted_dirs())
    except ToolDenied as exc:
        return f"(misconfigured: {exc})"


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
    try:
        executable = resolve_executable(command[0])
    except ToolDenied as exc:
        # Permitted in shape, but the binary that would run is not trusted.
        click.echo(f"          but it would not run: {exc}")
        sys.exit(1)
    if executable is None:
        click.echo(f"          (not installed in the trusted directories: {_where()})")
    else:
        click.echo(f"          would run {executable}")


if __name__ == "__main__":
    sys.exit(main())
