"""The read-only surface must hold against real mutating commands.

These drive :func:`guard` with the actual strings an agent would emit to drain
a node or cancel a job, including every spelling an audit found getting
through the first, denylist-based version. Asserting that a *prompt* forbids
something proves nothing; the control has to be the code path every execution
goes through.

Command lists are taken from the Slurm man pages (Slurm 26.05, read 2026-09-26)
and from the command matchers in the source at tag slurm-25-11-4-1, the version
the benchmark cluster runs:

* https://slurm.schedmd.com/scontrol.html and src/scontrol/scontrol.c
* https://slurm.schedmd.com/sacctmgr.html and src/sacctmgr/sacctmgr.c
* https://slurm.schedmd.com/sdiag.html and src/sdiag/opts.c

No Slurm is installed where these run. Execution is tested with small shell
scripts standing in for the binaries, in a temporary trusted directory.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from csa.mcp.readonly import (
    ALLOWED,
    DEFAULT_TRUSTED_DIRS,
    TRUSTED_DIRS_ENV,
    Arity,
    ToolDenied,
    UntrustedExecutable,
    _follow_links,
    guard,
    resolve_executable,
    run,
    tool_manifest,
    trusted_dirs,
)

SMOKE_SCRIPT = Path(__file__).parent.parent / "scripts" / "smoke-guard.sh"

# ── command inventories ────────────────────────────────────────────────────

#: Every scontrol command on the man page's COMMANDS and INTERACTIVE COMMANDS
#: lists, plus the extra spellings its matcher accepts in scontrol.c.
SCONTROL_COMMANDS = {
    # man page, COMMANDS
    "cancel_reboot", "create", "completing", "delete", "errnumstr",
    "fsdampeningfactor", "getaddrs", "getent", "help", "hold", "listjobs",
    "listpids", "liststeps", "notify", "pidinfo", "ping", "power", "reboot",
    "reconfigure", "release", "requeue", "requeuehold", "resume",
    "schedloglevel", "setdebug", "setdebugflags", "show", "shutdown", "suspend",
    "takeover", "top", "token", "uhold", "update", "version", "wait_job",
    "write",
    # man page, INTERACTIVE COMMANDS (not valid on the command line; refused anyway)
    "all", "cluster", "details", "exit", "hide", "oneliner", "quiet", "quit",
    "verbose",
    # scontrol.c at slurm-25-11-4-1: aliases and undocumented commands
    "errnostr", "gethost", "hash", "hash_file", "holdu", "reboot_nodes",
    "callerid", "fairsharedampeningfactor",
}  # fmt: skip
SCONTROL_READS = {
    "show", "ping", "version", "completing", "errnumstr", "pidinfo", "getaddrs",
    "listpids", "listjobs", "liststeps",
}  # fmt: skip
#: The ones that change cluster state, block, write files or mint credentials.
SCONTROL_WRITES = {
    "cancel_reboot", "create", "delete", "fsdampeningfactor",
    "fairsharedampeningfactor", "hold", "holdu", "notify", "power", "reboot",
    "reboot_nodes", "reconfigure", "release", "requeue", "requeuehold",
    "resume", "schedloglevel", "setdebug", "setdebugflags", "shutdown",
    "suspend", "takeover", "top", "token", "uhold", "update", "wait_job",
    "write",
}  # fmt: skip

#: Every sacctmgr command on the man page, plus what sacctmgr.c also matches.
SACCTMGR_COMMANDS = {
    "add", "archive", "clear", "create", "delete", "dump", "help", "list",
    "load", "modify", "ping", "reconfigure", "remove", "show", "shutdown",
    "version",
    # interactive-mode commands
    "exit", "quiet", "quit", "verbose",
    # sacctmgr.c at slurm-25-11-4-1
    "associations", "oneliner", "readonly", "rollup", "update",
}  # fmt: skip
SACCTMGR_READS = {"show", "list", "ping", "version"}
SACCTMGR_WRITES = {
    "add", "archive", "clear", "create", "delete", "dump", "load", "modify",
    "reconfigure", "remove", "rollup", "shutdown", "update",
}  # fmt: skip

#: Options that are harmless on their own; prefixed to every write below,
#: because the first guard only looked at argv[1].
LEADING_OPTIONS = [["-o"], ["-v"], ["-d"], ["-Q"], ["-a"], ["--details"], ["--quiet"]]


def _prefixes(word: str) -> list[str]:
    """Every abbreviation of ``word``.

    Both tools match a command by prefix down to a minimum length that differs
    per command (1 for scontrol's update, the full word for its shutdown), so
    testing every prefix covers whatever the minimum is.
    """
    return [word[:n] for n in range(1, len(word) + 1)]


class TestInventory:
    """The allowlist is exactly what these tests say it is."""

    def test_scontrol_reads_are_exactly_the_allowlist(self) -> None:
        verbs = ALLOWED["scontrol"].verbs
        assert verbs is not None
        assert set(verbs) == SCONTROL_READS

    def test_sacctmgr_reads_are_exactly_the_allowlist(self) -> None:
        verbs = ALLOWED["sacctmgr"].verbs
        assert verbs is not None
        assert set(verbs) == SACCTMGR_READS

    def test_every_listed_write_is_a_listed_command(self) -> None:
        assert SCONTROL_WRITES <= SCONTROL_COMMANDS
        assert SACCTMGR_WRITES <= SACCTMGR_COMMANDS

    def test_no_abbreviation_of_a_write_is_an_allowed_word(self) -> None:
        # If one were, a full allowed word would also be a valid abbreviation
        # of a write, and "exact words only" would not be enough.
        for write in SCONTROL_WRITES:
            assert not set(_prefixes(write)) & SCONTROL_READS, write
        for write in SACCTMGR_WRITES:
            assert not set(_prefixes(write)) & SACCTMGR_READS, write


# ── permitted ──────────────────────────────────────────────────────────────


class TestPermitted:
    @pytest.mark.parametrize(
        "command",
        [
            ["sinfo"],
            ["sinfo", "-N", "--noheader", "-o", "%N %t"],
            ["sinfo", "-R", "-l"],
            ["sinfo", "-t", "drain,down"],
            ["sinfo", "-t", "~idle"],
            ["sinfo", "--exact"],
            ["sinfo", "--sort=+P,-m"],
            ["squeue", "-h", "-o", "%T"],
            ["squeue", "--format=%.18i %.9P %.8j %.8u %.2t %.19S %.6D %20Y %R"],
            ["squeue", "--jobs=1008", "--steps"],
            ["squeue", "-t", "PENDING", "--start"],
            ["sacct", "-n", "-X"],
            ["sacct", "-S", "now-1hour", "-E", "now", "-o", "jobid,state,exitcode"],
            ["sacct", "--starttime=2026-09-26T10:00:00", "-a"],
            ["sdiag"],
            ["sdiag", "--all", "--json"],
            ["sprio", "-l"],
            ["sprio", "--jobs=1,2", "-w"],
            ["sshare", "-a"],
            ["scontrol", "show", "config"],
            ["scontrol", "show", "node", "c1"],
            ["scontrol", "show", "node=c1"],
            ["scontrol", "-o", "show", "job", "1234"],
            ["scontrol", "show", "job", "1234", "-d"],
            ["scontrol", "SHOW", "NODE", "c1"],
            ["scontrol", "--clusters=c1", "show", "partition", "debug"],
            ["scontrol", "show", "assoc_mgr", "users=alice", "flags=assoc"],
            ["scontrol", "show", "topology", "unit=s1"],
            ["scontrol", "ping"],
            ["scontrol", "version"],
            ["scontrol", "errnumstr", "2002"],
            ["scontrol", "completing"],
            ["scontrol", "pidinfo", "4242"],
            ["scontrol", "getaddrs", "c[1-2]"],
            ["scontrol", "listpids", "1234.0", "c1"],
            ["scontrol", "listjobs"],
            ["scontrol", "liststeps", "c1"],
            ["sacctmgr", "show", "assoc"],
            ["sacctmgr", "list", "associations", "where", "users=alice", "format=account,grpjobs"],
            ["sacctmgr", "-P", "-n", "show", "qos"],
            ["sacctmgr", "show", "stats"],
            ["sacctmgr", "show", "events", "start=now-1day", "all_clusters"],
            ["sacctmgr", "ping"],
        ],
    )
    def test_read_commands_pass(self, command: list[str]) -> None:
        guard(command)

    @pytest.mark.parametrize(
        "command",
        [
            # The first guard refused Slurm hostlist syntax as "shell
            # metacharacters", so the agent could not name a node range.
            ["sinfo", "-n", "node[01-04]"],
            ["squeue", "-w", "c[1-4]"],
            ["scontrol", "show", "node", "gpu[001-008]"],
            ["scontrol", "show", "hostnames", "c[1-4]"],
            ["sacct", "--delimiter=|", "-p"],
            # ...and harmless read flags that collided with its global list
            # ("-e"; it did not list "--exact", which it allowed).
            ["sacct", "-e"],
            ["sinfo", "-e"],
        ],
    )
    def test_reads_the_first_guard_wrongly_refused(self, command: list[str]) -> None:
        guard(command)

    @pytest.mark.parametrize(
        "tool", sorted(t for t, spec in ALLOWED.items() if "--json" in spec.options)
    )
    def test_json_and_yaml_take_an_optional_value(self, tool: str) -> None:
        # optional_argument in every 25.11.4 parser that has them: the man
        # pages document --json=<data_parser>, which a FLAG entry refused.
        options = ALLOWED[tool].options
        assert options["--json"] is options["--yaml"] is Arity.OPTIONAL
        prefix = ["show", "config"] if tool == "scontrol" else ["show", "qos"]
        rest = prefix if ALLOWED[tool].verbs is not None else []
        guard([tool, "--json", *rest])
        guard([tool, "--json=v0.0.42", *rest])
        guard([tool, "--yaml=v0.0.42", *rest])

    def test_squeue_long_options_are_the_parsers_spellings(self) -> None:
        # squeue's parser declares --accounts/--partitions only; the man
        # page's --account/--partition reach them by prefix expansion.
        guard(["squeue", "--accounts=a", "--partitions=debug", "-A", "a", "-p", "debug"])
        for spelling in ("--account=a", "--partition=debug"):
            with pytest.raises(ToolDenied):
                guard(["squeue", spelling])


# ── what the audit found getting through ────────────────────────────────────


#: Every command the audit probed as ALLOWED by the first guard, as the smoke
#: script spells it. ``scontrol shutd`` is here because the first guard let it
#: through, not because scontrol would run it: scontrol accepts ``shutdown``
#: only as the full word (``scontrol upd`` is the dangerous abbreviation).
AUDITED_BYPASSES = [
    "scontrol -o shutdown",
    "scontrol -v reconfigure",
    "scontrol -d delete PartitionName=debug",
    "scontrol -Q update PartitionName=debug MaxNodes=0",
    "scontrol -M c1 create reservation starttime=now duration=60 flags=maint nodes=ALL",
    "scontrol --clusters=c1 create reservation starttime=now flags=maint nodes=ALL",
    "scontrol top 1234",
    "scontrol uhold 1234",
    "scontrol schedloglevel 1",
    "scontrol fsdampeningfactor 5",
    "scontrol power down c1",
    "scontrol token username=root lifespan=99999",
    "scontrol cancel_reboot c1",
    "scontrol upd PartitionName=debug State=DOWN",
    "scontrol shutd",
    "scontrol",
    "sacctmgr -i delete user zhanyl",
    "sacctmgr -i modify account root set GrpJobs=0",
    "sacctmgr shutdown",
    "sacctmgr reconfigure",
    "sacctmgr clear stats",
    "sacctmgr",
    "sdiag -r",
    "/tmp/evil/sinfo",
    "./sinfo",
]


class TestAuditedBypasses:
    """Every command the audit probed as ALLOWED by the first guard."""

    @pytest.mark.parametrize("command", AUDITED_BYPASSES)
    def test_denied(self, command: str) -> None:
        with pytest.raises(ToolDenied):
            guard(command.split())

    def test_the_cli_smoke_list_covers_every_audited_bypass(self) -> None:
        # scripts/smoke-guard.sh says it includes every spelling the audit
        # found; this holds it to that.
        denied = {
            " ".join(shlex.split(line)[1:])
            for line in SMOKE_SCRIPT.read_text().splitlines()
            if line.startswith("denied ")
        }
        assert set(AUDITED_BYPASSES) <= denied, set(AUDITED_BYPASSES) - denied

    @pytest.mark.parametrize("name", sorted(t for t, spec in ALLOWED.items() if spec.verbs))
    def test_the_quoted_abbreviation_is_real_and_refused(self, name: str) -> None:
        # The denial message once said scontrol would run "shutd" as
        # shutdown; scontrol requires that word in full. Each tool now quotes
        # an abbreviation its own matcher expands to a write.
        example = ALLOWED[name].prefix_example
        match = re.fullmatch(r"'(\w+)' runs (\w+)", example)
        assert match, example
        abbreviation, write = match.groups()
        assert write.startswith(abbreviation) and abbreviation != write
        assert write in {"scontrol": SCONTROL_WRITES, "sacctmgr": SACCTMGR_WRITES}[name]
        with pytest.raises(ToolDenied, match=re.escape(example)):
            guard([name, abbreviation])


# ── every documented write ──────────────────────────────────────────────────


class TestScontrolWrites:
    @pytest.mark.parametrize("verb", sorted(SCONTROL_COMMANDS - SCONTROL_READS))
    def test_every_non_read_command_is_denied(self, verb: str) -> None:
        with pytest.raises(ToolDenied, match="not a read-only subcommand"):
            guard(["scontrol", verb, "c1"])

    @pytest.mark.parametrize("leading", LEADING_OPTIONS, ids=" ".join)
    @pytest.mark.parametrize("verb", sorted(SCONTROL_WRITES))
    def test_options_before_the_verb_do_not_hide_it(self, verb: str, leading: list[str]) -> None:
        with pytest.raises(ToolDenied):
            guard(["scontrol", *leading, verb, "c1"])

    @pytest.mark.parametrize("verb", sorted(SCONTROL_WRITES))
    def test_options_after_the_verb_do_not_hide_it(self, verb: str) -> None:
        # getopt permutes argv, so options can come after the command too.
        with pytest.raises(ToolDenied):
            guard(["scontrol", verb, "-o", "c1"])

    @pytest.mark.parametrize(
        "abbrev", sorted({p for w in SCONTROL_WRITES for p in _prefixes(w)} - SCONTROL_READS)
    )
    def test_no_abbreviation_of_a_write_passes(self, abbrev: str) -> None:
        with pytest.raises(ToolDenied):
            guard(["scontrol", abbrev, "NodeName=c1", "State=DRAIN"])

    @pytest.mark.parametrize("verb", sorted(SCONTROL_WRITES))
    def test_case_does_not_hide_a_write(self, verb: str) -> None:
        # scontrol commands are case-insensitive.
        with pytest.raises(ToolDenied):
            guard(["scontrol", verb.upper()])

    @pytest.mark.parametrize(
        "command",
        [
            ["scontrol", "update", "NodeName=c1", "State=DRAIN"],
            ["scontrol", "update", "NodeName=ALL", "State=RESUME"],
            ["scontrol", "update", "NodeName=c1", "State=DOWN", "Reason=agent"],
            ["scontrol", "NodeName=c1", "State=DRAIN"],
            ["scontrol", "reboot", "ASAP", "nextstate=DOWN", "ALL"],
            ["scontrol", "requeue", "1234"],
            ["scontrol", "write", "config", "/tmp/x"],
            ["scontrol", "setdebugflags", "-Backfill"],
        ],
    )
    def test_realistic_writes_are_denied(self, command: list[str]) -> None:
        with pytest.raises(ToolDenied):
            guard(command)


class TestScontrolShow:
    @pytest.mark.parametrize(
        "command",
        [
            # bbstat/dwstat hand their arguments to an external status tool.
            ["scontrol", "show", "bbstat"],
            ["scontrol", "show", "dwstat", "-a"],
            # hostlist can read a file or stdin; operands must start alphanumeric.
            ["scontrol", "show", "hostlist", "/etc/passwd"],
            ["scontrol", "show", "hostlist", "-"],
            # abbreviations are not accepted even for show: "bb" could be bbstat.
            ["scontrol", "show", "part"],
            ["scontrol", "show", "bb"],
            # show needs an entity, takes one ID, and accepts only known filters.
            ["scontrol", "show"],
            ["scontrol", "show", "job", "1", "2"],
            ["scontrol", "show", "job", "1", "update"],
            ["scontrol", "show", "node", "State=DRAIN"],
            ["scontrol", "show", "node="],
            ["scontrol", "ping", "extra"],
            ["scontrol", "errnumstr", "abc"],
        ],
    )
    def test_denied(self, command: list[str]) -> None:
        with pytest.raises(ToolDenied):
            guard(command)


class TestSacctmgrWrites:
    @pytest.mark.parametrize("verb", sorted(SACCTMGR_COMMANDS - SACCTMGR_READS))
    def test_every_non_read_command_is_denied(self, verb: str) -> None:
        with pytest.raises(ToolDenied):
            guard(["sacctmgr", verb, "user", "x"])

    @pytest.mark.parametrize("immediate", [[], ["-i"], ["--immediate"]], ids=str)
    @pytest.mark.parametrize("verb", sorted(SACCTMGR_WRITES))
    def test_writes_are_denied_with_or_without_immediate(
        self, verb: str, immediate: list[str]
    ) -> None:
        with pytest.raises(ToolDenied):
            guard(["sacctmgr", *immediate, verb, "user", "x"])

    @pytest.mark.parametrize(
        "abbrev", sorted({p for w in SACCTMGR_WRITES for p in _prefixes(w)} - SACCTMGR_READS)
    )
    def test_no_abbreviation_of_a_write_passes(self, abbrev: str) -> None:
        with pytest.raises(ToolDenied):
            guard(["sacctmgr", abbrev, "user", "x"])

    @pytest.mark.parametrize(
        "command",
        [
            # -i exists only to skip confirmation of writes; refused even on a read.
            ["sacctmgr", "-i", "show", "assoc"],
            ["sacctmgr", "--imm", "show", "assoc"],
            # runawayjobs offers to fix what it lists; so do its source aliases.
            ["sacctmgr", "show", "runawayjobs"],
            ["sacctmgr", "list", "runaway"],
            ["sacctmgr", "show", "ru"],
            ["sacctmgr", "show", "orphanjobs"],
            ["sacctmgr", "show", "lostjobs"],
            ["sacctmgr", "show", "runawayjobs", "set", "EndState=Completed"],
            # entities that exist to be modified
            ["sacctmgr", "show", "job"],
            ["sacctmgr", "show", "coordinator"],
            # set is how sacctmgr writes
            ["sacctmgr", "show", "qos", "set", "GrpJobs=0"],
            ["sacctmgr", "show"],
        ],
    )
    def test_denied(self, command: list[str]) -> None:
        with pytest.raises(ToolDenied):
            guard(command)


class TestSdiag:
    @pytest.mark.parametrize(
        "command",
        [
            ["sdiag", "-r"],
            ["sdiag", "--reset"],
            # getopt_long accepts any unique prefix of a long option...
            ["sdiag", "--r"],
            ["sdiag", "--re"],
            ["sdiag", "--res"],
            ["sdiag", "--rese"],
            # ...and combines short flags.
            ["sdiag", "-ar"],
            ["sdiag", "-ra"],
            ["sdiag", "-tr"],
            ["sdiag", "-a", "-r"],
            ["sdiag", "--all", "--reset"],
        ],
    )
    def test_every_reset_spelling_is_denied(self, command: list[str]) -> None:
        with pytest.raises(ToolDenied):
            guard(command)

    @pytest.mark.parametrize("option", sorted(ALLOWED["sdiag"].options))
    def test_every_other_sdiag_option_is_a_read(self, option: str) -> None:
        if option in {"-M", "--cluster", "--clusters"}:
            guard(["sdiag", option, "c1"])
        else:
            guard(["sdiag", option])


# ── option shapes ──────────────────────────────────────────────────────────


class TestOptionShapes:
    @pytest.mark.parametrize(
        "command",
        [
            ["sinfo", "-Nl"],  # combined short flags
            ["sinfo", "--summ"],  # abbreviated long option
            ["sinfo", "--"],
            ["sinfo", "--bogus"],
            ["sinfo", "-i", "5"],  # --iterate never returns
            ["sinfo", "--iterate=5"],
            ["sinfo", "-o"],  # missing value
            ["sinfo", "-S", "-t"],  # separate value that looks like an option
            ["sinfo", "--long=yes"],  # value on a flag
            ["sinfo", "-o=%N"],  # short options do not take =value
            ["squeue", "-j", "1008"],  # optional value must be attached
            ["squeue", "-j1008"],
            ["sprio", "-j", "5"],  # optional in the source, despite the man page
            ["sacct", "-B", "-j", "1"],  # job scripts can carry credentials
            ["sacct", "--env-vars", "-j", "1"],
            ["sacct", "-f", "/etc/passwd"],
            ["scontrol", "-M", "c1", "show", "config"],  # verb tools: values attached
            ["scontrol", "-u", "0", "show", "config"],
            ["scontrol", "--autocomplete=x", "show", "config"],
            ["sinfo", "extra"],  # no positionals on verb-less tools
        ],
    )
    def test_denied(self, command: list[str]) -> None:
        with pytest.raises(ToolDenied):
            guard(command)

    def test_a_value_is_never_read_as_the_subcommand(self) -> None:
        # With -M taking the next token, "-M update" would hide the verb from a
        # parser whose arity table was wrong. Attached values remove the case.
        with pytest.raises(ToolDenied, match="attach the value"):
            guard(["scontrol", "--clusters", "update", "show", "config"])
        with pytest.raises(ToolDenied):
            guard(["scontrol", "-M", "update", "show", "config"])
        guard(["scontrol", "--clusters=update", "show", "config"])


class TestInjection:
    """Shell syntax cannot reach a Slurm command, and argv never meets a shell."""

    @pytest.mark.parametrize(
        "command",
        [
            ["sinfo", ";", "rm", "-rf", "/"],
            ["sinfo", "; scancel -u root"],
            ["squeue", "-o", "%T && scontrol update NodeName=ALL State=DOWN"],
            ["sacct", "$(scancel 1)"],
            ["sinfo", "`scancel 1`"],
            ["sinfo", "|", "sh"],
            ["scontrol", "show", "node", "c1;reboot"],
            ["sacct", "-o", "$(id)"],
        ],
    )
    def test_shell_syntax_is_refused(self, command: list[str]) -> None:
        with pytest.raises(ToolDenied):
            guard(command)

    @pytest.mark.parametrize("char", ["\n", "\r", "\x00", "\x1b", "\x7f"])
    def test_control_characters_are_refused(self, char: str) -> None:
        with pytest.raises(ToolDenied, match="control character"):
            guard(["sinfo", "-o", f"%N{char}scancel 1"])


class TestNames:
    @pytest.mark.parametrize(
        "name", ["/usr/bin/sinfo", "./sinfo", "/tmp/evil/sinfo", "bin/sinfo", "/usr/bin/scancel"]
    )
    def test_paths_are_refused_even_for_allowed_tools(self, name: str) -> None:
        # The first guard stripped the directory, checked the basename, then
        # executed the full path it had been given.
        with pytest.raises(ToolDenied, match="is a path"):
            guard([name])

    @pytest.mark.parametrize(
        "binary",
        ["scancel", "sbatch", "srun", "salloc", "scrontab", "rm", "systemctl", "kubectl", "sh"],
    )
    def test_unlisted_binaries_are_denied(self, binary: str) -> None:
        # scancel is the interesting one: it is a Slurm command, it is what an
        # agent reaches for, and it destroys a user's running work.
        with pytest.raises(ToolDenied, match="not on the read-only tool surface"):
            guard([binary, "1234"])

    def test_empty_command_is_denied(self) -> None:
        with pytest.raises(ToolDenied):
            guard([])


# ── resolution and execution ───────────────────────────────────────────────


def _fake(directory: Path, name: str, body: str) -> Path:
    """A shell script standing in for a Slurm client."""
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def bindir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty trusted directory, configured through the environment."""
    directory = tmp_path / "bin"
    directory.mkdir()
    directory.chmod(0o755)
    monkeypatch.setenv(TRUSTED_DIRS_ENV, str(directory))
    return directory


class TestResolution:
    def test_default_trusted_dirs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TRUSTED_DIRS_ENV, raising=False)
        assert trusted_dirs() == tuple(Path(d) for d in DEFAULT_TRUSTED_DIRS)

    def test_resolves_to_an_absolute_path(self, bindir: Path) -> None:
        _fake(bindir, "sinfo", "exit 0")
        resolved = resolve_executable("sinfo")
        assert resolved is not None
        assert resolved.is_absolute()
        assert resolved == (bindir / "sinfo").resolve()

    def test_path_is_ignored(
        self, bindir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = tmp_path / "on-path"
        elsewhere.mkdir()
        marker = tmp_path / "ran"
        _fake(elsewhere, "sinfo", f"touch {marker}")
        monkeypatch.setenv("PATH", f"{elsewhere}{os.pathsep}{os.environ.get('PATH', '')}")
        result = run(["sinfo"])
        assert result.exit_code == 127
        assert TRUSTED_DIRS_ENV in result.stderr
        assert not marker.exists()

    @pytest.mark.parametrize("value", ["bin", "./bin", ""])
    def test_relative_or_empty_trusted_dirs_are_refused(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TRUSTED_DIRS_ENV, value)
        with pytest.raises(UntrustedExecutable):
            trusted_dirs()

    def test_a_group_writable_binary_is_refused(self, bindir: Path) -> None:
        _fake(bindir, "sinfo", "exit 0").chmod(0o775)
        with pytest.raises(UntrustedExecutable, match="writable by group or others"):
            run(["sinfo"])

    def test_a_world_writable_directory_is_refused(self, bindir: Path) -> None:
        _fake(bindir, "sinfo", "exit 0")
        bindir.chmod(0o777)
        try:
            with pytest.raises(UntrustedExecutable):
                resolve_executable("sinfo")
        finally:
            bindir.chmod(0o755)

    def test_a_symlink_into_an_untrusted_directory_is_refused(
        self, bindir: Path, tmp_path: Path
    ) -> None:
        evil = tmp_path / "evil"
        evil.mkdir()
        evil.chmod(0o777)
        try:
            target = _fake(evil, "sinfo", "exit 0")
            (bindir / "sinfo").symlink_to(target)
            with pytest.raises(UntrustedExecutable):
                resolve_executable("sinfo")
        finally:
            evil.chmod(0o755)

    def test_a_symlink_in_a_writable_trusted_directory_is_refused(
        self, bindir: Path, tmp_path: Path
    ) -> None:
        # The link's target is clean (0755 file in a 0755 directory), but
        # whoever can write the trusted directory chooses what the link points
        # at. Only the target used to be checked, so this ran.
        clean = tmp_path / "clean"
        clean.mkdir()
        clean.chmod(0o755)
        (bindir / "sinfo").symlink_to(_fake(clean, "sinfo", "echo attacker-controlled"))
        bindir.chmod(0o775)
        try:
            with pytest.raises(UntrustedExecutable, match="writable by group or others"):
                run(["sinfo"])
        finally:
            bindir.chmod(0o755)

    def test_a_symlink_hop_in_a_writable_directory_is_refused(
        self, bindir: Path, tmp_path: Path
    ) -> None:
        # trusted/sinfo -> hop/sinfo -> clean/sinfo: the middle link can be
        # re-pointed by whoever can write hop/.
        clean, hop = tmp_path / "clean", tmp_path / "hop"
        for d in (clean, hop):
            d.mkdir()
            d.chmod(0o755)
        (hop / "sinfo").symlink_to(_fake(clean, "sinfo", "exit 0"))
        (bindir / "sinfo").symlink_to(hop / "sinfo")
        assert resolve_executable("sinfo") == (clean / "sinfo").resolve()
        hop.chmod(0o777)
        try:
            with pytest.raises(UntrustedExecutable):
                resolve_executable("sinfo")
        finally:
            hop.chmod(0o755)

    def test_a_relative_symlink_into_a_clean_directory_resolves(
        self, bindir: Path, tmp_path: Path
    ) -> None:
        # The common install (/usr/bin/sinfo -> ../../opt/slurm/bin/sinfo)
        # must still work.
        clean = tmp_path / "clean"
        clean.mkdir()
        clean.chmod(0o755)
        target = _fake(clean, "sinfo", "exit 0")
        (bindir / "sinfo").symlink_to(Path("..") / "clean" / "sinfo")
        assert resolve_executable("sinfo") == target.resolve()

    def test_a_symlink_loop_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "a").symlink_to(tmp_path / "b")
        (tmp_path / "b").symlink_to(tmp_path / "a")
        with pytest.raises(UntrustedExecutable, match="symlinks"):
            _follow_links(tmp_path / "a")

    def test_only_allowed_tools_resolve(self, bindir: Path) -> None:
        _fake(bindir, "scancel", "exit 0")
        with pytest.raises(ToolDenied):
            resolve_executable("scancel")


