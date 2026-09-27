"""The graph must agree with what was measured.

A hand-written dependency graph is an opinion until something checks it. These
tests are the check, and the most important one asserts a *negative*: that
traversing from a scheduling halt does not reach the accounting path, because a
stalled slurmdbd was measured not to halt scheduling.

That single assertion is the difference between this graph and the diagram
everybody already has. Agreement with the benchmark's scenarios lives in
``test_benchmark_agreement.py``.
"""

from __future__ import annotations

import pytest

from csa.graph import (
    EDGES,
    Component,
    Confidence,
    DependencyGraph,
    Edge,
    Evidence,
    Propagation,
    UntestedMode,
)

GRAPH = DependencyGraph()
C = Component
MEAS, DOC, INF = Confidence.MEASURED, Confidence.DOCUMENTED, Confidence.INFERRED
HALTS, DEGRADES, NONE = Propagation.HALTS, Propagation.DEGRADES, Propagation.NONE


def edge(
    source: Component,
    target: Component,
    p: Propagation,
    c: Confidence,
    untested: Propagation | None = None,
) -> Edge:
    """A throwaway edge for a hand-built graph, optionally with an untested mode."""
    mode = UntestedMode("test mode", expected=untested) if untested is not None else None
    return Edge(source, target, p, Evidence(c, "test", untested_mode=mode), symptom="test")


class TestTheRefutation:
    """The measured finding the whole repo turns on, scoped to what was measured."""

    def test_accounting_does_not_halt_scheduling(self) -> None:
        e = GRAPH.edge(C.SLURMDBD, C.SCHEDULER)
        assert e is not None, "the edge must exist, precisely so it can say 'not a halt'"
        assert e.propagation is not HALTS
        # S01 recorded job start latency degrading to tens of seconds, so the
        # honest label is DEGRADES; NONE overstated the same measurement.
        assert e.propagation is DEGRADES
        assert e.evidence.is_measured, "a claim this load-bearing must be measured"
        # By number: the benchmark is renaming S01, and the agreement tests
        # check the full id against the pinned snapshot.
        assert (e.evidence.scenario or "").startswith("S01-")

    def test_the_refutation_states_where_it_stops(self) -> None:
        e = GRAPH.edge(C.SLURMDBD, C.SCHEDULER)
        assert e is not None
        untested = " ".join(e.evidence.untested)
        assert "MaxDBDMsgs" in untested, "the queue never came near its limit"
        assert "AccountingStorageEnforce" in untested, "the cluster ran with enforce=none"

    def test_traversal_from_a_scheduling_halt_never_blames_the_accounting_path(self) -> None:
        # The folk model's answer. If this ever starts appearing, the graph has
        # regressed into the diagram it was built to replace.
        causes = {h.cause for h in GRAPH.causes_of(C.SCHEDULER)}
        assert C.MYSQL not in causes
        assert C.SLURMDBD not in causes
        assert C.SHARED_FS not in causes

    def test_the_refutation_is_reportable_as_measured(self) -> None:
        # Being able to say "I checked the database and it cannot cause this"
        # is worth as much as naming the real cause.
        refuted = {r.component: r for r in GRAPH.refutations_for(C.SCHEDULER)}
        assert refuted[C.SLURMDBD].by_measurement
        assert refuted[C.SLURMDBD].limiting_edge.source is C.SLURMDBD

    def test_components_ruled_out_by_composition_are_listed(self) -> None:
        # The database and the filesystem reach the scheduler only through the
        # measured DEGRADES links, so a halt rules them out. The first version
        # of refutations_for listed only direct NONE edges and missed them.
        refuted = {r.component for r in GRAPH.refutations_for(C.SCHEDULER)}
        assert {C.MYSQL, C.SHARED_FS} <= refuted

    def test_a_degraded_scheduler_is_explained_by_the_accounting_path(self) -> None:
        causes = {h.cause: h for h in GRAPH.causes_of(C.SCHEDULER, severity=DEGRADES)}
        assert causes[C.SLURMDBD].fully_measured
        refuted = {r.component for r in GRAPH.refutations_for(C.SCHEDULER, severity=DEGRADES)}
        assert C.SLURMDBD not in refuted

    @pytest.mark.parametrize("severity", [HALTS, DEGRADES])
    @pytest.mark.parametrize("symptom", list(Component))
    def test_nothing_is_both_a_cause_and_ruled_out(
        self, symptom: Component, severity: Propagation
    ) -> None:
        # The CLI once printed slurmdbd as a cause and as "ruled out by
        # measurement" in the same answer.
        causes = {h.cause for h in GRAPH.causes_of(symptom, severity=severity)}
        refuted = {r.component for r in GRAPH.refutations_for(symptom, severity=severity)}
        assert not causes & refuted

    def test_accounting_symptoms_still_reach_storage(self) -> None:
        # The chain is real for accounting; it just stops short of halting
        # scheduling.
        causes = {h.cause for h in GRAPH.causes_of(C.SLURMDBD)}
        assert C.MYSQL in causes
        assert C.SHARED_FS in causes


