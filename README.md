# cluster-sre-agent

An LLM agent that diagnoses Slurm control-plane incidents, built as **five
ablatable configurations** so that what actually helps can be measured instead
of asserted.

> **Status: Phase 1 — design published, agent not built yet.** Nothing here has
> been measured. The results table below is empty and stays empty until it is
> not. Published now because the benchmark it will be scored against
> ([slurm-rca-bench](https://github.com/Zhanyl-tech/slurm-rca-bench)) is
> public, and the configurations were specified *before* any results existed.

---

## The thesis

Published RCA scores for LLM agents are poor, and consistently so:

| Benchmark | Best reported result | Scale |
|---|---|---|
| [ORCA-bench](https://arxiv.org/abs/2607.28545) | **48.8%** RCA depth (GPT-5.5) | 884 incident tasks |
| [OpenRCA](https://github.com/microsoft/OpenRCA) (ICLR'25) | **11.34%** accuracy (Claude 3.5 + RCA-agent) | 335 failures, 68 GB telemetry |

**The structural reason is the graph.** Cloud dependency graphs are huge,
dynamic and undocumented, so an agent must infer the topology and diagnose the
fault simultaneously. HPC control planes are the opposite: the Slurm chain

```
shared filesystem → accounting DB → slurmdbd → slurmctld → scheduling
```

is small, static, documented, and identical at every Slurm site.

> **Hypothesis:** most of the accuracy general agents lose is lost to graph
> inference, not reasoning. Write the graph down and much of it returns.

## The ablation is the finding

Five configurations, each runnable independently via config. The comparison
between **B and C** is the experiment; everything else is context for it.

| Config | Adds | Question it answers |
|---|---|---|
| **A** | raw LLM + shell | Baseline comparable to published numbers |
| **B** | + read-only MCP tools | How much is just tool access? |
| **C** | + hard-coded dependency graph | **The hypothesis.** Does writing the graph down help? |
| **D** | + multi-agent specialist split | Does splitting evidence across contexts help? |
| **E** | + calibrated abstention | Does knowing when to say "I don't know" help? |

**A negative result gets published as the finding.** If C does not beat B, the
hypothesis is wrong and that is worth knowing — it would suggest the bottleneck
is reasoning over evidence rather than topology, which points at a different
research direction entirely.

## Results

*Empty. Nothing has been measured.*

| Config | RCA depth | Time-to-hyp | Calibration | Action FP |
|---|---|---|---|---|
| A | — | — | — | — |
| B | — | — | — | — |
| C | — | — | — | — |
| D | — | — | — | — |
| E | — | — | — | — |

Populated in Phase 4, when the ablation first runs end to end.

## What the benchmark already taught us

Before this agent exists, building the benchmark produced a result that
constrains it.

S01's original ground truth encoded a chain every Slurm operator will
recognise: storage stalls → DB blocks → slurmdbd backs up → **scheduling halts
~13 minutes later**. Measured on a live cluster, it does not happen — slurmctld
keeps scheduling with a degraded accounting path, and jobs submitted during the
stall run to completion.

That matters here for two reasons:

1. **The dependency graph in config C must encode measured propagation, not
   folk knowledge.** A graph asserting "DB stall halts scheduling" would make
   config C *worse* than config B — confidently wrong instead of merely
   uninformed. The hypothesis is only testable if the graph is true.
2. It is a preview of the failure mode configuration E exists to catch:
   a fluent, plausible, widely-believed answer that is false.

## Architecture (planned)

```
                    ┌─────────────────────────────────────┐
                    │  supervisor                          │
                    │  traverses the graph, ranks          │
                    │  hypotheses, decides when to stop    │
                    └───┬─────────┬─────────┬──────────────┘
      ┌─────────────────┘         │         └───────────────┐
┌─────▼─────┐  ┌──────▼──────┐  ┌─▼──────────┐  ┌──────────▼─┐
│ logs      │  │ metrics     │  │ scheduler  │  │ validator  │
│ specialist│  │ specialist  │  │ state      │  │ (adversary)│
└─────┬─────┘  └──────┬──────┘  └─┬──────────┘  └──────────┬─┘
      └───────────────┴───────────┴────────────────────────┘
                            │
                 ┌──────────▼───────────┐
                 │ read-only MCP servers │  slurm · prometheus
                 │ (enforced in code)    │  dcgm · logs · storage
                 └───────────────────────┘
```

Six layers, in build order:

1. **Data plane** — read-only MCP servers, one per source. Read-only is enforced
   **in the server implementation, not the prompt**, and tested. A prompt that
   asks a model not to write is not a security control.
2. **Dependency graph** — the Slurm control plane as an explicit data structure
   with failure-propagation edges and *measured* latencies. Hard-coded and
   documented. The model does not infer it. This file is the intellectual core.
3. **Reasoning** — supervisor plus specialists, because single-agent reliability
   degrades once evidence accumulates in one context.
4. **Abstention** — calibrated confidence. "Insufficient evidence, here is what
   I ruled out" is a first-class scored output, required for the benchmark's
   undiagnosable scenarios.
5. **Guardrails** — blast-radius classification
   (`REVERSIBLE_SCOPED` / `REVERSIBLE_WIDE` / `DESTRUCTIVE`), policy-as-code,
   dry-run, immutable audit log, kill switch. **Default autonomy is RECOMMEND
   ONLY.** Hard invariant: stale or missing telemetry means refuse, never guess.
   A separate verifier agent — not the actor — confirms SLO recovery.
6. **Observability** — Langfuse tracing on the agent's own reasoning.

## Limitations

- **Nothing is measured yet.** Every claim above is a design intention.
- **Five scenarios** in the benchmark today, which is not enough to rank
  configurations. Phase 2 takes it to 15–20.
- **The faults are emulated.** See the benchmark's limitations section.
- **One model family initially.** Config A is meant to be comparable to
  published baselines, but "comparable" across different harnesses and task
  sets is a weaker claim than a like-for-like comparison.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Benchmark harness + 5 scenarios ([slurm-rca-bench](https://github.com/Zhanyl-tech/slurm-rca-bench)) | **done** |
| 2 | Scenarios to 15–20, scoring library | next |
| 3 | Configs A and B | |
| 4 | Dependency graph, config C, **first real comparison** | |
| 5 | Configs D and E | |
| 6 | Guardrails and action | |

## License

MIT
