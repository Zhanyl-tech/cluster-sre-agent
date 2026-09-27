"""The Slurm control-plane dependency graph.

This file is the hypothesis. Everything else in the repository exists to find
out whether writing it down is worth anything.

The claim under test is that a large share of what general RCA agents lose is
lost to *graph inference*: cloud topologies are huge, dynamic and undocumented,
so an agent must work out the shape of the system and diagnose the fault at the
same time. A Slurm control plane is much smaller and changes only when it is
redeployed, so its core can simply be written down. It is not identical at
every site: whether an edge is active can depend on configuration (the
``slurm.config`` edge below is inactive with ``AccountingStorageEnforce=none``)
and on how a component fails (a StateSaveLocation that *errors* is not one that
*stalls*). Configuration conditions cannot be expressed yet. A failure mode can
be, but only one way: an edge measured in one mode may declare an
:class:`UntestedMode` expected to propagate worse, and traversal then treats
that mode as inference rather than letting the measurement cover it. See the
README's limitations.

Three properties make this file worth more than a diagram.

**Edges record whether a failure actually propagates, not just that a
dependency exists.** ``slurmctld`` depends on ``slurmdbd``, and every
architecture diagram draws that arrow. But in the one regime this project
measured (slurm-rca-bench S01: Slurm 25.11.4, database suspended for about
fifteen minutes, on a cluster S08 found running
``AccountingStorageEnforce=none``), a stalled accounting path
did *not* stop scheduling. Jobs kept being submitted, started and completed;
job start latency rose to tens of seconds. So the edge says DEGRADES, not
HALTS. An agent handed a dependency graph without propagation semantics will
follow the arrow to a confident wrong answer, which is exactly what the folk
model does.

**The graph is measured where it can be, and marked where it cannot.** Every
edge carries an :class:`Evidence` record saying whether the behaviour was
observed on a live cluster (and in which benchmark scenario), stated in
documentation, or inferred. An unverified edge is still usable, but it is never
presented to the agent as though it were established.

**It is checked against a pinned snapshot of the benchmark.**
``tests/test_benchmark_agreement.py`` reads a hash-checked copy of
slurm-rca-bench (``tests/fixtures/slurm_rca_bench``) and asserts that every
consecutive pair in every causal chain is a causal edge here, that walking
upstream from each chain's last node finds its root, that every MEASURED edge
names a scenario whose verification block says ``measured: true``, and that no
zero-credit answer of a measured scenario is offered as a cause of its symptom.
The snapshot is refreshed deliberately with ``scripts/sync_bench_snapshot.py``;
the build does not follow the benchmark automatically.
"""

from __future__ import annotations

from collections.abc import Iterator
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
    #: The dependency exists but a failure was checked and does not propagate.
    #: No edge in the current graph uses it: slurmdbd -> scheduler carried it
    #: until a re-reading of S01 found the measurement had recorded a
    #: job-start-latency degradation, so that edge now says DEGRADES. The
    #: value stays because "checked, and it does not propagate" is a result
    #: the graph must be able to record.
    NONE = "none"


class Confidence(StrEnum):
    """How the edge came to be believed."""

    #: Observed on a live cluster by this project, in the slurm-rca-bench
    #: scenario named by ``Evidence.scenario``.
    MEASURED = "measured"
    #: Stated in Slurm documentation or source, not independently observed.
    DOCUMENTED = "documented"
    #: Reasoned from architecture. Weakest, and flagged as such to the agent.
    INFERRED = "inferred"


#: Lower is stronger. Used for ranking and for "weakest link" composition.
_EVIDENCE_RANK = {Confidence.MEASURED: 0, Confidence.DOCUMENTED: 1, Confidence.INFERRED: 2}

#: HALTS is the most severe, NONE the least.
_SEVERITY_RANK = {Propagation.HALTS: 2, Propagation.DEGRADES: 1, Propagation.NONE: 0}


@dataclass(frozen=True, slots=True)
class UntestedMode:
    """A failure mode of the source that the edge's evidence did not exercise.

    Declared only when that mode is expected to propagate *worse* than the
    edge's ``propagation``. Without it, a measurement of the mild mode would
    silently cap the severe one: S06 measured a StateSaveLocation that
    *errors* (the controller degrades), and the graph once reported
    StateSaveLocation as "ruled out by measurement" for a scheduling halt,
    although a StateSaveLocation that *stalls* is the open hypothesis for
    exactly that halt.

    Traversal treats the mode as INFERRED: it can make the source a (weakly
    evidenced) cause at ``expected``, and it stops the edge from refuting
    anything at that severity.
    """

    #: What the mode is and why it is expected, for the agent and the CLI.
    description: str
    #: The effect it is expected to have on the edge's target.
    expected: Propagation