class TestRefutationEvidence:
    """'Ruled out by measurement' has to mean exactly that."""

    def test_an_inferred_none_edge_is_not_reported_as_measured(self) -> None:
        g = DependencyGraph((edge(C.CONTROL_NET, C.SCHEDULER, NONE, INF),))
        (r,) = g.refutations_for(C.SCHEDULER, severity=DEGRADES)
        assert r.component is C.CONTROL_NET
        assert r.confidence is INF
        assert not r.by_measurement

    def test_a_component_is_ruled_out_only_as_strongly_as_its_weakest_path(self) -> None:
        g = DependencyGraph(
            (
                edge(C.MYSQL, C.SLURMCTLD, DEGRADES, MEAS),  # measured cap
                edge(C.SLURMCTLD, C.SCHEDULER, HALTS, MEAS),
                edge(C.MYSQL, C.SLURMDBD, HALTS, MEAS),
                edge(C.SLURMDBD, C.SCHEDULER, DEGRADES, INF),  # inferred cap
            )
        )
        r = {r.component: r for r in g.refutations_for(C.SCHEDULER)}[C.MYSQL]
        assert r.confidence is INF, "one path is closed only by inference"
        assert r.limiting_edge.source is C.SLURMDBD

    def test_edges_before_the_cap_do_not_weaken_a_refutation(self) -> None:
        # If the inferred first hop is wrong, the component's influence is
        # smaller, not larger, so the measured cap still rules it out.
        g = DependencyGraph(
            (
                edge(C.SHARED_FS, C.MYSQL, HALTS, INF),
                edge(C.MYSQL, C.SCHEDULER, DEGRADES, MEAS),
            )
        )
        refuted = {r.component: r for r in g.refutations_for(C.SCHEDULER)}
        assert refuted[C.SHARED_FS].by_measurement

    def test_a_long_path_that_produces_the_symptom_is_never_hidden(self) -> None:
        # A hop limit on refutations would turn this cause into a "refutation".
        g = DependencyGraph(
            (
                edge(C.SHARED_FS, C.SCHEDULER, DEGRADES, MEAS),
                edge(C.SHARED_FS, C.MYSQL, HALTS, MEAS),
                edge(C.MYSQL, C.SLURMDBD, HALTS, MEAS),
                edge(C.SLURMDBD, C.SLURMCTLD, HALTS, MEAS),
                edge(C.SLURMCTLD, C.CONFIG, HALTS, MEAS),
                edge(C.CONFIG, C.SCHEDULER, HALTS, MEAS),
            )
        )
        assert C.SHARED_FS not in {r.component for r in g.refutations_for(C.SCHEDULER)}
        assert C.SHARED_FS in {h.cause for h in g.causes_of(C.SCHEDULER, max_hops=5)}

    def test_every_path_cap_is_carried_not_only_the_limiting_one(self) -> None:
        # slurmdbd reaches the scheduler directly and through slurmctld; each
        # path is closed by a different measured edge, and "ruled out" rests
        # on both, so both sets of caveats apply. The CLI once printed only
        # the limiting edge's.
        r = {r.component: r for r in GRAPH.refutations_for(C.SCHEDULER)}[C.SLURMDBD]
        assert r.by_measurement
        assert [(e.source, e.target) for e in r.caps] == [
            (C.SLURMDBD, C.SCHEDULER),
            (C.SLURMDBD, C.SLURMCTLD),
        ]
        assert r.caps[0] == r.limiting_edge

    def test_caps_are_listed_once_limiting_first(self) -> None:
        g = DependencyGraph(
            (
                edge(C.SHARED_FS, C.MYSQL, HALTS, MEAS),
                edge(C.MYSQL, C.SLURMCTLD, DEGRADES, MEAS),
                edge(C.SLURMCTLD, C.SCHEDULER, HALTS, MEAS),
                edge(C.MYSQL, C.SCHEDULER, DEGRADES, INF),
            )
        )
        r = {r.component: r for r in g.refutations_for(C.SCHEDULER)}[C.SHARED_FS]
        assert r.confidence is INF
        assert [(e.source, e.target) for e in r.caps] == [
            (C.MYSQL, C.SCHEDULER),  # the limiting (least well refuted) path's cap
            (C.MYSQL, C.SLURMCTLD),
        ]


