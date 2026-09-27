"""The graph and slurm-rca-bench must not contradict each other.

Both encode the same measurements. If they diverge, one is stale, and a silent
divergence would mean the agent reasons over a world model the benchmark does
not score. These tests read a verbatim, hash-checked snapshot of the benchmark
(``tests/fixtures/slurm_rca_bench``, made by ``scripts/sync_bench_snapshot.py``
from one pinned commit), so they run offline and a changed benchmark cannot
slip in unnoticed; it has to be re-synced on purpose.

**Preview mode.** A pinned snapshot also means the tests stay green while the
benchmark changes underneath them, which is how a contradiction with an
uncommitted benchmark change once went unseen. ``CSA_BENCH_DIR=<checkout>``
(``make bench-agree``) runs the same checks against a benchmark *working
tree*, uncommitted edits included, before anything is re-synced. The hash
checks are skipped there, since a working tree has no pinned hashes.

What is checked, and what deliberately is not:

* every consecutive pair in every causal chain is a causal edge here;
* walking upstream from each chain's last node finds the chain's root;
* every MEASURED edge names a scenario whose verification block says
  ``measured: true``, on the Slurm version the edge claims. Ids are resolved
  through the benchmark's own ``LEGACY_IDS`` map when it has one (read with
  ``ast`` from ``src/slurmrca/loader.py``, never imported), and a citation of a
  legacy id is reported: a failure on the pinned snapshot, a skip in preview;
* for *measured* scenarios, no zero-credit answer is offered as a cause of the
  scenario's symptom node on evidence stronger than inference.

The last check is **not** applied to designed-only scenarios, because a zero
credit there is often scenario evidence rather than topology: S02 gives
``storage.shared_fs`` zero because that scenario's telemetry shows healthy
storage. For the same reason it does not cover offers that rest only on
inference. A measured scenario's zero credit is a fact about that run; it
contradicts the graph only where the graph claims, from measurement or
documentation, that the component produces the symptom. An INFERRED path is
already presented to the agent as an untested hypothesis. The case this
exists for: slurm-rca-bench 0.2.0 gives ``storage.shared_fs`` zero in S01
because the emulated cluster has no shared filesystem, while the graph keeps
the storage -> database edge as inference about clusters that do. For
single-link chains (S06 among the measured ones) there is no symptom node
distinct from the cause, so the check is vacuous there.
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from csa.graph import (
    EDGES,
    Component,
    Confidence,
    DependencyGraph,
    Edge,
    Evidence,
    Propagation,
)

#: A benchmark checkout to preview against instead of the pinned snapshot.
PREVIEW_ENV = "CSA_BENCH_DIR"
PINNED_SNAPSHOT = Path(__file__).parent / "fixtures" / "slurm_rca_bench"
PREVIEW = bool(os.environ.get(PREVIEW_ENV))
SNAPSHOT = Path(os.environ[PREVIEW_ENV]) if PREVIEW else PINNED_SNAPSHOT
SOURCE: dict[str, Any] = (
    {} if PREVIEW else json.loads((PINNED_SNAPSHOT / "SOURCE.json").read_text())
)
GRAPH = DependencyGraph()

pinned_only = pytest.mark.skipif(PREVIEW, reason=f"{PREVIEW_ENV} preview: no pinned hashes")


def _load_scenarios() -> dict[str, dict[str, Any]]:
    scenarios: dict[str, dict[str, Any]] = {}
    for path in sorted(SNAPSHOT.glob("scenarios/*/scenario.yaml")):
        raw = yaml.safe_load(path.read_text())
        scenarios[raw["id"]] = raw
    return scenarios


def _literal(path: Path, name: str) -> Any:
    """A module-level literal assigned to ``name`` in ``path``, read with ast."""
    for node in ast.walk(ast.parse(path.read_text())):
        targets: list[ast.expr]
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        named = any(isinstance(t, ast.Name) and t.id == name for t in targets)
        if named and node.value is not None:
            return ast.literal_eval(node.value)
    return None


def _legacy_ids() -> dict[str, str]:
    """The benchmark's map of retired scenario ids to current ones, if it has one."""
    loader = SNAPSHOT / "src" / "slurmrca" / "loader.py"
    value = _literal(loader, "LEGACY_IDS") if loader.is_file() else None
    return {str(k): str(v) for k, v in (value or {}).items()}