@dataclass(frozen=True, slots=True)
class Evidence:
    """Why an edge is believed, and how strongly."""

    confidence: Confidence
    note: str = ""
    #: The slurm-rca-bench scenario whose verification block records the
    #: observation. Tests require it for every MEASURED edge and check that the
    #: scenario really is marked ``measured: true``.
    scenario: str | None = None
    #: Slurm version the observation was made on. A measurement is a fact
    #: about one version, not about Slurm in general.
    slurm_version: str | None = None
    #: Conditions the observation did not cover. Printed next to any
    #: refutation that rests on this edge, so "ruled out" never silently
    #: stretches beyond what was measured.
    untested: tuple[str, ...] = ()
    #: A failure mode not exercised and expected to propagate worse; see
    #: :class:`UntestedMode`.
    untested_mode: UntestedMode | None = None
    #: Documentation that states the behaviour. Required for DOCUMENTED edges
    #: (a test holds every one to it), since that label claims a source.
    sources: tuple[str, ...] = ()

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
    #: than a point: where this was measured the values varied by a factor of
    #: four across runs on identical injections, so a single number would
    #: imply a precision the observations do not support.
    latency_s: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        mode = self.evidence.untested_mode
        if mode is not None and _SEVERITY_RANK[mode.expected] <= _SEVERITY_RANK[self.propagation]:
            raise ValueError(
                f"{self.source} -> {self.target}: an untested mode must be expected to be "
                f"worse than the edge's {self.propagation.value}, got {mode.expected.value}"
            )

    @property
    def causal(self) -> bool:
        """True when a fault here can explain a symptom there.

        An edge with ``Propagation.NONE`` is still part of the architecture and
        still worth recording: it is how the graph *refutes* a plausible
        hypothesis rather than merely failing to suggest it.
        """
        return self.propagation is not Propagation.NONE


_JOB_REASON_CODES = "https://slurm.schedmd.com/job_reason_codes.html"
_SLURM_CONF = "https://slurm.schedmd.com/slurm.conf.html"
_SLURMCTLD = "https://slurm.schedmd.com/slurmctld.html"
_OVERVIEW = "https://slurm.schedmd.com/overview.html"

#: The S01 run, by its id at the pinned snapshot commit
#: (tests/fixtures/slurm_rca_bench/SOURCE.json). slurm-rca-bench 0.2.0 renames
#: it ``S01-accounting-backend-stall`` and keeps this id as a legacy alias.
#: When the snapshot is re-synced past the rename, change it here, once; the
#: agreement tests fail on a pinned snapshot that no longer has this id.
S01 = "S01-storage-stall-scheduling-halt"
S06 = "S06-state-save-unwritable"

