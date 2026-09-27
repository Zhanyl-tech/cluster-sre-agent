# Changelog

## 0.2.0 — unreleased (2026-09-26)

Applies the fixes from a code audit. Nothing here was run on a Slurm
cluster or against a model: there is no cluster and no GPU in the environment
these changes were made in, and every execution test uses stand-in shell
scripts. Numbers below say which command produced them.

### Read-only guard: replaced the denylist with a strict allowlist

The audit found the first guard, which refused a list of mutating subcommands
and allowed everything else, letting state-changing commands through. A scratch
script that imported the original `guard()` (from `git archive HEAD`) and fed
it this release's test inventories found it allowed **24 of 24** commands the
audit probed (`scontrol -o shutdown`, `scontrol upd PartitionName=debug
State=DOWN`, `scontrol power down c1`, `scontrol token ...`, `sacctmgr -i
delete user x`, `sacctmgr clear stats`, `sdiag -r`, `/tmp/evil/sinfo`, a bare
`scontrol`, ...), **196 of 196** scontrol writes placed behind a harmless
leading option, and **175 of 192** abbreviations of scontrol writes, while
refusing **7 of 8** legitimate reads (node ranges such as `sinfo -n
node[01-04]`, `sacct --delimiter=|`, `sacct -e`; the eighth, `sinfo --exact`,
it allowed, which a replay of the original `guard()` confirmed again during
the second review below).

`guard()` now accepts a command only if every token fits a written-down shape:

- the tool is named bare and is one of eight read tools; any `/` in `argv[0]`
  is refused (the old guard checked the basename and then executed the full
  path it was given);
- every option is an exact token from a per-tool table whose arity was checked
  against the option parsers at Slurm tag `slurm-25-11-4-1` and the 26.05 man
  pages. Exactness matters because `getopt_long` expands `--res` to `--reset`
  and splits `-ar` into `-a -r`; both are refused. (The first pass of this
  check got `--json`/`--yaml` and two squeue spellings wrong; see the second
  review below.);
- scontrol and sacctmgr must name a full-word read subcommand (scontrol:
  `show ping version completing errnumstr pidinfo getaddrs listpids listjobs
  liststeps`; sacctmgr: `show list ping version`). Both tools match a command
  by prefix down to a minimum length set per command in the 25.11.4 source:
  `xstrncasecmp(tag, "update", MAX(tag_len, 1))` in scontrol.c (so `scontrol
  u` runs update) and `xstrncasecmp(argv[0], "shutdown", MAX(command_len, 4))`
  in sacctmgr.c (so `sacctmgr shutd` shuts down). scontrol demands the full
  word for `shutdown` and `takeover` (`MAX(tag_len, 8)`), so `scontrol shutd`
  is an "invalid keyword" there, not a shutdown, but most commands accept
  prefixes and only full words are safe. The audit had left "does sacctmgr
  accept abbreviations?" unverified; the source shows it does;
- a bare scontrol or sacctmgr (interactive mode) is refused;
- value options of scontrol/sacctmgr must be attached (`--clusters=c1`), so no
  token can be a value to this parser and the subcommand to the tool;
- `scontrol show` takes full entity names only (`bbstat`/`dwstat` excluded
  because they pass arguments to an external tool; operands must start with a
  letter or digit, which keeps `show hostlist` from reading a file or stdin);
- `sacctmgr show|list` refuses `runawayjobs` and its source aliases (it offers
  to fix what it lists), `job`, `coordinator`, and the `set` keyword; `-i` is
  refused everywhere;
- `sdiag -r/--reset` and every abbreviation or combination of it is refused;
  `sacct -B/--batch-script`, `--env-vars` (job scripts and environments often
  carry credentials) and `-f/--file` are excluded; `--iterate` is excluded
  where it exists (it never returns);
- the shell-metacharacter blacklist is gone (argv never meets a shell). Values
  and operands are checked against narrow character shapes instead, and
  control characters are refused, so Slurm hostlists (`gpu[001-008]`) and
  delimiters (`|`) now work.

`run()` now:

- resolves the tool to an **absolute path** only from trusted directories
  (`CSA_SLURM_BIN`, default `/usr/bin:/usr/local/bin`), never `$PATH`, and
  refuses relative entries and binaries (or their directories) writable by
  group or others (`UntrustedExecutable`, a `ToolDenied` subclass, so callers
  fail safe);