SCENARIOS = _load_scenarios()
LEGACY_IDS = _legacy_ids()
CHAINS = {sid: [link["node"] for link in s["causal_chain"]] for sid, s in SCENARIOS.items()}
MULTI_LINK = sorted(sid for sid, chain in CHAINS.items() if len(chain) > 1)


def _measured(scenario: dict[str, Any]) -> bool:
    # The benchmark's own default: a missing verification block means not measured.
    return bool((scenario.get("verification") or {}).get("measured", False))


def _scenario_for(sid: str | None) -> dict[str, Any] | None:
    """The scenario an edge cites, following the benchmark's legacy ids."""
    if sid is None:
        return None
    return SCENARIOS.get(sid) or SCENARIOS.get(LEGACY_IDS.get(sid, ""))


def contradicted_zero_credit(
    graph: DependencyGraph, zero: set[str], symptom: Component
) -> set[str]:
    """Zero-credit answers the graph offers as causes on more than inference."""
    offered = {h.cause.value: h for h in graph.causes_of(symptom, severity=Propagation.DEGRADES)}
    return {n for n in zero & offered.keys() if offered[n].weakest is not Confidence.INFERRED}


def _benchmark_nodes() -> set[str]:
    """The benchmark's answer vocabulary, read from spec.py without importing it."""
    value = _literal(SNAPSHOT / "src" / "slurmrca" / "spec.py", "NODES")
    assert isinstance(value, dict), "NODES not found in the benchmark's spec.py"
    return {str(k) for k in value}