#: The graph.
#:
#: Ordered roughly upstream-to-downstream for readability. Every edge either
#: cites a measurement (with its benchmark scenario) or is explicitly marked as
#: documentation (with the pages that state it) or inference.
EDGES: tuple[Edge, ...] = (
    # ── storage ────────────────────────────────────────────────────────────
    Edge(
        source=Component.SHARED_FS,
        target=Component.MYSQL,
        propagation=Propagation.HALTS,
        symptom="queries block indefinitely rather than returning errors",
        latency_s=(0, 60),
        evidence=Evidence(
            Confidence.INFERRED,
            "Not measured, and no benchmark scenario measures it. S01 froze "
            "the database container with `docker pause` (the cgroup freezer, "
            "so the processes are suspended without being signalled), which "
            "gives the blocking shape a storage stall beneath the database "
            "would produce; but no filesystem was stalled, and the benchmark "
            "cluster has no shared filesystem under its database. What S01 did "
            "observe is the next edge down (db.mysql -> slurm.slurmdbd). "
            "Measuring this one needs a fault-injecting filesystem under the "
            "database.",
        ),
    ),
    Edge(
        source=Component.STATE_SAVE,
        target=Component.SLURMCTLD,
        propagation=Propagation.DEGRADES,
        symptom="job submission rejected: 'I/O error writing script/environment to file'",
        # The first submission after the chmod, 12 s in, was already rejected;
        # nothing was tried earlier, so the range ends where the record does.
        latency_s=(0, 12),
        evidence=Evidence(
            Confidence.MEASURED,
            "chmod 500 on StateSaveLocation. sbatch was rejected with no JobID "
            "on the first attempt; for the next minute the queue stayed empty "
            "with no pending backlog, and a submission after restoring the "
            "permissions ran to completion. DEGRADES rather than HALTS: the "
            "controller kept answering while refusing new jobs. No job was "
            "running during the fault (the baseline job had completed), so "
            "the effect on running work was not observed.",
            scenario=S06,
            slurm_version="25.11.4",
            untested=(
                "jobs already running when StateSaveLocation becomes unwritable: "
                "S06's baseline job completed before the chmod",
            ),
            untested_mode=UntestedMode(
                "a StateSaveLocation *stall* (writes block instead of failing), "
                "which S06's caveats name as a candidate for blocking slurmctld; "
                "S06 measured the error mode only",
                expected=Propagation.HALTS,
            ),
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
            scenario=S01,
            slurm_version="25.11.4",
            untested=("a database that fails fast (refuses connections) instead of stalling",),
        ),
    ),
    # The edge that matters most, and the one a diagram would draw wrong.
    Edge(
        source=Component.SLURMDBD,
        target=Component.SCHEDULER,
        propagation=Propagation.DEGRADES,
        symptom=(
            "job start latency rises to tens of seconds; scheduling does not halt (measured, S01)"
        ),
        evidence=Evidence(
            Confidence.MEASURED,
            "THE REFUTATION, scoped to what was measured. The widely repeated "
            "model is that a storage stall backs up through the accounting "
            "path until the controller's queues fill and scheduling halts, "
            "roughly thirteen minutes later. Measured on Slurm 25.11.4 with the "
            "database suspended for fifteen minutes: jobs submitted, started "
            "and completed throughout, sinfo never stalled, and the node stayed "
            "idle-and-available the entire time. Job start latency did degrade "
            "to tens of seconds (S01 caveats). So in that regime the accounting "
            "path does not HALT scheduling; it DEGRADES it, which is what this "
            "edge says. An earlier version said NONE, which overstated the same "
            "measurement. The untested conditions below are where it stops.",
            scenario=S01,
            slurm_version="25.11.4",
            untested=(
                "accounting-queue saturation: the DBD agent queue peaked at 6, "
                "while slurm.conf documents a MaxDBDMsgs floor of 10000",
                "AccountingStorageEnforce=associations or limits (S08 found the benchmark "
                "cluster set to none)",
                "stalls longer than the roughly fifteen minutes observed",
            ),
        ),
    ),
    Edge(
        source=Component.SLURMDBD,
        target=Component.SLURMCTLD,
        propagation=Propagation.DEGRADES,
        symptom=(
            "sdiag 'DBD Agent queue size' climbs; slurmctld logs no error; RPC service unaffected"
        ),
        latency_s=(60, 900),
        evidence=Evidence(
            Confidence.MEASURED,
            "Agent queue depth grew monotonically while the database was "
            "suspended, but arrival time varied by a factor of four across "
            "identical runs, so the latency here is a wide range on purpose. "
            "The symptom names sdiag, not the log: S01 records that slurmctld "
            "logs no error for the stalled backend.",
            scenario=S01,
            slurm_version="25.11.4",
            untested=(
                "accounting-queue saturation: the DBD agent queue peaked at 6, "
                "while slurm.conf documents a MaxDBDMsgs floor of 10000",
            ),
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
            "slurmctld(8): slurmctld 'accepts work (jobs), and allocates "
            "resources to those jobs', so with no controller running nothing "
            "is allocated. Not independently measured here. The overview page "
            "adds that a backup manager may take over on failure; this graph "
            "does not model failover.",
            sources=(_SLURMCTLD, _OVERVIEW),
        ),
    ),
    Edge(
        source=Component.CONFIG,
        target=Component.SCHEDULER,
        propagation=Propagation.HALTS,
        symptom=(
            "jobs PENDING with a Reason naming a limit "
            "(e.g. AssocGrpJobsLimit, AssocMaxJobsLimit, QOSGrpJobsLimit)"
        ),
        latency_s=(0, 30),
        evidence=Evidence(
            Confidence.DOCUMENTED,
            "Documented, not measured. slurm.conf's AccountingStorageEnforce "
            "controls whether association limits are enforced at all, and the "
            "job reason codes page names the limit reasons above. What *was* "
            "observed (S08) is the negative: on the benchmark cluster, with "
            "AccountingStorageEnforce=none, a GrpJobs=0 limit had no effect and "
            "sbatch succeeded, so this edge is inactive there. The positive "
            "propagation has not been measured.",
            sources=(_SLURM_CONF, _JOB_REASON_CODES),
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
        symptom=(
            "'Node not responding'; after SlurmdTimeout the controller sets the "
            "node DOWN and the partition shrinks"
        ),
        latency_s=(0, 300),
        evidence=Evidence(
            Confidence.DOCUMENTED,
            "slurm.conf: SlurmdTimeout is how long slurmctld waits for slurmd "
            "before setting the node DOWN (default 300 s, hence the latency "
            "range); with ReturnToService=0 (the default) the node stays DOWN "
            "until an administrator changes its state. An earlier version of "
            "this edge claimed an observed drain after pod replacement; no "
            "benchmark run records that, so it is not claimed here.",
            sources=(_SLURM_CONF,),
        ),
    ),
    Edge(
        source=Component.GPU_DEVICE,
        target=Component.SLURMD,
        propagation=Propagation.DEGRADES,
        symptom="jobs on one node fail with CUDA errors; other nodes are fine",
        latency_s=(0, 60),
        # The three hardware edges below were labelled DOCUMENTED with no source
        # cited. What they describe is GPU, driver and fabric behaviour, which
        # Slurm's documentation does not state, so they are inference until a
        # page that states them is cited or a run measures them.
        evidence=Evidence(
            Confidence.INFERRED,
            "Reasoned, not measured or cited: per-device ECC and row-remap "
            "counters. The discriminator against a driver fault is that "
            "hardware faults are per-device.",
        ),
    ),
    Edge(
        source=Component.GPU_DRIVER,
        target=Component.SLURMD,
        propagation=Propagation.HALTS,
        symptom="every GPU on the node fails at once; health check drains it repeatedly",
        latency_s=(0, 60),
        evidence=Evidence(
            Confidence.INFERRED,
            "Reasoned, not measured or cited: wholesale rather than per-device "
            "failure is the signature that separates a driver fault from a "
            "hardware one. The drain needs a HealthCheckProgram that detects it.",
        ),
    ),
    Edge(
        source=Component.FABRIC,
        target=Component.SLURMD,
        propagation=Propagation.DEGRADES,
        symptom="multi-node jobs slow dramatically; single-node jobs unaffected",
        latency_s=(0, 300),
        evidence=Evidence(
            Confidence.INFERRED,
            "Reasoned, not measured or cited: the single-node/multi-node split "
            "is the discriminator against a GPU fault, which would slow both.",
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
    #: Edges this path crosses only through their :class:`UntestedMode`. The
    #: hypothesis holds only if the source fails in that unmeasured way, so
    #: it is never better than INFERRED, and the CLI says which mode.
    untested_modes: tuple[Edge, ...] = ()
    hops: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "hops", len(self.path) - 1)


@dataclass(frozen=True, slots=True)
class Refutation:
    """A component that reaches the symptom only through paths too weak to produce it.

    "Ruled out" always means *within this graph*: a component can only be
    ruled out through edges that exist, so a missing edge is a missing
    hypothesis, not a refutation.
    """

    component: Component
    #: The refuted path the confidence is held to (the least well refuted
    #: one), cause first, symptom last.
    path: tuple[Component, ...]
    #: The edge on that path whose propagation is below the reported
    #: severity. It is what does the refuting on that path.
    limiting_edge: Edge
    #: How strongly the component is ruled out. See
    #: :meth:`DependencyGraph.refutations_for` for how it is composed.
    confidence: Confidence
    #: The capping edge of *every* path from the component (the strongest cap
    #: on each), ``limiting_edge`` first, without repeats. Every path has to
    #: be closed, so the refutation rests on all of them, and the untested
    #: conditions of each are caveats of it; the CLI prints them all.
    caps: tuple[Edge, ...] = ()

    @property
    def by_measurement(self) -> bool:
        return self.confidence is Confidence.MEASURED


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
        is easy to get wrong: the first version of this method did.

        A fault at the far end of a path produces, at the near end, an effect no
        worse than the *weakest* link along it. So the accounting chain

            db.mysql --HALTS--> slurmdbd --DEGRADES--> slurmctld --HALTS--> scheduler

        cannot explain a scheduling *halt*: the middle link only degrades the
        controller, and a degraded controller keeps scheduling. Chaining the two
        HALTS edges around it and concluding "the database stopped scheduling"
        is precisely the folk model this project measured and refuted, and a
        naive breadth-first walk reintroduces it, because the arrows are all
        there. Requiring every edge to carry the severity (the ``min`` over the
        path) is what keeps it out.

        The same chain *does* explain a degraded controller, which is true and
        was observed. Ask for the severity you actually saw.

        An edge carries a severity its measured ``propagation`` does not reach
        only through a declared :class:`UntestedMode`, and then at INFERRED:
        StateSaveLocation is offered for a scheduling halt only as an inferred
        cause, through the stall mode S06 did not exercise, and the hypothesis
        names that edge in ``untested_modes``.

        **Every simple path is considered**, and for each cause the best-evidenced
        one is reported: strongest weakest-link evidence first, then fewest
        hops. (An earlier breadth-first version kept the first path it found,
        so a cause with an inferred one-hop path and a fully measured two-hop
        path was reported as "inferred".) Evidence is preferred over a more
        severe effect on purpose: any path at or above ``severity`` explains
        the symptom, and the question is which explanation is best supported.
        Enumerating every path is fine at this size (a dozen edges); it would
        need a best-first search on a much larger graph.

        Results are ordered by evidence strength first, then path length, so a
        measured three-hop explanation outranks an inferred one-hop guess.
        ``max_hops`` bounds path length: 0 returns nothing, 1 only direct causes.
        """
        _require_symptom_severity(severity)
        best: dict[Component, Hypothesis] = {}
        for edges in self._paths_to(symptom_at, max_hops=max_hops):
            carried = [c for c in (_carries(edge, severity) for edge in edges) if c is not None]
            if len(carried) < len(edges):
                continue  # some edge cannot carry this severity in any mode
            weakest = Confidence.MEASURED
            for confidence in carried:
                weakest = _weaker(weakest, confidence)
            candidate = Hypothesis(
                cause=edges[0].source,
                path=_components(edges),
                fully_measured=weakest is Confidence.MEASURED,
                weakest=weakest,
                untested_modes=tuple(e for e in edges if not _reaches(e.propagation, severity)),
            )
            current = best.get(candidate.cause)
            if current is None or _preference(candidate) < _preference(current):
                best[candidate.cause] = candidate
        return sorted(best.values(), key=_preference)

    def refutations_for(
        self,
        symptom_at: Component,
        *,
        severity: Propagation = Propagation.HALTS,
    ) -> list[Refutation]:
        """Components the graph rules out as causes of this symptom at this severity.

        Surfacing these to an agent is as useful as surfacing the causes: it is
        the difference between "I did not think of the database" and "I checked
        the database and it cannot produce this symptom".

        A component is ruled out when it reaches the symptom through at least
        one path and **every** path is capped below ``severity`` by some edge
        (a DEGRADES edge for a reported halt, or a NONE edge). Anything
        :meth:`causes_of` would return is therefore never listed here; the
        earlier version ignored severity and printed slurmdbd as both a cause
        and "ruled out".

        How strongly it is ruled out is composed the way the logic requires:

        * One path is refuted as strongly as the *strongest* of its capping
          edges. Only one true cap is needed, and the edges between the cause
          and the cap do not weaken it (if they are wrong, the component's
          influence is smaller, not larger).
        * The component is ruled out only as strongly as its *least* well
          refuted path, because every path has to be closed.

        So "ruled out by measurement" means every path ends in a measured cap,
        and each cap's ``Evidence.untested`` lists the conditions known to lie
        outside that measurement (a list of known gaps, not a proof that
        nothing else is). :attr:`Refutation.caps` carries every one of those
        caps, not only the limiting one.

        A measurement caps only the failure mode it exercised. An edge whose
        :class:`UntestedMode` is expected to reach ``severity`` caps nothing
        at that severity (the path is open through that mode, so the component
        is a cause, at INFERRED, and not a refutation). One whose untested mode
        is expected to stay below ``severity`` still caps, but no better than
        INFERRED, because that mode's staying below is itself inference.

        Paths are not bounded by a hop limit here: a hop limit could hide a
        long path that does produce the symptom and turn a cause into a false
        refutation.
        """
        _require_symptom_severity(severity)
        paths_by_source: dict[Component, list[tuple[Edge, ...]]] = {}
        for edges in self._paths_to(symptom_at, max_hops=len(self.edges)):
            paths_by_source.setdefault(edges[0].source, []).append(edges)

        found: list[Refutation] = []
        for component, paths in paths_by_source.items():
            refuted: list[tuple[Edge, Confidence, tuple[Edge, ...]]] = []
            for edges in paths:
                cap = _path_cap(edges, severity)
                if cap is None:
                    break  # this path explains the symptom: a cause, not a refutation
                refuted.append((*cap, edges))
            else:
                limiting, confidence, edges = min(
                    refuted,
                    key=lambda item: (
                        -_EVIDENCE_RANK[item[1]],
                        len(item[2]),
                        tuple(c.value for c in _components(item[2])),
                    ),
                )
                others = sorted({cap for cap, _, _ in refuted} - {limiting}, key=self.edges.index)
                found.append(
                    Refutation(
                        component=component,
                        path=_components(edges),
                        limiting_edge=limiting,
                        confidence=confidence,
                        caps=(limiting, *others),
                    )
                )
        return sorted(
            found,
            key=lambda r: (_EVIDENCE_RANK[r.confidence], len(r.path), r.component.value),
        )

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

    @property
    def evidence_counts(self) -> dict[Confidence, int]:
        """Edges per evidence level, computed from the edges themselves.

        The README quotes these numbers; a test holds the two together.
        """
        counts = dict.fromkeys(Confidence, 0)
        for edge in self.edges:
            counts[edge.evidence.confidence] += 1
        return counts

    def _paths_to(self, symptom_at: Component, *, max_hops: int) -> Iterator[tuple[Edge, ...]]:
        """Every simple upstream path ending at ``symptom_at``, cause-first.

        Includes non-causal edges; callers decide what a path means.
        """
        stack: list[tuple[Component, tuple[Edge, ...], frozenset[Component]]] = [
            (symptom_at, (), frozenset({symptom_at}))
        ]
        while stack:
            node, edges, visited = stack.pop()
            if len(edges) >= max_hops:
                continue
            for edge in self.upstream_of(node):
                if edge.source in visited:
                    continue
                path = (edge, *edges)
                yield path
                stack.append((edge.source, path, visited | {edge.source}))


def _components(edges: tuple[Edge, ...]) -> tuple[Component, ...]:
    return (edges[0].source, *(e.target for e in edges))


def _reaches(effect: Propagation, severity: Propagation) -> bool:
    """True when ``effect`` is at least ``severity`` (never NONE, which is no effect)."""
    return effect is not Propagation.NONE and _severity_rank(effect) >= _severity_rank(severity)


def _carries(edge: Edge, severity: Propagation) -> Confidence | None:
    """How well-evidenced it is that this edge passes on a failure of ``severity``.

    ``None`` when it cannot in any known mode. A path produces a symptom only
    if every edge carries it (a chain is only as strong as its weakest link),
    and the path's evidence is the weakest of its edges'.
    """
    if _reaches(edge.propagation, severity):
        return edge.evidence.confidence
    mode = edge.evidence.untested_mode
    if mode is not None and _reaches(mode.expected, severity):
        return Confidence.INFERRED
    return None


def _cap(edge: Edge, severity: Propagation) -> Confidence | None:
    """How strongly this edge keeps a path below ``severity``; ``None`` if it does not."""
    if _carries(edge, severity) is not None:
        return None
    if edge.evidence.untested_mode is None:
        return edge.evidence.confidence
    # Both modes stay below severity, but the untested one only by inference.
    return _weaker(edge.evidence.confidence, Confidence.INFERRED)


def _path_cap(edges: tuple[Edge, ...], severity: Propagation) -> tuple[Edge, Confidence] | None:
    """The strongest capping edge on a path and its confidence; ``None`` if it produces."""
    caps = [(edge, c) for edge in edges if (c := _cap(edge, severity)) is not None]
    if not caps:
        return None
    return min(caps, key=lambda item: _EVIDENCE_RANK[item[1]])


def _preference(h: Hypothesis) -> tuple[int, int, tuple[str, ...]]:
    """Sort key: strongest weakest-link evidence, then fewest hops, then name."""
    return (_EVIDENCE_RANK[h.weakest], h.hops, tuple(c.value for c in h.path))


def _require_symptom_severity(severity: Propagation) -> None:
    if severity is Propagation.NONE:
        raise ValueError("a symptom is something that happened: ask for HALTS or DEGRADES")


def _weaker(a: Confidence, b: Confidence) -> Confidence:
    return a if _EVIDENCE_RANK[a] >= _EVIDENCE_RANK[b] else b


def _severity_rank(p: Propagation) -> int:
    return _SEVERITY_RANK[p]
