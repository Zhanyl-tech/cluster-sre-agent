"""csa — groundwork for a multi-agent Slurm diagnosis ablation. No agent yet.

What exists: the dependency graph in :mod:`csa.graph`, which is the hypothesis
the ablation is to test, and the read-only tool guard in
:mod:`csa.mcp.readonly`, which is what will keep an agent from acting. No
agent exists and no diagnosis accuracy has been measured against
slurm-rca-bench; the README's results table is empty.
"""

from __future__ import annotations

from typing import Final

from csa.graph import (
    EDGES,
    Component,
    Confidence,
    DependencyGraph,
    Edge,
    Hypothesis,
    Propagation,
    Refutation,
)
from csa.mcp.readonly import (
    ALLOWED,
    ToolDenied,
    UntrustedExecutable,
    guard,
    resolve_executable,
    run,
    tool_manifest,
    trusted_dirs,
)

__version__ = "0.2.0"

#: The model every ablation configuration is to run, pinned on 2026-08-08
#: (commit 0deea7c) so that configurations differ in architecture only. Pinned
#: for reproducibility, not because it is the most capable model available. No
#: code calls a model yet; results, when they exist, must record this ID.
MODEL: Final = "claude-opus-5"

__all__ = [
    "ALLOWED",
    "EDGES",
    "MODEL",
    "Component",
    "Confidence",
    "DependencyGraph",
    "Edge",
    "Hypothesis",
    "Propagation",
    "Refutation",
    "ToolDenied",
    "UntrustedExecutable",
    "__version__",
    "guard",
    "resolve_executable",
    "run",
    "tool_manifest",
    "trusted_dirs",
]