class TestSnapshot:
    @pinned_only
    def test_snapshot_is_unmodified(self) -> None:
        # Hand edits to the fixture would let the tests agree with a benchmark
        # that does not exist.
        on_disk = {
            str(p.relative_to(SNAPSHOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in SNAPSHOT.rglob("*")
            if p.is_file() and p.name != "SOURCE.json"
        }
        assert on_disk == SOURCE["files"]

    @pinned_only
    def test_snapshot_names_its_commit(self) -> None:
        assert len(SOURCE["commit"]) == 40
        assert SOURCE["upstream"].startswith("https://github.com/")

    def test_there_are_scenarios_to_check(self) -> None:
        files = list(SNAPSHOT.glob("scenarios/*/scenario.yaml"))
        assert files, f"no scenarios under {SNAPSHOT}"
        assert len(SCENARIOS) == len(files), "two scenario files share an id"
        if not PREVIEW:
            assert len(files) == len([p for p in SOURCE["files"] if p.startswith("scenarios/")])
        assert MULTI_LINK, "without multi-link chains the chain checks test nothing"


class TestVocabulary:
    def test_components_are_the_benchmark_answer_vocabulary(self) -> None:
        # Component values are the benchmark's answer identifiers verbatim, so
        # a traversal result can be scored with no translation layer.
        assert {c.value for c in Component} == _benchmark_nodes() - {"unknown"}


class TestChains:
    @pytest.mark.parametrize("sid", MULTI_LINK)
    def test_every_hop_is_a_causal_edge(self, sid: str) -> None:
        chain = CHAINS[sid]
        for upstream, downstream in itertools.pairwise(chain):
            e = GRAPH.edge(Component(upstream), Component(downstream))
            assert e is not None, f"{sid}: no edge {upstream} -> {downstream}"
            assert e.causal, f"{sid}: {upstream} -> {downstream} is marked non-causal"

    @pytest.mark.parametrize("sid", MULTI_LINK)
    def test_the_root_is_found_from_the_symptom(self, sid: str) -> None:
        chain = CHAINS[sid]
        found = {
            h.cause.value
            for h in GRAPH.causes_of(Component(chain[-1]), severity=Propagation.DEGRADES)
        }
        assert chain[0] in found

    @pytest.mark.parametrize("sid", sorted(s for s in SCENARIOS if _measured(SCENARIOS[s])))
    def test_zero_credit_answers_of_measured_scenarios_are_not_offered(self, sid: str) -> None:
        # Offers resting only on inference are exempt; the module docstring
        # says why, and the next test holds the exemption to exactly that.
        zero = {c["node"] for c in SCENARIOS[sid]["scoring"] if float(c["credit"]) == 0.0}
        assert not contradicted_zero_credit(GRAPH, zero, Component(CHAINS[sid][-1]))

    def test_the_exemption_covers_inference_only(self) -> None:
        C, P = Component, Propagation

        def graph(first_hop: Confidence) -> DependencyGraph:
            return DependencyGraph(
                (
                    Edge(C.SHARED_FS, C.MYSQL, P.HALTS, Evidence(first_hop, "t"), "t"),
                    Edge(C.MYSQL, C.SLURMDBD, P.HALTS, Evidence(Confidence.MEASURED, "t"), "t"),
                )
            )

        zero = {C.SHARED_FS.value}
        assert contradicted_zero_credit(graph(Confidence.INFERRED), zero, C.SLURMDBD) == set()
        for stronger in (Confidence.DOCUMENTED, Confidence.MEASURED):
            assert contradicted_zero_credit(graph(stronger), zero, C.SLURMDBD) == zero


MEASURED_EDGES = [e for e in EDGES if e.evidence.is_measured]


class TestMeasuredEdges:
    @pytest.mark.parametrize(
        "e", MEASURED_EDGES, ids=[f"{e.source}->{e.target}" for e in MEASURED_EDGES]
    )
    def test_measured_means_a_measured_scenario(self, e: Edge) -> None:
        # "MEASURED" is a claim about a recorded run. If the benchmark does not
        # record it as measured, neither may the graph.
        scenario = _scenario_for(e.evidence.scenario)
        assert scenario is not None, f"{e.evidence.scenario!r} is not a benchmark scenario"
        assert _measured(scenario)

    @pytest.mark.parametrize(
        "e", MEASURED_EDGES, ids=[f"{e.source}->{e.target}" for e in MEASURED_EDGES]
    )
    def test_measured_edges_cite_current_ids(self, e: Edge) -> None:
        sid = e.evidence.scenario or ""
        if sid in SCENARIOS:
            return
        current = LEGACY_IDS.get(sid)
        message = f"{sid!r} is a legacy id for {current!r}; update csa.graph"
        if current is not None and PREVIEW:
            pytest.skip(f"rename pending re-sync: {message}")
        pytest.fail(message if current is not None else f"{sid!r} is not a benchmark scenario")

    @pytest.mark.parametrize(
        "e", MEASURED_EDGES, ids=[f"{e.source}->{e.target}" for e in MEASURED_EDGES]
    )
    def test_the_version_is_the_one_the_run_records(self, e: Edge) -> None:
        scenario = _scenario_for(e.evidence.scenario)
        assert scenario is not None
        method = scenario["verification"]["method"]
        assert e.evidence.slurm_version
        assert f"Slurm {e.evidence.slurm_version}" in " ".join(method.split())

    def test_the_s01_measurement_is_what_the_refutation_cites(self) -> None:
        # S01 is the run that refuted the folk model. Its caveats record a real
        # job-start-latency degradation, which is why the edge says DEGRADES.
        e = GRAPH.edge(Component.SLURMDBD, Component.SCHEDULER)
        assert e is not None
        scenario = _scenario_for(e.evidence.scenario)
        assert scenario is not None
        assert str(scenario["id"]).startswith("S01-")
        caveats = " ".join(scenario["verification"]["caveats"].split())
        assert "latency did degrade" in caveats
        assert e.propagation is Propagation.DEGRADES