class TestExecution:
    def test_runs_the_resolved_path_with_argv_verbatim(self, bindir: Path) -> None:
        _fake(bindir, "sacct", 'printf "%s\\n" "$@"')
        result = run(["sacct", "--delimiter=|", "-p", "-S", "now-1hour"])
        assert result.ok
        assert result.stdout.splitlines() == ["--delimiter=|", "-p", "-S", "now-1hour"]
        assert result.executable == str((bindir / "sacct").resolve())
        assert not result.truncated

    def test_a_denied_command_never_runs(self, bindir: Path, tmp_path: Path) -> None:
        marker = tmp_path / "ran"
        _fake(bindir, "scancel", f"touch {marker}")
        _fake(bindir, "scontrol", f"touch {marker}")
        with pytest.raises(ToolDenied):
            run(["scancel", "1"])
        with pytest.raises(ToolDenied):
            run(["scontrol", "update", "NodeName=c1", "State=DRAIN"])
        with pytest.raises(ToolDenied):
            run(["scontrol"])
        assert not marker.exists()

    def test_a_missing_binary_is_reported_not_raised(self, bindir: Path) -> None:
        # sinfo is permitted but absent. A clean 127 keeps the agent's error
        # handling identical whether or not Slurm is installed, and pointing
        # the trusted directory at an empty one makes this deterministic.
        result = run(["sinfo"])
        assert result.exit_code == 127
        assert not result.ok
        assert result.executable is None

    def test_a_non_zero_exit_is_reported(self, bindir: Path) -> None:
        _fake(bindir, "squeue", "echo 'slurm_load_jobs error' >&2; exit 1")
        result = run(["squeue"])
        assert result.exit_code == 1
        assert not result.ok
        assert "slurm_load_jobs error" in result.stderr

    def test_a_hang_is_reported_as_a_timeout(self, bindir: Path) -> None:
        # The storage-stall family is identified by commands that block, so a
        # hang is evidence and must come back as data, not an exception. Only
        # the timeout is asserted here: whether the child has written anything
        # within 0.5 s depends on load (under 12 parallel runs it had not).
        _fake(bindir, "sacct", "exec sleep 30")
        result = run(["sacct"], timeout=0.5)
        assert result.timed_out
        assert result.exit_code == -1
        assert not result.ok
        assert "timed out after 0.5s" in result.stderr

    def test_output_before_a_hang_is_kept(
        self, bindir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # What a child wrote before the deadline arrives on the exception
        # (TimeoutExpired.stdout/.stderr, filled in by subprocess.run). Whether
        # a real child has written by a given deadline depends on load: a
        # stand-in that echoed first and then slept still had written nothing
        # after 3 s in 21 of 24 parallel runs. So the exception is raised
        # directly, and only run()'s handling of it is under test.
        _fake(bindir, "sacct", "exit 0")

        def hang(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired(
                argv, 7.0, output=b"partial\n", stderr=b"sacct: waiting\n"
            )

        monkeypatch.setattr(subprocess, "run", hang)
        result = run(["sacct"], timeout=7.0)
        assert result.timed_out
        assert result.exit_code == -1
        assert result.stdout == "partial\n"
        assert result.stdout_bytes == len(b"partial\n")
        assert result.stderr.startswith("sacct: waiting\n")
        assert result.stderr.endswith("timed out after 7.0s")
        assert result.executable == str((bindir / "sacct").resolve())

    def test_output_is_capped_and_says_so(self, bindir: Path) -> None:
        _fake(
            bindir, "sacct", "i=0; while [ $i -lt 200 ]; do echo 0123456789abcdef; i=$((i+1)); done"
        )
        result = run(["sacct"], max_output_bytes=100)
        assert result.truncated
        assert len(result.stdout.encode()) == 100
        assert result.stdout_bytes == 200 * 17

    def test_stdin_is_closed_to_the_child(self, bindir: Path) -> None:
        # The audit piped "update ... / shutdown" into a process that called
        # run(["scontrol"]) and a fake interactive scontrol "executed" both.
        # Bare scontrol is now refused by the guard; this checks the second
        # barrier: even an allowed command cannot read the parent's stdin.
        _fake(
            bindir,
            "scontrol",
            'while IFS= read -r line; do echo "would execute: $line"; done; echo "stdin: EOF"',
        )
        code = (
            "from csa.mcp.readonly import run; "
            "r = run(['scontrol', 'show', 'config']); print(r.stdout, end='')"
        )
        child = subprocess.run(  # noqa: S603 - fixed argv, test interpreter
            [sys.executable, "-c", code],
            input=b"update NodeName=ALL State=DRAIN\nshutdown\n",
            capture_output=True,
            env={**os.environ, TRUSTED_DIRS_ENV: str(bindir)},
            timeout=60,
            check=True,
        )
        assert child.stdout.decode() == "stdin: EOF\n"


class TestManifest:
    def test_every_tool_is_advertised_read_only(self) -> None:
        manifest = tool_manifest()
        assert {entry["name"] for entry in manifest} == set(ALLOWED)
        for entry in manifest:
            annotations = entry["annotations"]
            assert isinstance(annotations, dict)
            assert annotations["readOnlyHint"] is True

    def test_entries_have_the_mcp_tool_shape(self) -> None:
        # MCP schema 2026-07-28: inputSchema is required and its root is an object.
        for entry in tool_manifest():
            assert set(entry) == {"name", "description", "inputSchema", "annotations", "_meta"}
            schema = entry["inputSchema"]
            assert isinstance(schema, dict)
            assert schema["type"] == "object"
            meta = entry["_meta"]
            assert isinstance(meta, dict)
            assert all(key.startswith("io.github.zhanyl-tech/") for key in meta)

    def test_the_exclusions_are_published(self) -> None:
        sdiag = next(e for e in tool_manifest() if e["name"] == "sdiag")
        meta = sdiag["_meta"]
        assert isinstance(meta, dict)
        assert "-r/--reset" in meta["io.github.zhanyl-tech/excluded"]

    def test_no_write_capable_binary_is_on_the_surface(self) -> None:
        # A regression guard: if someone adds sbatch "just for testing", this
        # fails before it reaches a cluster.
        dangerous = {"scancel", "sbatch", "srun", "salloc", "scrontab", "strigger"}
        assert not (set(ALLOWED) & dangerous)
