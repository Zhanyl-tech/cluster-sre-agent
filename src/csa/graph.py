"""The Slurm control-plane dependency graph.

This file is the hypothesis. Everything else in the repository exists to find
out whether writing it down is worth anything.

The claim under test is that a large share of what general RCA agents lose is
lost to *graph inference* — cloud topologies are huge, dynamic and undocumented,
so an agent must work out the shape of the system and diagnose the fault at the
same time. A Slurm control plane is the opposite: a dozen components, static
between deploys, and identical at every site on earth. So it can simply be
written down.

Three properties make this file worth more than a diagram.

**Edges record whether a failure actually propagates, not just that a
dependency exists.** ``slurmctld`` depends on ``slurmdbd``, and every
architecture diagram draws that arrow — but a stalled accounting path does
*not* stop scheduling. Measured, not assumed: with the database suspended,
jobs continued to submit, start and complete. An agent handed a dependency
graph without propagation semantics will follow that arrow to a confident wrong
answer, which is exactly what the folk model does.

**The graph is measured where it can be, and marked where it cannot.** Every
edge carries a :class:`Evidence` record saying whether the behaviour was
observed on a live cluster or inferred from documentation. An unverified edge
is still usable, but it is never presented to the agent as though it were
established.

**It is validated against the benchmark's ground truth.** ``tests/`` asserts
that every causal chain in slurm-rca-bench is reproducible by traversing this
graph, and that no edge contradicts a measured scenario. If the benchmark and
the graph ever disagree, the build fails and one of them is wrong. That is the
only honest way to hold a hand-written graph to account.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum


class Component(StrEnum):
    """Components of a Slurm control plane.

    Deliberately identical to the answer vocabulary in slurm-rca-bench, so a
    hypothesis produced by traversing this graph can be scored without any
    translation layer. A mapping step between the graph and the benchmark would
    be a place for quiet disagreement to hide.
    """

    SHARED_FS = "storage.shared_fs"
    STATE_SAVE = "storage.state_save"
    MYSQL = "db.mysql"
    SLURMDBD = "slurm.slurmdbd"
    SLURMCTLD = "slurm.slurmctld"
    SCHEDULER = "slurm.scheduler"
    SLURMD = "slurm.slurmd"
    CONFIG = "slurm.config"
    GPU_DEVICE = "gpu.device"
    GPU_DRIVER = "gpu.driver"
    FABRIC = "fabric.interconnect"
    CONTROL_NET = "network.control_plane"


class Propagation(StrEnum):
    """What happens downstream when the source component fails."""

    #: The downstream component stops working.
    HALTS = "halts"
    #: The downstream component keeps working, but worse or slower.
    DEGRADES = "degrades"
    #: The downstream component is unaffected in any way a user would notice.
    #: The most valuable value in this enum — see BLOCKS_ACCOUNTING_NOT_SCHEDULING.
    NONE = "none"


class Confidence(StrEnum):
    """How the edge came to be believed."""

    #: Observed on a live cluster by this project. See the `note` field.
    MEASURED = "measured"
    #: Stated in Slurm documentation or source, not independently observed.
    DOCUMENTED = "documented"
    #: Reasoned from architecture. Weakest, and flagged as such to the agent.
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class Evidence:
    """Why an edge is believed, and how strongly."""

    confidence: Confidence
    note: str = ""

    @property
    def is_measured(self) -> bool:
        return self.confidence is Confidence.MEASURED


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed dependency, annotated with what failure does to it."""

    source: Component
    target: Component
    #: What happens to ``target`` when ``source`` fails.
    propagation: Propagation
    evidence: Evidence
    #: What an operator would actually see downstream.
    symptom: str = ""
    #: Rough time for the effect to become visible, in seconds. A range rather
    #: than a point: the measured values varied by a factor of four across runs
    #: on identical injections, so a single number would imply a precision the
    #: observations do not support.
    latency_s: tuple[int, int] | None = None

    @property
    def causal(self) -> bool:
        """True when a fault here can explain a symptom there.

        The property the traversal actually uses. An edge with
        ``Propagation.NONE`` is still part of the architecture and still worth
        recording — it is how the graph *refutes* a plausible hypothesis rather
        than merely failing to suggest it.
        """
        return self.propagation is not Propagation.NONE


