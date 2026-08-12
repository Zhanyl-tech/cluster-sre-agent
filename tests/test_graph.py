"""The graph must agree with what was measured.

A hand-written dependency graph is an opinion until something checks it. These
tests are the check, and the most important one asserts a *negative*: that
traversing from a scheduling symptom does not reach the accounting path,
because a stalled slurmdbd was measured not to halt scheduling.

That single assertion is the difference between this graph and the diagram
everybody already has.
"""

from __future__ import annotations

import pytest

from csa.graph import (
    EDGES,
    Component,
    Confidence,
    DependencyGraph,
    Propagation,
)

GRAPH = DependencyGraph()


class TestTheRefutation:
    """The measured finding the whole repo turns on."""

    def test_accounting_does_not_halt_scheduling(self) -> None:
        edge = GRAPH.edge(Component.SLURMDBD, Component.SCHEDULER)
        assert edge is not None, "the edge must exist, precisely so it can say 'no'"
        assert edge.propagation is Propagation.NONE
        assert not edge.causal
        assert edge.evidence.is_measured, "a claim this load-bearing must be measured"

    def test_traversal_from_scheduling_never_blames_the_database(self) -> None:
        # The folk model's answer. If this ever starts appearing, the graph has
        # regressed into the diagram it was built to replace.
        causes = {h.cause for h in GRAPH.causes_of(Component.SCHEDULER)}
        assert Component.MYSQL not in causes
        assert Component.SLURMDBD not in causes
        assert Component.SHARED_FS not in causes

    def test_the_refutation_is_reportable(self) -> None:
        # Being able to say "I checked the database and it cannot cause this"
        # is worth as much as naming the real cause.
        refuted = GRAPH.refutations_for(Component.SCHEDULER)
        assert any(e.source is Component.SLURMDBD for e in refuted)

    def test_accounting_symptoms_still_reach_storage(self) -> None:
        # The chain is real for accounting — it just stops short of scheduling.
        causes = {h.cause for h in GRAPH.causes_of(Component.SLURMDBD)}
        assert Component.MYSQL in causes
        assert Component.SHARED_FS in causes


class TestTraversal:
    def test_scheduling_symptoms_reach_the_controller_and_config(self) -> None:
        causes = {h.cause for h in GRAPH.causes_of(Component.SCHEDULER)}
        assert Component.SLURMCTLD in causes
        assert Component.CONFIG in causes

    def test_state_save_reaches_the_controller(self) -> None:
        # Asked at DEGRADES, because that is what was measured: submissions are
        # rejected while the controller keeps serving queries and running work.
        # Asking at HALTS correctly returns nothing, and that asymmetry is the
        # point — the caller has to say what they actually saw.
        causes = {
            h.cause for h in GRAPH.causes_of(Component.SLURMCTLD, severity=Propagation.DEGRADES)
        }
        assert Component.STATE_SAVE in causes
        assert Component.STATE_SAVE not in {
            h.cause for h in GRAPH.causes_of(Component.SLURMCTLD, severity=Propagation.HALTS)
        }

    def test_node_symptoms_reach_gpu_driver_and_fabric(self) -> None:
        degraded = {
            h.cause for h in GRAPH.causes_of(Component.SLURMD, severity=Propagation.DEGRADES)
        }
        assert Component.GPU_DEVICE in degraded
        assert Component.FABRIC in degraded
        # A wedged driver takes every GPU at once, so it halts the node rather
        # than degrading it — and the graph distinguishes the two.
        halted = {h.cause for h in GRAPH.causes_of(Component.SLURMD, severity=Propagation.HALTS)}
        assert Component.GPU_DRIVER in halted
        assert Component.GPU_DEVICE not in halted

    def test_measured_paths_are_ranked_above_inferred_ones(self) -> None:
        hypotheses = GRAPH.causes_of(Component.SLURMCTLD)
        weakest = [h.weakest for h in hypotheses]
        order = {Confidence.MEASURED: 0, Confidence.DOCUMENTED: 1, Confidence.INFERRED: 2}
        assert [order[w] for w in weakest] == sorted(order[w] for w in weakest)

    def test_paths_start_at_the_cause_and_end_at_the_symptom(self) -> None:
        for hypothesis in GRAPH.causes_of(Component.SCHEDULER):
            assert hypothesis.path[0] is hypothesis.cause
            assert hypothesis.path[-1] is Component.SCHEDULER
            assert hypothesis.hops >= 1

    def test_no_hypothesis_repeats_a_component(self) -> None:
        for hypothesis in GRAPH.causes_of(Component.SCHEDULER):
            assert len(set(hypothesis.path)) == len(hypothesis.path)

    def test_a_leaf_symptom_yields_no_causes(self) -> None:
        assert GRAPH.causes_of(Component.SHARED_FS) == []