- passes `stdin=subprocess.DEVNULL`. The audit showed a fake interactive
  scontrol "executing" `update NodeName=ALL State=DRAIN` read from the parent's
  stdin; a regression test repeats that probe and asserts the child reads EOF;
- caps returned output (`max_output_bytes`, 64 KiB per stream by default) and
  reports `stdout_bytes`, `stderr_bytes` and `truncated`. The audit also
  suggested requiring a time window for sacct; not done, because sacct's
  documented default window is already "00:00:00 of the current day" unless
  `-s` or `-j` is given, and the cap bounds what reaches the agent either way.
  The child's full output is still read into memory before capping.

Tests (`tests/test_readonly.py`) cover every scontrol and sacctmgr command on
the man pages plus the extra spellings in the 25.11.4 command matchers
(`gethost`, `hash`, `hash_file`, `callerid`, `reboot_nodes`, `holdu`,
`rollup`, ...), every abbreviation of every write, writes behind leading and
trailing options, upper-case spellings, every sdiag reset spelling, and
execution through stand-in binaries (argv passed verbatim, `$PATH` ignored,
non-zero exit, timeout with partial output, output cap, closed stdin,
untrusted binaries and symlinks). `tool_manifest()` now emits MCP-shaped
entries (`inputSchema`, `annotations.readOnlyHint`, project data under a
reverse-DNS `_meta` prefix), checked against MCP schema revision 2026-07-28.
There is still no MCP server; the package docstring and README now say so.

### Dependency graph: labels, the refutation, and traversal

- **Relabelled three edges that had no recorded measurement** (7 measured
  edges became 4 of 12, 33%):
  - `slurm.config → slurm.scheduler`: MEASURED → DOCUMENTED. What S08 measured
    is that the edge does *not* fire with `AccountingStorageEnforce=none`.
    Cites slurm.conf and the job reason codes page.
  - `storage.shared_fs → db.mysql`: MEASURED → INFERRED. S01 froze the
    database container (`docker pause`); no filesystem was stalled. (The audit also offered a new
    EMULATED level; not added.)
  - `slurm.slurmd → slurm.slurmctld`: MEASURED → DOCUMENTED rather than the
    audit's suggested INFERRED, because slurm.conf documents it
    (`SlurmdTimeout`, default 300 s, sets the node DOWN; `ReturnToService=0`
    keeps it down). The symptom said the node *drains*; the documentation says
    DOWN, so the symptom now says DOWN. The "after pod replacement" observation
    had no recorded run and is no longer claimed.
- **The central refutation edge was overstated.** `slurm.slurmdbd →
  slurm.scheduler` said NONE, but the S01 run it cites recorded job start
  latency degrading to tens of seconds. It now says DEGRADES, scoped to the
  measured regime, with the untested conditions recorded as data
  (`Evidence.untested`): accounting-queue saturation (the queue peaked at 6
  against a documented MaxDBDMsgs floor of 10000),
  `AccountingStorageEnforce=associations|limits`, and longer stalls. Traversal
  from a scheduling *halt* still never blames mysql, slurmdbd or shared_fs.
- The config edge's symptom named reason codes that do not exist
  (`QOSMaxJobs`, `AssocMaxJobs`); it now names `AssocGrpJobsLimit`,
  `AssocMaxJobsLimit`, `QOSGrpJobsLimit` from
  https://slurm.schedmd.com/job_reason_codes.html.
- The slurmdbd → slurmctld symptom said the controller *logs* queue growth; S01
  records that slurmctld logs no error, so it now names sdiag.
- `Evidence` gained `scenario`, `slurm_version`, `untested` and `sources`.
- **`causes_of` now considers every simple path** and reports, per cause, the
  best-evidenced one (strongest weakest-link evidence, then fewest hops).
  Breadth-first search had reported a fully measured cause as "inferred". The
  audit suggested ranking by effect first; evidence comes first here because
  any path at or above the requested severity explains the symptom, and
  effect-first would reintroduce the same bug when effects differ (a test
  covers it).