class TestUntestedModes:
    """A measurement caps only the failure mode it exercised."""

    def test_state_save_is_not_ruled_out_by_measurement_for_a_scheduling_halt(self) -> None:
        # S06 measured a StateSaveLocation that *errors*. A stalled one is the
        # open hypothesis for exactly this halt, so the error-mode measurement
        # must not be reported as having ruled it out.
        refuted = {r.component: r for r in GRAPH.refutations_for(C.SCHEDULER)}
        assert C.STATE_SAVE not in refuted

    def test_state_save_is_offered_for_a_halt_only_as_inference(self) -> None:
        h = {h.cause: h for h in GRAPH.causes_of(C.SCHEDULER)}[C.STATE_SAVE]
        assert h.weakest is INF
        assert not h.fully_measured
        assert [(e.source, e.target) for e in h.untested_modes] == [(C.STATE_SAVE, C.SLURMCTLD)]

    def test_the_measured_mode_still_answers_a_degradation(self) -> None:
        h = {h.cause: h for h in GRAPH.causes_of(C.SLURMCTLD, severity=DEGRADES)}[C.STATE_SAVE]
        assert h.fully_measured
        assert h.untested_modes == ()

    @pytest.mark.parametrize("severity", [HALTS, DEGRADES])
    @pytest.mark.parametrize("symptom", list(Component))
    def test_no_measured_refutation_rests_on_an_edge_with_an_untested_mode(
        self, symptom: Component, severity: Propagation
    ) -> None:
        for r in GRAPH.refutations_for(symptom, severity=severity):
            if r.by_measurement:
                assert all(cap.evidence.is_measured for cap in r.caps)
                assert all(cap.evidence.untested_mode is None for cap in r.caps)

    def test_an_untested_mode_below_the_severity_caps_only_by_inference(self) -> None:
        # Measured: no effect. Untested mode: expected to degrade. At HALTS
        # both modes stay below, but only one of them was observed to.
        g = DependencyGraph((edge(C.MYSQL, C.SCHEDULER, NONE, MEAS, untested=DEGRADES),))
        (r,) = g.refutations_for(C.SCHEDULER)
        assert r.confidence is INF
        (h,) = g.causes_of(C.SCHEDULER, severity=DEGRADES)
        assert h.weakest is INF
        assert g.refutations_for(C.SCHEDULER, severity=DEGRADES) == []

    def test_the_documented_mode_is_preferred_where_it_suffices(self) -> None:
        g = DependencyGraph((edge(C.MYSQL, C.SCHEDULER, DEGRADES, DOC, untested=HALTS),))
        (h,) = g.causes_of(C.SCHEDULER, severity=DEGRADES)
        assert h.weakest is DOC
        assert h.untested_modes == ()
        (h,) = g.causes_of(C.SCHEDULER)
        assert h.weakest is INF
        assert g.refutations_for(C.SCHEDULER) == []

    @pytest.mark.parametrize(("measured", "untested"), [(DEGRADES, DEGRADES), (HALTS, DEGRADES)])
    def test_an_untested_mode_must_be_worse_than_the_measured_one(
        self, measured: Propagation, untested: Propagation
    ) -> None:
        # Otherwise it adds nothing but the appearance of a caveat.
        with pytest.raises(ValueError, match="worse"):
            edge(C.MYSQL, C.SCHEDULER, measured, MEAS, untested=untested)