class TestEvidenceDiscipline:
    """Every edge must say how it came to be believed."""

    @pytest.mark.parametrize("edge", EDGES, ids=[f"{e.source}->{e.target}" for e in EDGES])
    def test_every_edge_cites_its_evidence(self, edge: object) -> None:
        assert edge.evidence.note.strip(), "an unexplained edge is an unfalsifiable one"  # type: ignore[attr-defined]

    @pytest.mark.parametrize("edge", EDGES, ids=[f"{e.source}->{e.target}" for e in EDGES])
    def test_causal_edges_describe_a_symptom(self, edge: object) -> None:
        if edge.causal:  # type: ignore[attr-defined]
            assert edge.symptom.strip()  # type: ignore[attr-defined]

    def test_latency_is_a_range_not_a_point(self) -> None:
        # Identical injections varied by a factor of four, so a single number
        # would imply precision the observations do not support.
        for edge in EDGES:
            if edge.latency_s is not None:
                low, high = edge.latency_s
                assert 0 <= low <= high

    def test_most_of_the_graph_is_measured(self) -> None:
        # The graph's authority comes from observation. If this drops, the file
        # has drifted back toward being a diagram.
        assert GRAPH.measured_fraction >= 0.5

    def test_no_duplicate_edges(self) -> None:
        pairs = [(e.source, e.target) for e in EDGES]
        assert len(set(pairs)) == len(pairs)


class TestAgreesWithTheBenchmark:
    """The graph and slurm-rca-bench must not contradict each other.

    Both encode the same measurements. If they diverge, one is stale — and a
    silent divergence would mean the agent is reasoning over a world model the
    benchmark does not score.
    """

    def test_vocabulary_matches_the_benchmark(self) -> None:
        # Component values are the benchmark's answer identifiers verbatim, so a
        # traversal result can be scored with no translation layer.
        expected = {
            "storage.shared_fs",
            "storage.state_save",
            "db.mysql",
            "slurm.slurmdbd",
            "slurm.slurmctld",
            "slurm.scheduler",
            "slurm.slurmd",
            "slurm.config",
            "gpu.device",
            "gpu.driver",
            "fabric.interconnect",
            "network.control_plane",
        }
        assert {c.value for c in Component} == expected

    def test_s06_state_save_chain_is_reproducible(self) -> None:
        # slurm-rca-bench S06: StateSaveLocation unwritable -> submissions fail
        # while everything else keeps working. A degradation, not a halt.
        causes = {
            h.cause for h in GRAPH.causes_of(Component.SLURMCTLD, severity=Propagation.DEGRADES)
        }
        assert Component.STATE_SAVE in causes

    def test_s01_refutation_matches_the_benchmark(self) -> None:
        # slurm-rca-bench S01 documents the same refutation. Both must agree
        # that the accounting path does not reach scheduling.
        assert not GRAPH.edge(Component.SLURMDBD, Component.SCHEDULER).causal  # type: ignore[union-attr]
