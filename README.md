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

Published RCA scores for LLM agents are poor — but improving fast with model
generation, and quoting a stale number would misrepresent both facts. Dated and
attributed:

| Benchmark | Model | Result | When |
|---|---|---|---|
| [OpenRCA](https://github.com/microsoft/OpenRCA) (335 failures) | Claude 3.5 + RCA-agent | 11.34% | ICLR'25 |
| OpenRCA | Claude Opus 4.5 | 90/335 = **27%** | [Opus 4.6 system card](https://www.anthropic.com/news/claude-opus-4-6) |
| OpenRCA | Claude Opus 4.6 | 117/335 = **35%** | Feb 2026 |
| [ORCA-bench](https://arxiv.org/abs/2607.28545) (884 tasks) | Claude Sonnet 4.6 | 30.6% strict accuracy | Jul 2026 |
| ORCA-bench | GPT-5.5 | **48.8%** RCA depth | Jul 2026 |

**Read the trend, not the floor.** OpenRCA went 11% → 27% → 35% across three
Claude generations on an unchanged task set. Still, the best current numbers
leave **roughly two thirds of incidents misdiagnosed**, which is nowhere near
dependable for an on-call rotation.

**The structural reason is the graph.** Cloud dependency graphs are huge,
dynamic and undocumented, so an agent must infer the topology and diagnose the
fault simultaneously. HPC control planes are the opposite: the Slurm chain

```
shared filesystem → accounting DB → slurmdbd → slurmctld → scheduling
```

is small, static, documented, and identical at every Slurm site.

> **Hypothesis:** a meaningful share of what general agents lose is lost to
> graph inference, not reasoning. Write the graph down and some of it returns.

**The generational trend is why this needs an ablation rather than an opinion.**
Scores are climbing on raw capability alone, so the only interesting question is
whether an explicit graph helps *beyond* scaling — which is measurable exactly
once configurations differ by one variable at a time.

## Model

All configurations run **`claude-opus-5`**, the current strongest model, so the
ablation isolates architecture rather than model choice. Every config uses the
same model and the same telemetry; only the layer under test changes.

Two consequences worth stating up front:

- **Config A is not directly comparable to the published numbers above.** Those
  used different models, harnesses and task sets. A is a baseline *within this
  ablation*, not a reproduction of anyone else's result, and treating it as one
  would be the easiest way to manufacture a flattering headline.
- **The model will move again.** Any absolute score here has a shelf life
  measured in months; the *differences between configurations* on a fixed model
  are the durable finding.

## What exists today

Phases 1–2 are built and tested. The LLM configurations (A–E) need an API key
and are next.

| Layer | Status |
|---|---|
| **Dependency graph** (`csa/graph.py`) | **built** — 13 edges, 62% measured on a live cluster |
| **Read-only tool surface** (`csa/mcp/readonly.py`) | **built** — enforced in code, 30+ adversarial tests |
| Supervisor + specialists | next (needs `ANTHROPIC_API_KEY`) |
| Calibrated abstention | after |
| Blast-radius guardrails + verifier | after |

```bash
csa causes slurm.scheduler        # rank what could explain a symptom
csa tools                         # the read-only surface
csa check scontrol update NodeName=ALL State=DRAIN   # exits 1
```

### The graph refuses the folk model

```
$ csa causes slurm.scheduler

  symptom: slurm.scheduler (halts)

  measured   slurm.config             1 hop(s)  slurm.config → slurm.scheduler
  documented slurm.slurmctld          1 hop(s)  slurm.slurmctld → slurm.scheduler

  ruled out by measurement:
    slurm.slurmdbd    none — scheduling continues normally with accounting unavailable
```

Being able to say *"I checked the accounting path and it cannot produce this
symptom"* is worth as much as naming the cause. That line is the entire
difference between this and the architecture diagram everyone already has.

**Severity composes along a path, and does not compose transitively.** The
first version of the traversal got this wrong and a test caught it. The chain

```
db.mysql --HALTS--> slurmdbd --DEGRADES--> slurmctld --HALTS--> scheduler
```

has all its arrows present, so a naive breadth-first walk concludes the
database can stop scheduling — reintroducing the exact folk model this project
measured and refuted. A path is only as strong as its weakest link, so the
effect is the `min` over the edges: the middle link merely degrades the
controller, and a degraded controller keeps scheduling.

### Read-only is a control, not a request

A prompt saying "only use read-only commands" fails open. The allowlist lives
in `guard()`, which every execution path calls, and the tests drive it with the
commands an agent would actually reach for at 3am — `scontrol update
NodeName=ALL State=DRAIN`, `scancel`, `sinfo; rm -rf /`, `/usr/bin/scancel`.
CI fails the build if any of them is permitted.

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