class TestTraversal:
    def test_scheduling_symptoms_reach_the_controller_and_config(self) -> None:
        causes = {h.cause for h in GRAPH.causes_of(C.SCHEDULER)}
        assert C.SLURMCTLD in causes
        assert C.CONFIG in causes

    def test_state_save_reaches_the_controller(self) -> None:
        # At DEGRADES the measured error mode explains it: submissions are
        # rejected while the controller keeps answering. At HALTS only the
        # unmeasured stall mode could, so it is offered, but only as inference.
        # That asymmetry is the point: the caller has to say what they saw.
        degraded = {h.cause: h for h in GRAPH.causes_of(C.SLURMCTLD, severity=DEGRADES)}
        assert degraded[C.STATE_SAVE].fully_measured
        halted = {h.cause: h for h in GRAPH.causes_of(C.SLURMCTLD)}
        assert halted[C.STATE_SAVE].weakest is INF

    def test_node_symptoms_reach_gpu_driver_and_fabric(self) -> None:
        degraded = {h.cause for h in GRAPH.causes_of(C.SLURMD, severity=DEGRADES)}
        assert C.GPU_DEVICE in degraded
        assert C.FABRIC in degraded
        # A wedged driver takes every GPU at once, so it halts the node rather
        # than degrading it, and the graph distinguishes the two.
        halted = {h.cause for h in GRAPH.causes_of(C.SLURMD, severity=HALTS)}
        assert C.GPU_DRIVER in halted
        assert C.GPU_DEVICE not in halted

    def test_measured_paths_are_ranked_above_inferred_ones(self) -> None:
        for severity in (HALTS, DEGRADES):
            ranks = [
                list(Confidence).index(h.weakest)
                for h in GRAPH.causes_of(C.SCHEDULER, severity=severity)
            ]
            assert ranks == sorted(ranks)

    def test_paths_start_at_the_cause_and_end_at_the_symptom(self) -> None:
        for hypothesis in GRAPH.causes_of(C.SCHEDULER, severity=DEGRADES):
            assert hypothesis.path[0] is hypothesis.cause
            assert hypothesis.path[-1] is C.SCHEDULER
            assert hypothesis.hops >= 1

    def test_no_hypothesis_repeats_a_component(self) -> None:
        for hypothesis in GRAPH.causes_of(C.SCHEDULER, severity=DEGRADES):
            assert len(set(hypothesis.path)) == len(hypothesis.path)

    def test_a_leaf_symptom_yields_no_causes(self) -> None:
        assert GRAPH.causes_of(C.SHARED_FS) == []

    def test_none_is_not_a_symptom_severity(self) -> None:
        with pytest.raises(ValueError, match="HALTS or DEGRADES"):
            GRAPH.causes_of(C.SCHEDULER, severity=NONE)
        with pytest.raises(ValueError, match="HALTS or DEGRADES"):
            GRAPH.refutations_for(C.SCHEDULER, severity=NONE)


class TestPathChoice:
    """Regression tests for the audit's counterexamples."""

    def test_a_fully_measured_path_beats_a_shorter_inferred_one(self) -> None:
        # Breadth-first search kept the first (one-hop, inferred) path and
        # reported a fully measured cause as "inferred".
        g = DependencyGraph(
            (
                edge(C.CONTROL_NET, C.SLURMCTLD, HALTS, INF),
                edge(C.CONTROL_NET, C.SLURMD, HALTS, MEAS),
                edge(C.SLURMD, C.SLURMCTLD, HALTS, MEAS),
            )
        )
        h = {h.cause: h for h in g.causes_of(C.SLURMCTLD)}[C.CONTROL_NET]
        assert h.fully_measured
        assert h.path == (C.CONTROL_NET, C.SLURMD, C.SLURMCTLD)

    def test_evidence_beats_a_more_severe_but_weaker_path(self) -> None:
        # Both paths explain a degradation; the measured one is the better
        # supported explanation even though the inferred one would halt.
        g = DependencyGraph(
            (
                edge(C.CONTROL_NET, C.SLURMCTLD, HALTS, INF),
                edge(C.CONTROL_NET, C.SLURMD, DEGRADES, MEAS),
                edge(C.SLURMD, C.SLURMCTLD, DEGRADES, MEAS),
            )
        )
        h = {h.cause: h for h in g.causes_of(C.SLURMCTLD, severity=DEGRADES)}[C.CONTROL_NET]
        assert h.weakest is MEAS

    def test_max_hops_zero_returns_nothing(self) -> None:
        # Off by one before: max_hops=0 returned a one-hop hypothesis.
        assert GRAPH.causes_of(C.SLURMD, max_hops=0) == []

    def test_max_hops_one_returns_only_direct_causes(self) -> None:
        hops = {h.hops for h in GRAPH.causes_of(C.SLURMCTLD, severity=DEGRADES, max_hops=1)}
        assert hops == {1}

    def test_max_hops_is_inclusive(self) -> None:
        hops = {h.hops for h in GRAPH.causes_of(C.SLURMCTLD, severity=DEGRADES, max_hops=2)}
        assert hops == {1, 2}