#: The graph.
#:
#: Ordered roughly upstream-to-downstream for readability. Every edge either
#: cites a measurement or is explicitly marked as documentation or inference.
EDGES: tuple[Edge, ...] = (
    # ── storage ────────────────────────────────────────────────────────────
    Edge(
        source=Component.SHARED_FS,
        target=Component.MYSQL,
        propagation=Propagation.HALTS,
        symptom="queries block indefinitely rather than returning errors",
        latency_s=(0, 60),
        evidence=Evidence(
            Confidence.MEASURED,
            "Emulated by suspending the database process group. Callers block on "
            "an unbounded wait; nothing logs an error, which is what makes a "
            "storage stall harder to find than a storage failure.",
        ),
    ),
    Edge(
        source=Component.STATE_SAVE,
        target=Component.SLURMCTLD,
        propagation=Propagation.DEGRADES,
        symptom="job submission rejected: 'I/O error writing script/environment to file'",
        latency_s=(0, 5),
        evidence=Evidence(
            Confidence.MEASURED,
            "chmod 500 on StateSaveLocation. Submission fails immediately and "
            "loudly; running jobs are untouched and sinfo stays normal. Note "
            "DEGRADES rather than HALTS: the controller keeps serving queries "
            "and managing running work, it just cannot accept new jobs.",
        ),
    ),
    # ── accounting path ────────────────────────────────────────────────────
    Edge(
        source=Component.MYSQL,
        target=Component.SLURMDBD,
        propagation=Propagation.HALTS,
        symptom="slurmdbd stops answering; sacct and sreport hang",
        latency_s=(0, 45),
        evidence=Evidence(
            Confidence.MEASURED,
            "With the database suspended, sacct blocked rather than erroring, "
            "consistently within a few seconds.",
        ),
    ),
    # The edge that matters most, and the one a diagram would draw wrong.
    Edge(
        source=Component.SLURMDBD,
        target=Component.SCHEDULER,
        propagation=Propagation.NONE,
        symptom="none — scheduling continues normally with accounting unavailable",
        evidence=Evidence(
            Confidence.MEASURED,
            "THE REFUTATION. The widely-repeated model is that a storage stall "
            "backs up through the accounting path until the controller's queues "
            "fill and scheduling halts, roughly thirteen minutes later. Measured "
            "on Slurm 25.11.4 with the database suspended for fifteen minutes: "
            "jobs submitted, started and completed throughout, sinfo never "
            "stalled, and the node stayed idle-and-available the entire time. "
            "Slurm degrades the accounting path independently of scheduling. "
            "An agent that follows this arrow reaches a confident wrong answer, "
            "which is precisely why the propagation field exists.",
        ),
    ),
    Edge(
        source=Component.SLURMDBD,
        target=Component.SLURMCTLD,
        propagation=Propagation.DEGRADES,
        symptom="controller logs accounting agent-queue growth; RPC service unaffected",
        latency_s=(60, 900),
        evidence=Evidence(
            Confidence.MEASURED,
            "Agent queue depth grew monotonically while the database was "
            "suspended, but arrival time varied by a factor of four across "
            "identical runs, so the latency here is a wide range on purpose.",
        ),
    ),
    # ── controller and scheduling ──────────────────────────────────────────
    Edge(
        source=Component.SLURMCTLD,
        target=Component.SCHEDULER,
        propagation=Propagation.HALTS,
        symptom="jobs stay PENDING with idle nodes available",
        latency_s=(0, 30),
        evidence=Evidence(
            Confidence.DOCUMENTED,
            "The scheduling loop runs inside slurmctld, so a controller that is "
            "down cannot schedule. Not independently measured here.",
        ),
    ),
    Edge(
        source=Component.CONFIG,
        target=Component.SCHEDULER,
        propagation=Propagation.HALTS,
        symptom="jobs PENDING with a Reason naming a limit (QOSMaxJobs, AssocMaxJobs)",
        latency_s=(0, 30),
        evidence=Evidence(
            Confidence.MEASURED,
            "Measured as a NEGATIVE on the benchmark cluster: with "
            "AccountingStorageEnforce=none, association limits are recorded and "
            "never enforced, so this edge is inactive unless enforcement is on. "
            "A graph that assumed it always applies would mislead an agent on "
            "any cluster with enforcement disabled.",
        ),
    ),
    Edge(
        source=Component.CONTROL_NET,
        target=Component.SLURMCTLD,
        propagation=Propagation.DEGRADES,
        symptom="nodes flap between responding and not responding",
        latency_s=(0, 120),
        evidence=Evidence(Confidence.INFERRED, "Reasoned from architecture; not observed."),
    ),
    # ── compute nodes ──────────────────────────────────────────────────────
    Edge(
        source=Component.SLURMD,
        target=Component.SLURMCTLD,
        propagation=Propagation.DEGRADES,
        symptom="'Node not responding'; the node drains, the partition shrinks",
        latency_s=(0, 120),
        evidence=Evidence(
            Confidence.MEASURED,
            "Killing slurmd uncleanly produced 'not responding' followed by a "
            "drain. Note the operator, not Slurm, is what clears it: after pod "
            "replacement the node stayed down until explicitly resumed.",
        ),
    ),
    Edge(
        source=Component.GPU_DEVICE,
        target=Component.SLURMD,
        propagation=Propagation.DEGRADES,
        symptom="jobs on one node fail with CUDA errors; other nodes are fine",
        latency_s=(0, 60),
        evidence=Evidence(
            Confidence.DOCUMENTED,
            "Per-device ECC and row-remap counters. The discriminator against a "
            "driver fault is that hardware faults are per-device.",
        ),
    ),
    Edge(
        source=Component.GPU_DRIVER,
        target=Component.SLURMD,
        propagation=Propagation.HALTS,
        symptom="every GPU on the node fails at once; health check drains it repeatedly",
        latency_s=(0, 60),
        evidence=Evidence(
            Confidence.DOCUMENTED,
            "Wholesale rather than per-device failure is the signature that "
            "separates a driver fault from a hardware one.",
        ),
    ),
    Edge(
        source=Component.FABRIC,
        target=Component.SLURMD,
        propagation=Propagation.DEGRADES,
        symptom="multi-node jobs slow dramatically; single-node jobs unaffected",
        latency_s=(0, 300),
        evidence=Evidence(
            Confidence.DOCUMENTED,
            "The single-node/multi-node split is the discriminator against a GPU "
            "fault, which would slow both.",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """A candidate root cause, with the path that reaches the symptom."""

    cause: Component
    path: tuple[Component, ...]
    #: True when every edge along the path was measured rather than assumed.
    fully_measured: bool
    #: The weakest evidence anywhere on the path.
    weakest: Confidence
    hops: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "hops", len(self.path) - 1)


class DependencyGraph:
    """Traversal over :data:`EDGES`."""

    def __init__(self, edges: tuple[Edge, ...] = EDGES) -> None:
        self.edges = edges
        self._incoming: dict[Component, list[Edge]] = {}
        for edge in edges:
            self._incoming.setdefault(edge.target, []).append(edge)

    def upstream_of(self, component: Component) -> list[Edge]:
        """Edges whose target is ``component``."""
        return list(self._incoming.get(component, []))

    def causes_of(
        self,
        symptom_at: Component,
        *,
        severity: Propagation = Propagation.HALTS,
        max_hops: int = 5,
    ) -> list[Hypothesis]:
        """Walk upstream from a symptom to components that could explain it.

        **Severity composes along the path, and does not compose transitively.**
        This is the subtlety that makes the graph more than a topology, and it
        is easy to get wrong — the first version of this method did.

        A fault at the far end of a path produces, at the near end, an effect no
        worse than the *weakest* link along it. So the accounting chain

            db.mysql --HALTS--> slurmdbd --DEGRADES--> slurmctld --HALTS--> scheduler

        cannot explain a scheduling *halt*: the middle link only degrades the
        controller, and a degraded controller keeps scheduling. Chaining the two
        HALTS edges around it and concluding "the database stopped scheduling"
        is precisely the folk model this project measured and refuted — and a
        naive breadth-first walk reintroduces it, because the arrows are all
        there. The `min` over the path is what keeps it out.

        The same chain *does* explain a degraded controller, which is true and
        was observed. Ask for the severity you actually saw.

        Results are ordered by evidence strength first, then path length, so a
        measured three-hop explanation outranks an inferred one-hop guess.
        """
        found: dict[Component, Hypothesis] = {}
        # (node, path, weakest-evidence, weakest-propagation-so-far)
        queue: deque[tuple[Component, tuple[Component, ...], Confidence, Propagation]] = deque(
            [(symptom_at, (symptom_at,), Confidence.MEASURED, Propagation.HALTS)]
        )
        # Visited per (component, severity) rather than per component: a node
        # reachable only via a degrading path may still be reachable at full
        # severity by another route.
        seen: set[tuple[Component, Propagation]] = {(symptom_at, Propagation.HALTS)}

        while queue:
            node, path, weakest, carried = queue.popleft()
            if len(path) > max_hops + 1:
                continue
            for edge in self.upstream_of(node):
                if not edge.causal:
                    continue
                effect = _weaker_propagation(carried, edge.propagation)
                if _severity_rank(effect) < _severity_rank(severity):
                    continue  # too weak to produce the symptom that was reported
                key = (edge.source, effect)
                if key in seen or edge.source in path:
                    continue
                seen.add(key)

                new_weakest = _weaker(weakest, edge.evidence.confidence)
                new_path = (edge.source, *path)
                existing = found.get(edge.source)
                if existing is None or len(new_path) < len(existing.path):
                    found[edge.source] = Hypothesis(
                        cause=edge.source,
                        path=new_path,
                        fully_measured=new_weakest is Confidence.MEASURED,
                        weakest=new_weakest,
                    )
                queue.append((edge.source, new_path, new_weakest, effect))

        order = {Confidence.MEASURED: 0, Confidence.DOCUMENTED: 1, Confidence.INFERRED: 2}
        return sorted(found.values(), key=lambda h: (order[h.weakest], h.hops))

    def refutations_for(self, symptom_at: Component) -> list[Edge]:
        """Edges that a naive reading would follow, and that measurement rules out.

        Surfacing these to an agent is as useful as surfacing the causes: it is
        the difference between "I did not think of the database" and "I checked
        the database and it cannot produce this symptom".
        """
        return [e for e in self.edges if e.target == symptom_at and not e.causal]

    def edge(self, source: Component, target: Component) -> Edge | None:
        for candidate in self.edges:
            if candidate.source is source and candidate.target is target:
                return candidate
        return None

    @property
    def measured_fraction(self) -> float:
        """Share of edges backed by observation rather than assumption."""
        if not self.edges:
            return 0.0
        return sum(1 for e in self.edges if e.evidence.is_measured) / len(self.edges)


def _weaker(a: Confidence, b: Confidence) -> Confidence:
    order = {Confidence.MEASURED: 0, Confidence.DOCUMENTED: 1, Confidence.INFERRED: 2}
    return a if order[a] >= order[b] else b


def _severity_rank(p: Propagation) -> int:
    """HALTS is the most severe, NONE the least."""
    return {Propagation.HALTS: 2, Propagation.DEGRADES: 1, Propagation.NONE: 0}[p]


def _weaker_propagation(a: Propagation, b: Propagation) -> Propagation:
    """The weaker of two effects — a chain is only as strong as its weakest link."""
    return a if _severity_rank(a) <= _severity_rank(b) else b