- **`max_hops` was off by one** (`max_hops=0` returned a one-hop hypothesis).
- **`refutations_for(symptom, severity)`** replaces the severity-blind version,
  which made `csa causes slurm.scheduler --severity degrades` list slurmdbd as a
  cause *and* as "ruled out by measurement". A component is now ruled out only
  if every path to the symptom is capped below the severity; a path's
  refutation is as strong as its strongest capping edge, and the component's is
  as strong as its weakest path. This departs from the audit's suggestion
  ("measured only when every edge on the path is measured"), because edges
  between the cause and the cap cannot weaken a refutation (if they are wrong,
  the component's influence is smaller), while requiring every path to be
  closed is what the logic needs. Refutations are not hop-limited, so a long
  producing path can never turn a cause into a "refutation". The CLI prints
  the caveats of every measured cap under "not covered by those measurements"
  (as first shipped it printed only the limiting edge's; fixed in the second
  review), and labels the list "within this graph".
- `csa stats` prints edge counts computed from `csa.graph.EDGES`.

### Benchmark agreement is now enforced, against a pinned snapshot

The module docstring claimed the build fails if the graph and slurm-rca-bench
disagree; the tests never read the benchmark. `tests/fixtures/slurm_rca_bench`
is now a verbatim, SHA-256-checked copy of the scenarios and `spec.py` at
commit `469ea760de677295a833f709b7713f06e36abff2`, made by
`scripts/sync_bench_snapshot.py` with `git show` (never the working tree).
`tests/test_benchmark_agreement.py` checks the vocabulary against the vendored
`NODES` (read with `ast`, never imported), that every chain hop is a causal
edge, that each chain's root is found from its last node, that every MEASURED
edge names a scenario recorded as `measured: true` on the claimed Slurm
version, and that no zero-credit answer of a *measured* scenario is offered as
a cause. That last check is deliberately not applied to designed-only
scenarios: S02 gives `storage.shared_fs` zero because its telemetry shows
healthy storage, which is scenario evidence, not topology. The build does not
follow the benchmark automatically; `make bench-drift` reports drift, and
`make bench-agree` (second review) runs the checks against the benchmark's
working tree.

### README and docs

- Graph numbers corrected from "13 edges, 62% measured" to "12 edges, 4
  measured (33%)", and a test now holds every such phrase to the code; the
  `csa causes slurm.scheduler` transcript is compared with real output.
- "Identical at every Slurm site" and "it does not happen" are scoped to what
  was measured.
- Model: "the current strongest model" removed; `claude-opus-5` is described as
  pinned (2026-08-08) for reproducibility, exposed as `csa.MODEL`, and the
  results table gained a Model column.
- Thesis table: the source link now points at the Opus 4.6 system card rather
  than the announcement post. The card reports both the 3-run average scores
  (26.9% for Opus 4.5, 34.9% for Opus 4.6, "run on the author's agent
  harness", Table 2.6.A) and full-identification counts ("117 of 335 cases
  (35%), up from 90 (27%) for Opus 4.5"), both on page 21. The table quotes the
  averages; the "roughly two thirds not fully diagnosed" sentence quotes the
  counts. (This entry first said the card did not report 90/335 and 117/335;
  it does, and the old README had quoted them correctly.) Whether that harness
  matches the OpenRCA paper's 11.34% RCA-agent is not stated (unverified), so
  the "11% → 27% → 35%" trend line is now qualified.
  ORCA-bench is "884 incident tasks, of 1,079", and its Claude Fable 5 figure
  on the 32-task Verified subset is quoted from the paper (arXiv 2607.28545v2).
- Config A was called both "comparable to published numbers" and "not
  directly comparable"; it is now a within-ablation baseline everywhere, and
  its shell is specified to go through the same `guard()` (A and B differ in
  packaging, not permissions).
- Added the threat to validity that C's graph shares measurements with S01/S06
  ground truth, and that no frozen analysis plan exists yet.
- Phase numbering made consistent; "10 scenarios" is now "10 scenarios, 2 with
  measured chains".
- Components are described as built only where built: no MCP server, no agent.
  (The package description and the README's first sentence still said
  otherwise until the second review.)

### Packaging, CI, tooling

- `pyyaml` moved from runtime to the dev extra (only tests use it). The `llm`
  extra (`anthropic>=0.40`, `mcp>=1.2`, never imported) was removed rather than
  kept as an untested range.
- `uv.lock` added. CI: `permissions: contents: read`, actions pinned by commit
  SHA (`actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.2.0, SHAs resolved
  with `git ls-remote`), Python 3.13 added, `uv sync --locked`, branch coverage
  with `fail_under = 95`, and the CLI smoke list moved to
  `scripts/smoke-guard.sh` (now 33 writes, 7 reads; 22 before the second
  review added every audited command verbatim).
- `make install` prefers `uv sync --locked`; without uv it checks for Python
  >= 3.11 and bootstraps pip with `ensurepip` (it failed in pip-less uv venvs).
  `make check` now also runs `ruff format --check` and the smoke list.
- mypy now also checks `scripts/`; the vendored fixture is excluded from ruff
  and mypy.

### Second review: fourteen verified findings

A review of the changes above found fourteen defects that
survived verification. All are fixed here except one step that depends on
another repository (the last item). Upstream claims were re-checked for this
pass, not taken from the reviewers: the Slurm parsers were fetched again at
tag `slurm-25-11-4-1`, and the Opus 4.6 system card was downloaded again and
compared by SHA-256 with the reviewers' copy (identical).

- **A measurement no longer rules out a failure mode it did not exercise.**
  `refutations_for(slurm.scheduler)` listed `storage.state_save` as ruled out
  *by measurement*, on the strength of S06's chmod (error mode) measurement,
  although a StateSaveLocation *stall* is the open hypothesis for exactly a
  scheduling halt. An edge can now declare an `UntestedMode` expected to be
  worse than what was measured; traversal treats it as INFERRED, so
  `storage.state_save` is now an *inferred* cause of a halt (named with its
  untested mode in `csa causes`) and not a refutation. A mode expected to stay
  below the severity still caps, but only at INFERRED. As a side effect the
  graph has its first (inferred) cause that halts slurmctld.
- **Every cap a refutation rests on is reported.** `Refutation.caps` carries
  the strongest cap of every path, and the CLI prints all their caveats. For a
  scheduling halt that adds `slurm.slurmdbd → slurm.slurmctld`.
- **The S06 edge claims only what S06 recorded.** It said running jobs were
  untouched; S06's baseline job completed before the chmod and no job ran
  during the fault. The note is narrowed, "jobs already running" is listed as
  untested, and the latency range now ends at 12 s, the first (already
  rejected) submission, instead of an unrecorded 5 s.
- **DOCUMENTED now requires a citation.** Four DOCUMENTED edges cited nothing.
  `slurm.slurmctld → slurm.scheduler` now cites slurmctld(8) ("accepts work
  (jobs), and allocates resources to those jobs") and the overview page (which
  also records the backup controller the graph does not model). The GPU
  device, GPU driver and fabric edges describe hardware behaviour no Slurm page
  states, so they are INFERRED. `csa stats`: documented 6 → 3, inferred 2 → 5;
  measured is unchanged (4 of 12). A test requires `sources` on every
  DOCUMENTED edge.
- **Benchmark agreement, including a change the pinned snapshot hides.** The
  sibling slurm-rca-bench checkout has uncommitted changes (0.2.0) that rename
  S01 to `S01-accounting-backend-stall` (the old id stays as a legacy alias in
  its `LEGACY_IDS`) and give `storage.shared_fs` zero credit, because the
  emulated cluster has no shared filesystem. Against that working tree this
  repo's checks failed (three MEASURED edges cited a missing id, and S01's
  zero-credit check flagged `storage.shared_fs`), while CI stayed green on the
  pinned snapshot. Now:
  - `CSA_BENCH_DIR=<checkout>` / `make bench-agree` runs the agreement tests
    against a working tree, uncommitted edits included (hash checks skipped);
  - scenario ids resolve through the benchmark's own `LEGACY_IDS` (read with
    `ast`, never imported), and a legacy citation is a failure on the pinned
    snapshot and a reported skip in preview;
  - the zero-credit check exempts offers that rest only on inference, with
    the reason in the test module and a test that documented or measured
    offers are still caught. `storage.shared_fs → db.mysql` stays INFERRED:
    it is a claim about clusters that have storage under the database, which
    the benchmark cluster does not, and the pinned S01's chain still needs it;
  - the S01 id lives in one constant, `csa.graph.S01`;
  - the `shared_fs → mysql` note now says S01 froze the database container
    with `docker pause` (the cgroup freezer, per
    https://docs.docker.com/reference/cli/docker/container/pause/), which is
    what both versions of S01's method record, not a SIGSTOP of its process
    group.

  `make bench-agree` against the sibling working tree as it stood on
  2026-09-26: every check passes, 3 skips report the pending rename, 2 skip the
  hash checks. `make bench-drift` still reports the snapshot matching
  `469ea76` (origin/main). The re-sync and the rename wait for that
  benchmark change to be committed.
- **Guard: `--json`/`--yaml` take an optional value.** Every 25.11.4 parser
  declares them `optional_argument`; the tables had them as flags, refusing the
  documented `--json=<data_parser>`. squeue's `--account`/`--partition` were
  accepted although its parser declares only `--accounts`/`--partitions`
  (getopt reaches them by prefix expansion); they are now refused and listed
  as excluded with the reason. A scratch comparison of every long option in
  `ALLOWED` against the `long_options` tables found 16 mismatches before and 0
  after.
- **Guard: the abbreviation example was wrong.** The docstring, the denial
  message and this changelog said `scontrol shutd` would run shutdown. At
  25.11.4 scontrol requires the full word for `shutdown` (`MAX(tag_len, 8)`,
  commented "require full command name"), while `update` matches from one
  letter and sacctmgr's `shutdown` from four. Each tool's denial now quotes an
  abbreviation its own matcher expands to a write (`'upd' runs update`,
  `'shutd' runs shutdown`), and a test checks the quote is a real prefix of a
  listed write and is refused. The full-word rule itself was right.
- **Resolution: symlinks could bypass the writability check.** Only the final
  file and its directory were checked, so a symlink planted in a group-writable
  trusted directory could point at a clean binary anywhere. The trusted
  directory and the directory of every symlink hop are now checked too, and a
  symlink loop is refused. Ownership and ancestor directories are still not
  checked (documented in the docstring and README).
- **Smoke list.** `scripts/smoke-guard.sh` said it included every spelling the
  audit found; eight were missing. It now lists all 25 audited commands
  verbatim (a test fails if one goes missing) plus 8 more: 33 writes, 7 reads.
- **Tests.** The timeout test asserted partial output after 0.5 s, which
  failed under load. The real-process test now asserts only the timeout, and
  the partial-output handling is tested by raising `TimeoutExpired` with
  output directly: a real stand-in that echoed before sleeping still had
  written nothing after 3 s in 21 of 24 parallel runs, so a longer timeout
  does not remove the race. `sinfo --exact` moved out of "reads the first guard
  wrongly refused" (it allowed it).
- **Docs.** The README Quickstart ran bare `csa` after `uv sync`, which does
  not put it on `PATH`; it now uses `uv run csa` and shows the activated-venv
  pip route. The package description, `csa.__doc__` and the README's opening
  sentence no longer describe a measured multi-agent system. The system-card
  statement above is corrected.

Checks for this pass (macOS, local; not the CI runner):

- `.venv/bin/python -m pytest -q`: 1039 passed. With `--cov=csa
  --cov-branch`: 97.74% line + branch.
- `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  `.venv/bin/mypy` (strict): clean.
- `TestExecution` in 24 and in 36 concurrent pytest processes (load average
  about 3 and 7 shortly after launch): all passed. The discarded 3 s variant failed 21 of
  24 at 24-way.
- `scripts/smoke-guard.sh`: passes; against a stand-in `csa` that allows
  everything it reports 33 errors and exits 1; shellcheck-clean.
- The Quickstart commands, run as written in a scratch copy: `uv run csa ...`
  works after `uv sync --extra dev` (bare `csa` is not on `PATH`), and the
  venv + pip route works with a uv-provided Python 3.11 standing in for
  `python3.11`.

### Checks run for this release (macOS, local; not the CI runner)

- `.venv/bin/python -m pytest -q`: 970 passed. With `--cov=csa --cov-branch`:
  97.53% line + branch coverage (the audit measured 69%, with the CLI at 0%).
- `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  `.venv/bin/mypy` (strict): clean.
- The CI job's steps, run in scratch copies with `uv sync --locked` on Python
  3.11.15, 3.12.13 and 3.13.13: all green.
- `scripts/smoke-guard.sh` passes, fails with 22 errors against a stand-in
  `csa` that allows everything, and is shellcheck-clean. (Second-review
  numbers are in the section below.)
- `make check` passes via the uv path, and via the pip fallback in a pip-less
  uv venv on Python 3.11.

### Not done (see the README limitations)

- Re-syncing the benchmark snapshot past slurm-rca-bench 0.2.0 and changing
  `csa.graph.S01` to `S01-accounting-backend-stall`. Both wait for that change
  to be committed upstream: the sync script reads commits, never a working
  tree, so that uncommitted edits cannot leak into the snapshot.

- Failure mode and configuration conditions on edges, causes that halt
  slurmctld, and the missing components (munge, DNS, failover, slurmrestd,
  health checks, prolog/epilog, Slinky).
- A frozen analysis plan and a graph-informed vs held-out scenario split.
- Sharing one guard with the sibling slurm-mcp repository, or renaming
  `csa.mcp` until a server exists.
- Fixing the same nonexistent reason codes in slurm-rca-bench S08 (a change to
  that repository).
