# cluster-sre-agent

An LLM agent for diagnosing Slurm control-plane incidents, **being built** as
**five ablatable configurations** so that what actually helps can be measured
instead of asserted. What exists today is its dependency graph and read-only
tool guard; the agent does not exist yet.

> **Status: the dependency graph and the read-only tool surface are built and
> tested; the LLM configurations are not.** `csa causes`, `csa check` and
> `csa stats` work today. What is *not* built is the agent loop itself
> (configs A–E) and any MCP server, so **no diagnosis accuracy has been
> measured** and the results table below stays empty until it is.
>
> The configurations were specified *before* any results existed, and the
> benchmark they will be scored against
> ([slurm-rca-bench](https://github.com/Zhanyl-tech/slurm-rca-bench)) was
> published first. That ordering makes fitting the ablation to its own
> conclusion harder; a frozen, tagged analysis plan would complete it, and that
> plan has not been written yet.

---

## The thesis

Published RCA scores for LLM agents are poor, but improving with model
generation, and quoting a stale number would misrepresent both facts. Dated and
attributed:

| Benchmark | Model | Result | Source, when |
|---|---|---|---|
| [OpenRCA](https://github.com/microsoft/OpenRCA) (335 failures) | Claude 3.5 + RCA-agent | 11.34% | OpenRCA paper, ICLR'25 |
| OpenRCA | Claude Opus 4.5 | 26.9% (3-run average) | [Opus 4.6 system card](https://www.anthropic.com/claude-opus-4-6-system-card), Feb 2026 |
| OpenRCA | Claude Opus 4.6 | **34.9%** (3-run average) | same system card, Feb 2026 |
| [ORCA-bench](https://arxiv.org/abs/2607.28545) (884 incident tasks, of 1,079) | Claude Sonnet 4.6 | 30.6% RCA accuracy (best on that metric) | ORCA-bench, Jul 2026 (v2 Aug 2026) |
| ORCA-bench | GPT-5.5 | **48.8%** RCA depth | same paper |
| ORCA-bench Verified (32 incident tasks) | Claude Fable 5 | 58.2 ± 5.8% RCA depth, 40.6 ± 8.8% RCA accuracy | same paper |

**Read the trend, with care.** On OpenRCA the Opus 4.6 system card reports
26.9% for Opus 4.5 and 34.9% for Opus 4.6, both 3-run averages that, in its
words, were "run on the author's agent harness". The 11.34% is the OpenRCA
paper's own RCA-agent with Claude 3.5. Whether the system-card runs used the
same agent configuration is not stated (unverified), so the step from 11% is not
a like-for-like comparison, while the step from 26.9% to 34.9% is within one
report. On the full task sets the best numbers still leave **roughly two thirds
of incidents not fully diagnosed**: the same card says Opus 4.6 "fully
identifies the root cause in 117 of 335 cases (35%), up from 90 (27%) for Opus
4.5", and the best RCA accuracy on ORCA-bench is 30.6% (the best subset
figure, 40.6% on 32 verified tasks, is not much better). That is nowhere near
dependable for an on-call rotation.

**The structural reason is the graph.** Cloud dependency graphs are huge,
dynamic and undocumented, so an agent must infer the topology and diagnose the
fault simultaneously. HPC control planes are much closer to the opposite: the
Slurm chain

```
shared filesystem → accounting DB → slurmdbd → slurmctld → scheduling
```

is small, changes only when it is redeployed, and is documented. It is not
identical at every site: whether a link is active can depend on configuration
(`AccountingStorageEnforce` decides whether association limits apply at all)
and on how a component fails (a StateSaveLocation that errors behaves unlike
one that stalls). Recording that is the graph's job, and it is not finished.

> **Hypothesis:** a meaningful share of what general agents lose is lost to
> graph inference, not reasoning. Write the graph down and some of it returns.

**The generational trend is why this needs an ablation rather than an opinion.**
Scores are climbing on raw capability alone, so the only interesting question is
whether an explicit graph helps *beyond* scaling, which is measurable exactly
once configurations differ by one variable at a time.

## Model

All configurations are to run **`claude-opus-5`** (`csa.MODEL`), pinned on
2026-08-08 for reproducibility, so the ablation isolates architecture rather
than model choice. It was not chosen as, and is not claimed to be, the most
capable model available; the pin exists to hold the model fixed. Every config
will use the same model and the same telemetry; only the layer under test
changes, and every results row records the model ID.

Two consequences worth stating up front:

- **Config A is not directly comparable to the published numbers above.** Those
  used different models, harnesses and task sets. A is a baseline *within this
  ablation*, not a reproduction of anyone else's result, and treating it as one
  would be the easiest way to manufacture a flattering headline.
- **The model will move again.** Any absolute score here has a shelf life
  measured in months; the *differences between configurations* on a fixed model
  are the durable finding.

## What exists today

Phase 3 of the roadmap (the graph and the read-only tool surface) is built and
tested. The LLM configurations (A–E) are not built.

| Layer | Status |
|---|---|
| **Dependency graph** (`csa/graph.py`) | **built**: 12 edges, 4 measured (33%) on the benchmark's Docker Compose cluster; the rest documented or inferred and labelled so (`csa stats`) |
| **Read-only tool surface** (`csa/mcp/readonly.py`) | **built**: an allowlisted argv grammar and an executor that runs tools by absolute path; tested against every scontrol and sacctmgr command on the man pages. No MCP server yet: this is the guard and manifest data a server would use |
| Supervisor + specialists | not built (needs `ANTHROPIC_API_KEY`) |
| Calibrated abstention | not built |
| Blast-radius guardrails + verifier | not built |

## Quickstart

No cluster and no API key. The graph and the read-only tool surface are the
parts that are built, and both run offline.

```bash
make install                      # uv sync if uv is installed, else venv + pip; provides `csa`
make graph                        # rank what could explain a scheduling symptom
make tools                        # the read-only surface
make check                        # ruff, format check, mypy --strict, pytest + coverage, CLI smoke
```

Or without make. `uv sync` installs `csa` into `.venv` but does not put it on
your `PATH`, so run it through `uv run` (or activate the venv first):

```bash
uv sync --extra dev
uv run csa causes slurm.scheduler        # rank what could explain a symptom
uv run csa causes slurm.scheduler --severity degrades
uv run csa stats                         # edge counts by evidence
uv run csa tools                         # the read-only surface
uv run csa check scontrol update NodeName=ALL State=DRAIN   # exits 1
uv run csa check scontrol -o shutdown                       # exits 1
uv run csa check sinfo -n 'node[01-04]'                     # exits 0
```

Without uv, create and activate a venv on Python >= 3.11, then install; `csa`
is on `PATH` only while the venv is active:

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
csa causes slurm.scheduler
```

The LLM configurations are not built, and no LLM dependency is declared until
code imports one.

### The graph refuses the folk model

```
$ csa causes slurm.scheduler

  symptom: slurm.scheduler (halts)

  documented slurm.config             1 hop(s)  slurm.config → slurm.scheduler
  documented slurm.slurmctld          1 hop(s)  slurm.slurmctld → slurm.scheduler
  inferred   storage.state_save       2 hop(s)  storage.state_save → slurm.slurmctld → slurm.scheduler
             only through an untested failure mode: a StateSaveLocation *stall* (writes block instead of failing), which S06's caveats name as a candidate for blocking slurmctld; S06 measured the error mode only

  ruled out at halts by measurement, within this graph:
    slurm.slurmdbd           slurm.slurmdbd → slurm.scheduler only degrades
    db.mysql                 slurm.slurmdbd → slurm.scheduler only degrades
    storage.shared_fs        slurm.slurmdbd → slurm.scheduler only degrades

  ruled out at halts by documentation or inference only:
    documented slurm.slurmd             slurm.slurmd → slurm.slurmctld only degrades
    documented fabric.interconnect      slurm.slurmd → slurm.slurmctld only degrades
    documented gpu.device               slurm.slurmd → slurm.slurmctld only degrades
    documented gpu.driver               slurm.slurmd → slurm.slurmctld only degrades
    inferred   network.control_plane    network.control_plane → slurm.slurmctld only degrades

  not covered by those measurements:
    slurm.slurmdbd → slurm.scheduler:
      - accounting-queue saturation: the DBD agent queue peaked at 6, while slurm.conf documents a MaxDBDMsgs floor of 10000
      - AccountingStorageEnforce=associations or limits (S08 found the benchmark cluster set to none)
      - stalls longer than the roughly fifteen minutes observed
    slurm.slurmdbd → slurm.slurmctld:
      - accounting-queue saturation: the DBD agent queue peaked at 6, while slurm.conf documents a MaxDBDMsgs floor of 10000
```

(A test compares this block with the command's real output.)

Being able to say *"I checked the accounting path and it cannot produce this
symptom"* is worth as much as naming the cause. A component is ruled out when
every path from it to the symptom passes through a link too weak to produce
what was reported; "by measurement" means every such path is closed by a
measured link. The last section lists the untested conditions of every
measured link those refutations rest on, one per path, so it says where each
measurement stopped: the refutation of the accounting path holds for the
regime S01 measured, not for a saturated accounting queue. "Within this graph"
is literal: a missing edge is a missing hypothesis, not a refutation.

A measurement covers only the failure mode it exercised. S06 measured a
StateSaveLocation that *errors*, which only degrades the controller; one that
*stalls* is expected to block it and has not been measured. So
`storage.state_save` is not ruled out for a halt: it is offered as an
*inferred* cause, with the untested mode named on the next line. (It used to
be listed as ruled out by measurement.)

Ask for what you actually saw. At `--severity degrades` the same command lists
`slurm.slurmdbd` and `db.mysql` as *measured causes*, because the stall did slow
job starts, and rules nothing out.

**Severity composes along a path, and does not compose transitively.** The
first version of the traversal got this wrong and a test caught it. The chain

```
db.mysql --HALTS--> slurmdbd --DEGRADES--> slurmctld --HALTS--> scheduler
```

has all its arrows present, so a naive breadth-first walk concludes the
database can stop scheduling, reintroducing the exact folk model this project
measured and refuted. A path is only as strong as its weakest link, so the
effect is the `min` over the edges: the middle link merely degrades the
controller, and a degraded controller keeps scheduling.

### Read-only is a control, not a request

A prompt saying "only use read-only commands" fails open. The control lives in
`guard()`, which every execution path calls, and it is an **allowlist**. The
first version was a denylist of known-bad subcommands, and an audit found
`scontrol -o shutdown`, `scontrol upd PartitionName=debug State=DOWN` (scontrol
expands `upd` to `update`), `sacctmgr -i delete user x`, `sdiag -r`,
`/tmp/evil/sinfo` and a bare `scontrol` (interactive mode reads commands from
stdin) all getting through. Now a command passes only if:

- the tool is named bare, never as a path, and is one of eight read tools;
- every option is spelled exactly as in that tool's option parser (Slurm
  25.11.4 source), with the arity the parser gives it, so `--res` is not taken
  for `--reset` and `-ar` is not split into `-a -r`. Where the man page differs
  the parser wins: `--json=<data_parser>` is accepted, and squeue's
  `--account`/`--partition` are refused in favour of `--accounts`/`--partitions`;
- scontrol and sacctmgr name a full-word read subcommand (`show`, `ping`, ...).
  Both match commands by prefix down to a minimum length set per command:
  `scontrol u` runs update and `sacctmgr shutd` runs shutdown, although
  scontrol itself insists on the full word for `shutdown` and `takeover`.
  `sacctmgr show runawayjobs` is excluded because it offers to fix what it lists;
- operands and values fit narrow shapes: node ranges like `gpu[001-008]` pass,
  control characters do not.

`run()` then executes the tool by absolute path from trusted directories
(`CSA_SLURM_BIN`, default `/usr/bin:/usr/local/bin`), never from `$PATH`;
refuses to run anything group or others could swap out (the trusted directory,
every directory holding a symlink on the way, and the final binary and its
directory; ownership and ancestor directories are not checked); closes stdin; reports a
timeout as data, since a command that hangs is itself evidence; and caps the
returned output (64 KiB per stream by default) with a truncation flag. The
tests cover every scontrol and sacctmgr command on the man pages and in the
25.11.4 command matchers, every abbreviation of every write, options before and
after the verb, and execution through stand-in binaries. CI also runs
`scripts/smoke-guard.sh`, which lists every command the audit found getting
through, and fails the build if any listed write is allowed.

## The ablation is the finding

Five configurations, each runnable independently via config. The comparison
between **B and C** is the experiment; everything else is context for it.

| Config | Adds | Question it answers |
|---|---|---|
| **A** | raw LLM + shell (every command still passes `guard()`) | Within-ablation baseline (not comparable to published numbers) |
| **B** | + read-only tools, named and described as an MCP server would expose them | How much do packaged tools help over a raw shell? |
| **C** | + hard-coded dependency graph | **The hypothesis.** Does writing the graph down help? |
| **D** | + multi-agent specialist split | Does splitting evidence across contexts help? |
| **E** | + calibrated abstention | Does knowing when to say "I don't know" help? |

**A and B differ in packaging, not permissions.** Config A's shell is not an
unguarded shell: every command it emits is to go through the same `guard()` and
be logged, so A cannot undo an injected fault (unpause the database) or change
the evidence mid-scenario, and the Action FP column means the same thing in
every row.

**A negative result gets published as the finding.** If C does not beat B, the
hypothesis is wrong and that is worth knowing: it would suggest the bottleneck
is reasoning over evidence rather than topology, which points at a different
research direction entirely.

**A known threat to validity: C's graph and the benchmark share
measurements.** The graph's measured edges come from the same runs that set
S01's and S06's ground truth (S01 gives `slurm.scheduler` zero credit because
of the measurement that also produced the slurmdbd → scheduler edge). A
C-over-B gain on those two scenarios would partly measure being handed the
answer key's topology. So any C-vs-B comparison has to report the
graph-informed scenarios (S01, S06) separately from the rest; the clean test is
scenarios written after the graph is frozen at a tag, ideally by someone else,
and none exist yet. Every configuration gets the same answer vocabulary (the
node set); C adds only the edges.

## Results

*Empty. Nothing has been measured.*

| Config | Model | RCA depth | Time-to-hyp | Calibration | Action FP |
|---|---|---|---|---|---|
| A | — | — | — | — | — |
| B | — | — | — | — | — |
| C | — | — | — | — | — |
| D | — | — | — | — | — |
| E | — | — | — | — | — |

First rows in Phase 4 (configs A and B); the full table after Phase 6. The
metrics are slurm-rca-bench's scoring (`slurmrca.scoring`): RCA depth, median
time to first hypothesis, Brier-score calibration, and action false-positive
rate. Runs per scenario, confidence intervals and the stopping rule belong in
the analysis plan that has not been written yet.

## What the benchmark already taught us

Before this agent exists, building the benchmark produced a result that
constrains it.

S01's original ground truth encoded a chain every Slurm operator will
recognise: storage stalls → DB blocks → slurmdbd backs up → **scheduling halts
~13 minutes later**. Measured on the benchmark's Compose cluster (Slurm
25.11.4, database suspended for fifteen minutes; S08 found the same cluster
running `AccountingStorageEnforce=none`), it did not happen: slurmctld kept
scheduling with a degraded
accounting path, jobs submitted during the stall ran to completion, and job
start latency rose to tens of seconds. That is one regime. The DBD agent queue
peaked at 6 against a `MaxDBDMsgs` limit that slurm.conf documents as at least
10000, so the queue-saturation mechanism the folk model relies on was never
exercised.

That matters here for two reasons:

1. **The dependency graph in config C must encode measured propagation, not
   folk knowledge.** A graph asserting "DB stall halts scheduling" would make
   config C *worse* than config B: confidently wrong instead of merely
   uninformed. The hypothesis is only testable if the graph is true, and only
   as far as the measurements behind it reach.
2. It is a preview of the failure mode configuration E exists to catch:
   a fluent, plausible, widely believed answer that is false.

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

1. **Data plane**: read-only MCP servers, one per source. Read-only is to be
   enforced **in the server implementation, not the prompt**. Today that means
   `guard()` and `run()` in `csa/mcp/readonly.py`, which a server would call;
   no server exists yet. A prompt that asks a model not to write is not a
   security control.
2. **Dependency graph**: the Slurm control plane as an explicit data structure
   with failure-propagation edges and latency ranges, measured where the edge is
   measured. Hard-coded and documented. The model does not infer it. This file
   is the intellectual core.
3. **Reasoning**: supervisor plus specialists, because single-agent reliability
   degrades once evidence accumulates in one context.
4. **Abstention**: calibrated confidence. "Insufficient evidence, here is what
   I ruled out" is a first-class scored output, required for the benchmark's
   undiagnosable scenarios.
5. **Guardrails**: blast-radius classification
   (`REVERSIBLE_SCOPED` / `REVERSIBLE_WIDE` / `DESTRUCTIVE`), policy-as-code,
   dry-run, immutable audit log, kill switch. **Default autonomy is RECOMMEND
   ONLY.** Hard invariant: stale or missing telemetry means refuse, never guess.
   A separate verifier agent, not the actor, confirms SLO recovery.
6. **Observability**: Langfuse tracing on the agent's own reasoning.

## Limitations

- **No diagnosis accuracy is measured yet.** The graph and the tool surface
  are tested; the agent's ability to *use* them is not.
- **Ten scenarios in the benchmark, two with measured chains** (S01, S06). That
  is not enough to rank configurations confidently; 15–20 is the target.
- **The graph is 4 of 12 edges measured (33%).** The rest are documented or
  inferred, and every edge says which, but an inferred edge is still a
  hypothesis, not a fact. Three edges were previously labelled measured with no
  recorded run behind them, and three hardware edges labelled documented with
  nothing cited; all have been relabelled (see CHANGELOG.md).
- **The benchmark snapshot is pinned, so it can lag the benchmark.** The
  agreement tests read slurm-rca-bench at one commit, so a benchmark change
  does not fail them until it is re-synced. `make bench-agree` runs the same
  checks against the sibling checkout's working tree, uncommitted edits
  included, so a contradiction shows up before that.
- **The graph expresses failure modes only crudely, and configuration
  conditions not at all.** An edge can declare one untested mode expected to be
  worse than what was measured (a StateSaveLocation that errors was measured
  to degrade the controller; one that stalls is expected to block it, not
  measured), and traversal treats that mode as inference. The `slurm.config`
  edge is inactive when `AccountingStorageEnforce=none` without the graph
  knowing. Its only cause that *halts* slurmctld itself is that untested
  StateSaveLocation stall (`csa causes slurm.slurmctld`), and it omits
  munge/auth, DNS, controller failover, slurmrestd, node health checks,
  prolog/epilog and the Kubernetes layer (Slinky).
- **Refutations are only as complete as the graph.** "Ruled out" means every
  path in this graph is capped; it cannot rule out a mechanism the graph does
  not contain.
- **The faults are emulated.** See the benchmark's limitations section.
- **One model family initially.** Config A is a within-ablation baseline; it is
  not comparable to published numbers from other harnesses and task sets.
- **No analysis plan is frozen yet.** Runs per scenario, statistics and a
  stopping rule must be written and tagged before the first run.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Benchmark harness + scenarios ([slurm-rca-bench](https://github.com/Zhanyl-tech/slurm-rca-bench)) | **done**: 10 scenarios, 2 with measured chains |
| 2 | Scoring library + degenerate baselines | **done** |
| 3 | **Dependency graph** + read-only tool surface | **done**: 12 edges, 4 measured (33%) |
| 4 | Configs A and B (raw LLM, then + tools) | next, needs `ANTHROPIC_API_KEY` |
| 5 | Config C (+ graph), **first real comparison** | |
| 6 | Configs D and E, guardrails and action | |

The graph moved earlier than originally planned, because it is the one layer
that could be built and tested without an API key, and because building it
surfaced a modelling error (severity does not compose transitively) that would
otherwise have been discovered only after it had polluted a set of results.

## License

MIT