class TestEvidenceDiscipline:
    """Every edge must say how it came to be believed."""

    @pytest.mark.parametrize("e", EDGES, ids=[f"{e.source}->{e.target}" for e in EDGES])
    def test_every_edge_cites_its_evidence(self, e: Edge) -> None:
        assert e.evidence.note.strip(), "an unexplained edge is an unfalsifiable one"

    @pytest.mark.parametrize("e", EDGES, ids=[f"{e.source}->{e.target}" for e in EDGES])
    def test_causal_edges_describe_a_symptom(self, e: Edge) -> None:
        if e.causal:
            assert e.symptom.strip()

    @pytest.mark.parametrize("e", EDGES, ids=[f"{e.source}->{e.target}" for e in EDGES])
    def test_measured_edges_name_their_run(self, e: Edge) -> None:
        # Which run, on which version. The benchmark test checks the scenario
        # really is marked measured.
        if e.evidence.is_measured:
            assert e.evidence.scenario
            assert e.evidence.slurm_version

    @pytest.mark.parametrize("e", EDGES, ids=[f"{e.source}->{e.target}" for e in EDGES])
    def test_documented_edges_cite_the_documentation(self, e: Edge) -> None:
        # DOCUMENTED claims a page states the behaviour; four edges once
        # carried the label with nothing cited. If no page can be named, the
        # honest label is INFERRED.
        if e.evidence.confidence is DOC:
            assert e.evidence.sources, "a documented edge must name its documentation"
            assert all(url.startswith("https://") for url in e.evidence.sources)

    def test_the_state_save_edge_claims_only_what_s06_recorded(self) -> None:
        # S06's baseline job completed before the chmod, so no running job was
        # observed during the fault; the edge once said running jobs were
        # "untouched".
        e = GRAPH.edge(C.STATE_SAVE, C.SLURMCTLD)
        assert e is not None
        assert "untouched" not in e.evidence.note
        assert any("already running" in item for item in e.evidence.untested)
        assert e.evidence.untested_mode is not None
        assert e.evidence.untested_mode.expected is HALTS

    def test_latency_is_a_range_not_a_point(self) -> None:
        # Identical injections varied by a factor of four, so a single number
        # would imply precision the observations do not support.
        for e in EDGES:
            if e.latency_s is not None:
                low, high = e.latency_s
                assert 0 <= low <= high

    def test_no_duplicate_edges(self) -> None:
        pairs = [(e.source, e.target) for e in EDGES]
        assert len(set(pairs)) == len(pairs)

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            # What S08 measured is that this edge did not fire.
            (C.CONFIG, C.SCHEDULER),
            # S01 suspended the database; no filesystem was stalled.
            (C.SHARED_FS, C.MYSQL),
            # No benchmark run records it.
            (C.SLURMD, C.SLURMCTLD),
        ],
    )
    def test_edges_without_a_recorded_observation_are_not_measured(
        self, source: Component, target: Component
    ) -> None:
        e = GRAPH.edge(source, target)
        assert e is not None
        assert not e.evidence.is_measured

    def test_the_config_edge_names_real_reason_codes(self) -> None:
        # https://slurm.schedmd.com/job_reason_codes.html lists
        # AssocGrpJobsLimit etc.; "QOSMaxJobs" and "AssocMaxJobs" do not exist,
        # and an agent handed them would search for text that never appears.
        e = GRAPH.edge(C.CONFIG, C.SCHEDULER)
        assert e is not None
        words = set(e.symptom.replace(",", " ").replace("(", " ").replace(")", " ").split())
        assert "AssocGrpJobsLimit" in words
        assert not words & {"QOSMaxJobs", "AssocMaxJobs"}

    def test_measured_fraction_is_computed_from_the_edges(self) -> None:
        measured = sum(1 for e in EDGES if e.evidence.is_measured)
        assert measured / len(EDGES) == GRAPH.measured_fraction
        assert GRAPH.evidence_counts[MEAS] == measured
        assert sum(GRAPH.evidence_counts.values()) == len(EDGES)
