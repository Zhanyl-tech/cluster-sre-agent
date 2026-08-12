"""csa — multi-agent Slurm cluster diagnosis, measured against slurm-rca-bench.

The dependency graph in :mod:`csa.graph` is the hypothesis under test; the
read-only tool surface in :mod:`csa.mcp.readonly` is what keeps an agent from
acting on it.
"""

from __future__ import annotations

from csa.graph import EDGES, Component, Confidence, DependencyGraph, Edge, Propagation
from csa.mcp.readonly import ALLOWED, ToolDenied, guard, run, tool_manifest

__version__ = "0.1.0"

__all__ = [
    "ALLOWED",
    "EDGES",
    "Component",
    "Confidence",
    "DependencyGraph",
    "Edge",
    "Propagation",
    "ToolDenied",
    "__version__",
    "guard",
    "run",
    "tool_manifest",
]
