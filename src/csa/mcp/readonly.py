"""Read-only tool surface, enforced in code rather than in the prompt.

A prompt that says "only use read-only commands" is a request, not a control.
It fails open: the model is free to ignore it, and a jailbreak, a confused
tool call, or an ordinary hallucination is enough to run ``scontrol update``
on a production controller. Anything that could drain a node must be
impossible to express, not merely discouraged.

**The control is a positive grammar, not a list of bad words.** The first
version of this module refused a denylist of mutating subcommands and let
everything else through. An audit then found it allowing ``scontrol -o
shutdown`` (an option before the verb), ``scontrol upd ...`` (an abbreviation
scontrol expands to ``update``), ``sacctmgr -i delete user x``, ``sdiag -r``
(which wipes the counters a diagnosis needs), ``/tmp/evil/sinfo``, and a bare
``scontrol`` (interactive mode, which reads further commands from stdin). A
denylist has to anticipate every spelling of every write; an allowlist only
has to describe the reads. So :func:`guard` accepts a command only if every
token fits a shape written down here:

* ``argv[0]`` is the bare name of a tool in :data:`ALLOWED`, never a path;
* every option is an exact token from that tool's option table, with the arity
  its option parser gives it. Exact matters: ``getopt_long`` would expand
  ``--res`` to ``--reset`` and split ``-ar`` into ``-a -r``;
* scontrol and sacctmgr must name a subcommand, and it must be a *full word*
  from a short read-only list (``show``, ``ping``, ...). Both tools match a
  command by prefix, down to a minimum length set per command in the source
  (``xstrncasecmp(tag, "update", MAX(tag_len, 1))`` in scontrol.c, so ``u``
  runs update; ``MAX(command_len, 4)`` for sacctmgr's shutdown, so ``shutd``
  runs it). A few commands demand the full word (scontrol's ``shutdown`` and
  ``takeover``), but most do not, so only full words are safe;
* a value for a value-taking option of scontrol or sacctmgr must be attached
  (``--clusters=c1``), so no token can be read as a value by this parser and as
  the subcommand by the tool, or the other way round;
* operands and option values must match conservative character shapes, and no
  argument may contain a control character.

:func:`run` then executes the tool by an **absolute path** resolved only from
trusted directories (:func:`trusted_dirs`), never from ``$PATH`` and never from
a path supplied with the command; with stdin closed, so nothing can reach an
interactive prompt; with a timeout that is reported rather than raised; and
with a cap on how much output is returned.

Sources for every table below, read on 2026-09-26:

* man pages (Slurm 26.05): https://slurm.schedmd.com/scontrol.html and the
  sibling pages sacctmgr, sdiag, sinfo, squeue, sacct, sprio, sshare;
* the option parsers at tag ``slurm-25-11-4-1``, the version the benchmark
  cluster runs: https://github.com/SchedMD/slurm/tree/slurm-25-11-4-1/src
  (``scontrol/scontrol.c``, ``sacctmgr/sacctmgr.c``, ``sdiag/opts.c``,
  ``sinfo/opts.c``, ``squeue/opts.c``, ``sacct/options.c``,
  ``sprio/opts.c``, ``sshare/sshare.c``).

Where the two disagree the source wins: ``sprio -j`` takes an *optional* value
in the source although the man page shows it as required, and squeue's
``--account``/``--partition`` (man page) are refused because the parser
declares only ``--accounts``/``--partitions`` and reaches the shorter
spellings by the prefix expansion this grammar exists to avoid.

The threat model is deliberately not "malicious user". It is an agent that has
read a confusing log line at 3am and is about to do something decisive. Log
lines are text the agent does not control, which is why the grammar has to hold
even when the command was shaped by someone else.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType


class ToolDenied(PermissionError):
    """The requested command is not on the read-only surface."""


class UntrustedExecutable(ToolDenied):
    """The tool is permitted, but the binary that would run cannot be trusted.

    A subclass of :class:`ToolDenied` so that every caller that already fails
    safe on a denial also fails safe on this.
    """


class Arity(Enum):
    """How an option takes its value, as the tool's getopt table declares it."""

    #: No value (``no_argument``).
    FLAG = "flag"
    #: A required value: the next token, or attached as ``--long=value``.
    VALUE = "value"
    #: A value only when attached as ``--long=value`` (``optional_argument``).
    #: A following token is never consumed, which is also what getopt does.
    OPTIONAL = "optional"


# ── shapes ────────────────────────────────────────────────────────────────
# ASCII only, and narrow on purpose. Nothing here is passed to a shell
# (execution uses an argv list), so these are not an injection filter; they
# keep operands from being read as options ("-x") or files ("/etc/...") and
# keep option values to what Slurm format strings, lists and times need.

#: Control characters. argv goes straight to execve, so no shell would read a
#: newline, but no legitimate read needs one and it can forge log lines.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: Option values: format strings ("%N %t"), lists ("a,b"), hostlists
#: ("c[1-4]"), times ("now-1hour", "2026-09-26T10:00"), sort keys ("+P,-m"),
#: state filters ("~idle") and the sacct delimiter ("|").
_VALUE = re.compile(r"[A-Za-z0-9 %.,:_|#+~/\[\]-]+", re.ASCII)
#: Operands after a subcommand: names, IDs, hostlists. Must start with a letter
#: or digit, so an operand can never be an option or an absolute path.
_OPERAND = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.,:+%\[\]-]*", re.ASCII)
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]*", re.ASCII)
_NUMBER = re.compile(r"[0-9]+", re.ASCII)

#: Checks a subcommand's operands; raises :class:`ToolDenied`.
OperandCheck = Callable[[str, str, Sequence[str]], None]


def _table(flags: str, values: str = "", optional: str = "") -> Mapping[str, Arity]:
    """Build an option table from whitespace-separated option spellings."""
    table: dict[str, Arity] = {}
    for spellings, arity in (
        (flags, Arity.FLAG),
        (values, Arity.VALUE),
        (optional, Arity.OPTIONAL),
    ):
        for name in spellings.split():
            if name in table:
                raise ValueError(f"option {name} listed twice")
            table[name] = arity
    return MappingProxyType(table)


def _operands(minimum: int, maximum: int, shape: re.Pattern[str] = _OPERAND) -> OperandCheck:
    """A subcommand taking between ``minimum`` and ``maximum`` plain operands."""

    def check(tool: str, verb: str, operands: Sequence[str]) -> None:
        if not minimum <= len(operands) <= maximum:
            want = str(minimum) if minimum == maximum else f"{minimum} to {maximum}"
            raise ToolDenied(f"{tool} {verb} takes {want} operand(s), got {len(operands)}")
        for operand in operands:
            if not shape.fullmatch(operand):
                raise ToolDenied(f"{tool} {verb}: operand {operand!r} is not a plain name or ID")

    return check


#: ``scontrol show`` entities, as full words, with the ``key=value`` filters
#: each accepts (lower case). Left out on purpose: ``bbstat`` and ``dwstat``
#: pass their remaining arguments to an external status tool run by
#: slurmctld; ``resources`` is in the source but undocumented.
_SCONTROL_SHOW: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "aliases": frozenset(),
        "assoc_mgr": frozenset({"users", "accounts", "qos", "flags"}),
        "burstbuffer": frozenset(),
        "config": frozenset(),
        "daemons": frozenset(),
        "federation": frozenset(),
        "hostlist": frozenset(),
        "hostlistsorted": frozenset(),
        "hostnames": frozenset(),
        "job": frozenset(),
        "licenses": frozenset(),
        "node": frozenset(),
        "partition": frozenset(),
        "reservation": frozenset(),
        "slurmd": frozenset(),
        "step": frozenset({"container-id"}),
        "topoconf": frozenset(),
        "topology": frozenset({"unit", "switch", "block", "ring", "node"}),
    }
)


def _scontrol_show(tool: str, verb: str, operands: Sequence[str]) -> None:
    """``show <entity>[=<id>] [<id>] [key=value ...]``, at most one plain ID.

    The operand shape (letter or digit first) is also what keeps ``show
    hostlist`` from reading a file (``/path``) or stdin (``-``).
    """
    if not operands:
        raise ToolDenied(f"{tool} show needs an entity: {', '.join(_SCONTROL_SHOW)}")
    head, *rest = operands
    entity, has_id, ident = head.partition("=")
    keys = _SCONTROL_SHOW.get(entity.lower())
    if keys is None:
        raise ToolDenied(
            f"{tool} show {entity!r} is not a permitted entity (full names only): "
            f"{', '.join(_SCONTROL_SHOW)}"
        )
    plain = [ident] if has_id else []
    for operand in rest:
        key, has_value, value = operand.partition("=")
        if not has_value:
            plain.append(operand)
        elif key.lower() not in keys:
            raise ToolDenied(f"{tool} show {entity}: filter {key!r} is not accepted here")
        elif not _OPERAND.fullmatch(value):
            raise ToolDenied(f"{tool} show {entity}: {operand!r} is not a plain filter")
    if len(plain) > 1:
        raise ToolDenied(f"{tool} show {entity} takes at most one name or ID, got {plain}")
    for operand in plain:
        if not _OPERAND.fullmatch(operand):
            raise ToolDenied(f"{tool} show {entity}: {operand!r} is not a plain name or ID")


#: ``sacctmgr show|list`` entities, full words plus the spellings the man page
#: itself uses (``assoc``). Left out on purpose: ``runawayjobs`` (and its
#: source aliases ``orphanjobs``/``lostjobs``), whose list form offers to fix
#: the records it finds; ``job`` and ``coordinator``, which exist to be
#: modified.
_SACCTMGR_ENTITIES = frozenset(
    {
        "account",
        "accounts",
        "assoc",
        "association",
        "associations",
        "cluster",
        "clusters",
        "configuration",
        "event",
        "events",
        "federation",
        "instance",
        "instances",
        "problem",
        "problems",
        "qos",
        "reservation",
        "reservations",
        "resource",
        "stats",
        "transaction",
        "transactions",
        "tres",
        "user",
        "users",
        "wckey",
        "wckeys",
    }
)

#: Bare keywords a ``show``/``list`` may carry (lower case). ``set`` is
#: deliberately absent: it is how sacctmgr writes.
_SACCTMGR_WORDS = frozenset(
    {
        "where",
        "withassoc",
        "withcoord",
        "withdeleted",
        "withsubaccounts",
        "withrawqoslevel",
        "withfed",
        "withclusters",
        "onlydefaults",
        "tree",
        "wolimits",
        "wopinfo",
        "woplimits",
        "wopi",
        "wopl",
        "all_clusters",
        "all_time",
        "ave_time",
        "total_time",
    }
)


def _sacctmgr_show(tool: str, verb: str, operands: Sequence[str]) -> None:
    """``show|list <entity> [where] [key=value ...] [with... ...]``.

    Filter keys are checked for shape rather than enumerated: sacctmgr has
    dozens, and once the entity is a read-only one and ``set`` is refused, a
    ``show`` only displays (sacctmgr man page: "Display information about the
    specified entity").
    """
    if not operands:
        raise ToolDenied(f"{tool} {verb} needs an entity: {', '.join(sorted(_SACCTMGR_ENTITIES))}")
    entity, *rest = operands
    if entity.lower() not in _SACCTMGR_ENTITIES:
        raise ToolDenied(
            f"{tool} {verb} {entity!r} is not a permitted entity (runawayjobs is excluded "
            "because listing it offers to fix records)"
        )
    for operand in rest:
        key, has_value, value = operand.partition("=")
        if has_value:
            if not (_KEY.fullmatch(key) and _OPERAND.fullmatch(value)):
                raise ToolDenied(f"{tool} {verb}: {operand!r} is not a plain key=value filter")
        elif operand.lower() not in _SACCTMGR_WORDS:
            raise ToolDenied(
                f"{tool} {verb}: {operand!r} is not a permitted keyword "
                "('set' is refused because it is how sacctmgr writes)"
            )


@dataclass(frozen=True, slots=True)
class Tool:
    """One permitted binary and the shape of its safe usage."""

    binary: str
    description: str
    #: The official page every table entry was checked against.
    doc_url: str
    #: Every option this surface accepts, spelled exactly, with its arity.
    options: Mapping[str, Arity]
    #: Permitted subcommands and the check each applies to its operands.
    #: ``None`` means the tool takes no subcommand and no positional arguments.
    verbs: Mapping[str, OperandCheck] | None = None
    #: Read-shaped or famous things deliberately left off, with the reason.
    #: Documentation only: the allowlist above is the control, and anything
    #: not on it is refused whether or not it is listed here.
    excluded: Mapping[str, str] = field(default_factory=dict)
    #: For a tool with subcommands: an abbreviation the tool itself expands to
    #: a write, quoted when a subcommand is refused. Checked against the
    #: 25.11.4 command matcher, since the minimum prefix differs per command.
    prefix_example: str = ""


#: ``optional_argument`` in every parser that has them, so ``--json`` and
#: ``--json=<data_parser>`` both parse, and a following token is never taken.
_JSON_YAML = "--json --yaml"

#: Every tool the agent may call. Adding to this is a deliberate act; the
#: default answer to "should the agent be able to run this?" is no.
ALLOWED: Mapping[str, Tool] = MappingProxyType(
    {
        "sinfo": Tool(
            "sinfo",
            "Node and partition state",
            "https://slurm.schedmd.com/sinfo.html",
            _table(
                "-a --all -d --dead -e --exact --federation -F --future --help --hide "
                "-R --list-reasons --local -l --long --noconvert -N --Node "
                "-h --noheader -T --reservation -r --responding -s --summarize --usage "
                "-v --verbose -V --version",
                values="-M --cluster --clusters -o --format -O --Format -n --nodes "
                "-p --partition -S --sort -t --states",
                optional=_JSON_YAML,
            ),
            excluded=MappingProxyType(
                {"-i/--iterate": "reports forever; the call could only end in a timeout"}
            ),
        ),
        "squeue": Tool(
            "squeue",
            "Job queue",
            "https://slurm.schedmd.com/squeue.html",
            _table(
                "-a --all -r --array --expand-patterns --federation --help --hide "
                "--local -l --long --me --noconvert -h --noheader --notme "
                "--only-job-state -P --priority --sibling --start --usage -v --verbose "
                "-V --version",
                values="-A --accounts -M --cluster --clusters -o --format "
                "-O --Format -L --licenses -n --name -w --nodelist -p "
                "--partitions -q --qos -R --reservation --running-over --running-under "
                "-S --sort -t --states -u --user --users",
                optional=f"-j --jobs -s --steps {_JSON_YAML}",
            ),
            excluded=MappingProxyType(
                {
                    "-i/--iterate": "reports forever; the call could only end in a timeout",
                    "--account, --partition": (
                        "man-page spellings the 25.11.4 parser does not declare; getopt "
                        "reaches --accounts/--partitions from them only by prefix "
                        "expansion. Use those, or -A/-p"
                    ),
                }
            ),
        ),
        "sacct": Tool(
            "sacct",
            "Accounting history",
            "https://slurm.schedmd.com/sacct.html",
            _table(
                "--array -L --allclusters -X --allocations -a --allusers -b --brief "
                "-c --completion -D --duplicates --expand-patterns --federation -h --help "
                "-e --helpformat --local -l --long --noconvert -n --noheader "
                "-p --parsable -P --parsable2 -T --truncate --usage --use-local-uid "
                "-v --verbose -V --version",
                values="-A --accounts -x --associations -M --cluster --clusters "
                "-C --constraints --delimiter -E --endtime -F --flags -o --format --fields "
                "-g --gid --group -j --jobs --name -i --nnodes -I --ncpus -N --nodelist "
                "-r --partition -q --qos -R --reason -S --starttime -s --state "
                "-K --timelimit-max -k --timelimit-min -u --uid --user --units -W --wckeys",
                optional=f"--whole-hetjob {_JSON_YAML}",
            ),
            excluded=MappingProxyType(
                {
                    "-B/--batch-script, --env-vars": (
                        "print job scripts and environments, which routinely carry "
                        "credentials, into a logged agent transcript"
                    ),
                    "-f/--file": "reads a file named by the caller",
                }
            ),
        ),
        "sdiag": Tool(
            "sdiag",
            "Scheduler diagnostics: cycle counts, RPC and agent queues",
            "https://slurm.schedmd.com/sdiag.html",
            _table(
                "-a --all -h --help --no-trunc -i --sort-by-id -t --sort-by-time "
                "-T --sort-by-time2 --usage -V --version",
                values="-M --cluster --clusters",
                optional=_JSON_YAML,
            ),
            excluded=MappingProxyType(
                {
                    "-r/--reset": (
                        "resets scheduler and RPC counters to 0, destroying the "
                        "evidence a diagnosis reads"
                    )
                }
            ),
        ),
        "sprio": Tool(
            "sprio",
            "Priority breakdown per pending job",
            "https://slurm.schedmd.com/sprio.html",
            _table(
                "--federation --help -l --long --local -h --noheader -n --norm --sibling "
                "--usage -v --verbose -V --version -w --weights",
                values="-M --cluster --clusters -o --format -p --partition -S --sort "
                "-u --user --users",
                optional="-j --jobs",
            ),
        ),
        "sshare": Tool(
            "sshare",
            "Fairshare state",
            "https://slurm.schedmd.com/sshare.html",
            _table(
                "-a --all --help -l --long -n --noheader -p --parsable "
                "-P --parsable2 -m --partition --usage -U --Users -v --verbose -V --version",
                values="-A --accounts -M --cluster --clusters -o --format -u --users",
                optional=_JSON_YAML,
            ),
        ),
        "scontrol": Tool(
            "scontrol",
            "Cluster state, read paths only",
            # scontrol is the sharpest object in the drawer: `show` is
            # harmless and `update` drains nodes. Permitted narrowly rather than
            # excluded, since `scontrol show config` is often the fastest route
            # to a diagnosis.
            "https://slurm.schedmd.com/scontrol.html",
            _table(
                "-a --all -d --details --federation -F --future -h --help --hide "
                "--local -o --oneliner -Q --quiet --sibling -v --verbose "
                "-V --version",
                values="--cluster --clusters",
                optional=_JSON_YAML,
            ),
            verbs=MappingProxyType(
                {
                    "show": _scontrol_show,
                    "ping": _operands(0, 0),
                    "version": _operands(0, 0),
                    "completing": _operands(0, 0),
                    "errnumstr": _operands(1, 1, _NUMBER),
                    "pidinfo": _operands(1, 1, _NUMBER),
                    "getaddrs": _operands(1, 1),
                    "listpids": _operands(0, 2),
                    "listjobs": _operands(0, 1),
                    "liststeps": _operands(0, 1),
                }
            ),
            excluded=MappingProxyType(
                {
                    "-u/--uid": "only meaningful for update, which is not permitted",
                    "every other subcommand": (
                        "update, delete, create, reconfigure, shutdown, takeover, power, "
                        "reboot, top, hold/uhold/release, ... change state; token mints "
                        "a credential"
                    ),
                }
            ),
            # scontrol.c: xstrncasecmp(tag, "update", MAX(tag_len, 1)). Not
            # shutdown, which scontrol accepts only as the full word.
            prefix_example="'upd' runs update",
        ),
        "sacctmgr": Tool(
            "sacctmgr",
            "Accounting associations and limits, read paths only",
            "https://slurm.schedmd.com/sacctmgr.html",
            _table(
                "-s --associations -h --help -n --noheader -p --parsable "
                "-P --parsable2 -Q --quiet -r --readonly -v --verbose -V --version",
                optional=_JSON_YAML,
            ),
            verbs=MappingProxyType(
                {
                    "show": _sacctmgr_show,
                    "list": _sacctmgr_show,
                    "ping": _operands(0, 0),
                    "version": _operands(0, 0),
                }
            ),
            excluded=MappingProxyType(
                {
                    "-i/--immediate": "exists only to commit writes without confirmation",
                    "show runawayjobs": "offers to fix the records it lists",
                }
            ),
            # sacctmgr.c: xstrncasecmp(argv[0], "shutdown", MAX(command_len, 4)).
            prefix_example="'shutd' runs shutdown",
        ),
    }
)


def guard(command: Sequence[str]) -> None:
    """Raise :class:`ToolDenied` unless ``command`` is a permitted read.

    Called by every execution path. Pure: it reads no files and runs nothing,
    so ``csa check`` can answer on a machine without Slurm. Raising rather
    than sanitising is deliberate: silently stripping a forbidden flag would
    let the agent believe it had acted when it had not, which is its own
    failure mode.
    """
    if not command:
        raise ToolDenied("empty command")
    for arg in command:
        if _CONTROL.search(arg):
            raise ToolDenied(f"argument {arg!r} contains a control character")

    name = command[0]
    if "/" in name:
        raise ToolDenied(
            f"{name!r} is a path; give the bare tool name. Tools are resolved to an "
            "absolute path from trusted directories, never from a path in the command"
        )
    tool = ALLOWED.get(name)
    if tool is None:
        permitted = ", ".join(sorted(ALLOWED))
        raise ToolDenied(f"{name!r} is not on the read-only tool surface; permitted: {permitted}")

    positionals = _parse_options(tool, command[1:])
    if tool.verbs is None:
        if positionals:
            raise ToolDenied(
                f"{name} takes no positional arguments, got {positionals[0]!r} (an option "
                "with an optional value takes it attached, e.g. --jobs=1008)"
            )
        return
    if not positionals:
        raise ToolDenied(
            f"bare {name} starts interactive mode and reads further commands from stdin, "
            f"where no guard can see them; name a subcommand: {', '.join(tool.verbs)}"
        )
    verb, *operands = positionals
    check = tool.verbs.get(verb.lower())
    if check is None:
        raise ToolDenied(
            f"{name} {verb!r} is not a read-only subcommand on this surface (full words "
            f"only: {name} matches subcommands by prefix, so {tool.prefix_example}); "
            f"permitted: {', '.join(tool.verbs)}. This agent recommends actions, it does "
            "not take them"
        )
    check(name, verb.lower(), operands)


def _parse_options(tool: Tool, args: Sequence[str]) -> list[str]:
    """Validate every option in ``args`` and return the positionals, in order.

    Options are recognised anywhere, not only before the subcommand, because
    the Slurm tools use GNU ``getopt_long``, which permutes argv.
    """
    positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if not arg.startswith("-") or arg == "-":
            positionals.append(arg)
            continue
        name, attached, value = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        arity = tool.options.get(name)
        if arity is None:
            raise ToolDenied(
                f"{tool.binary} option {arg!r} is not permitted. Options must be spelled "
                "exactly as listed (no abbreviations such as --res, no combined short "
                "flags such as -ar, values attached only as --long=value)"
            )
        if attached:
            if arity is Arity.FLAG:
                raise ToolDenied(f"{tool.binary} {name} takes no value")
            _check_value(tool, name, value)
        elif arity is Arity.VALUE:
            if tool.verbs is not None:
                raise ToolDenied(
                    f"{tool.binary} {name}: attach the value as {name}=<value>, so it "
                    "cannot be mistaken for the subcommand"
                )
            if i >= len(args):
                raise ToolDenied(f"{tool.binary} {name} needs a value")
            value = args[i]
            i += 1
            if value.startswith("-"):
                raise ToolDenied(
                    f"{tool.binary} {name}: value {value!r} looks like an option; "
                    "attach it to the long form if it really is the value"
                )
            _check_value(tool, name, value)
    return positionals


def _check_value(tool: Tool, name: str, value: str) -> None:
    if not _VALUE.fullmatch(value):
        raise ToolDenied(
            f"{tool.binary} {name}: value {value!r} has characters outside those Slurm "
            "names, lists, format strings and times use"
        )


# ── resolution ────────────────────────────────────────────────────────────

#: Environment variable naming the directories Slurm clients may run from,
#: separated by ``os.pathsep``. Deployment configuration, not agent input.
TRUSTED_DIRS_ENV = "CSA_SLURM_BIN"

#: Where distribution packages put the Slurm clients, used when
#: :data:`TRUSTED_DIRS_ENV` is unset. Sites that install elsewhere (for
#: example ``/opt/slurm/bin``) set the variable.
DEFAULT_TRUSTED_DIRS: tuple[str, ...] = ("/usr/bin", "/usr/local/bin")


def trusted_dirs() -> tuple[Path, ...]:
    """The directories an allowed tool may be executed from, all absolute.

    Never ``$PATH``: whoever controls the environment the agent runs in, or
    its working directory, would otherwise decide what "sinfo" means.
    """
    raw = os.environ.get(TRUSTED_DIRS_ENV)
    entries = DEFAULT_TRUSTED_DIRS if raw is None else tuple(e for e in raw.split(os.pathsep) if e)
    if not entries:
        raise UntrustedExecutable(f"{TRUSTED_DIRS_ENV} is set but names no directory")
    dirs: list[Path] = []
    for entry in entries:
        directory = Path(entry)
        if not directory.is_absolute():
            raise UntrustedExecutable(
                f"{TRUSTED_DIRS_ENV} entry {entry!r} is relative; trusted directories must "
                "be absolute so the working directory can never supply a binary"
            )
        dirs.append(directory)
    return tuple(dirs)


#: Symlink hops followed before giving up; the kernel's own limit is similar.
_MAX_SYMLINK_HOPS = 40


def resolve_executable(name: str, dirs: Sequence[Path] | None = None) -> Path | None:
    """Absolute path of an allowed tool, found only in trusted directories.

    Returns ``None`` when the tool is not installed there. Raises
    :class:`UntrustedExecutable` when it is installed but group or others can
    write anything that decides what runs, because then someone other than
    the owner decides what the "read-only" tool does. That is: the trusted
    directory it was found in; the directory holding each symlink on the way
    (whoever can write it can re-point the link); and the final file and its
    directory. Checking only the final file let a symlink planted in a
    group-writable trusted directory point anywhere clean.

    Not checked: who *owns* these (a site may install Slurm as a non-root
    admin user), and ancestors of these directories.
    """
    if name not in ALLOWED:
        raise ToolDenied(f"{name!r} is not on the read-only tool surface")
    for directory in trusted_dirs() if dirs is None else dirs:
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            _refuse_if_writable_by_others(directory)
            real = _follow_links(candidate)
            _refuse_if_writable_by_others(real)
            _refuse_if_writable_by_others(real.parent)
            return real
    return None


def _follow_links(path: Path) -> Path:
    """Resolve ``path``, refusing if any symlink on the way sits in an unsafe directory."""
    current = path
    for _ in range(_MAX_SYMLINK_HOPS):
        if not current.is_symlink():
            return current.resolve()
        _refuse_if_writable_by_others(current.parent)
        current = current.parent / current.readlink()  # an absolute target replaces the base
    raise UntrustedExecutable(f"{path}: more than {_MAX_SYMLINK_HOPS} symlinks")


def _refuse_if_writable_by_others(path: Path) -> None:
    mode = path.stat().st_mode
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UntrustedExecutable(
            f"{path} is writable by group or others ({stat.filemode(mode)}); refusing to "
            f"run a tool whose contents someone else can change. Point {TRUSTED_DIRS_ENV} "
            "at a directory only its owner can write"
        )


# ── execution ─────────────────────────────────────────────────────────────

#: Per-stream cap on returned output. Large enough for a partition's sinfo or a
#: filtered squeue; small enough that one unfiltered sacct cannot fill the
#: agent's context. Deployments can pass their own.
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Outcome of a guarded invocation."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    #: The absolute path that was executed; ``None`` when nothing ran.
    executable: str | None = None
    #: Bytes the command wrote to each stream, before the cap.
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    #: True when either stream was cut to the cap.
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def run(
    command: Sequence[str],
    *,
    timeout: float = 30.0,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> ToolResult:
    """Run a read-only command after guarding it.

    Executed by absolute path, without a shell, with stdin closed: a tool that
    somehow reached an interactive prompt reads end-of-file, not whatever the
    agent's own stdin carries (for a stdio MCP server, that is the JSON-RPC
    stream). A timeout is reported rather than raised, because a command that
    hangs is itself diagnostic information: the storage-stall family is
    identified precisely by commands that block instead of failing. Output is
    capped at ``max_output_bytes`` per stream, with the true sizes and a
    ``truncated`` flag in the result. The cap bounds what reaches the agent's
    context; the child's full output is still read into memory first.
    """
    guard(command)
    argv = tuple(command)
    executable = resolve_executable(argv[0])
    if executable is None:
        where = os.pathsep.join(str(d) for d in trusted_dirs())
        return ToolResult(
            argv,
            127,
            "",
            f"{argv[0]}: not found in trusted directories ({where}); "
            f"set {TRUSTED_DIRS_ENV} to where the Slurm clients are installed",
        )

    try:
        completed = subprocess.run(  # noqa: S603 - argv list from guard(), absolute path
            [str(executable), *argv[1:]],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        out, err = _as_bytes(exc.stdout), _as_bytes(exc.stderr)
        stderr = _decode(err, max_output_bytes)
        note = f"timed out after {timeout}s"
        return ToolResult(
            argv,
            -1,
            _decode(out, max_output_bytes),
            f"{stderr}\n{note}" if stderr else note,
            timed_out=True,
            executable=str(executable),
            stdout_bytes=len(out),
            stderr_bytes=len(err),
            truncated=max(len(out), len(err)) > max_output_bytes,
        )

    out, err = completed.stdout, completed.stderr
    return ToolResult(
        argv,
        completed.returncode,
        _decode(out, max_output_bytes),
        _decode(err, max_output_bytes),
        executable=str(executable),
        stdout_bytes=len(out),
        stderr_bytes=len(err),
        truncated=max(len(out), len(err)) > max_output_bytes,
    )


def _as_bytes(data: object) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode()
    return b""


def _decode(data: bytes, limit: int) -> str:
    return data[:limit].decode("utf-8", errors="replace")


# ── manifest ──────────────────────────────────────────────────────────────

#: Namespace for this project's ``_meta`` keys. The MCP spec reserves
#: prefixes whose second label is ``modelcontextprotocol`` or ``mcp`` and asks
#: for reverse-DNS prefixes otherwise (basic/index, "General fields: _meta").
_META = "io.github.zhanyl-tech/"


def tool_manifest() -> list[dict[str, object]]:
    """The tool surface, shaped like MCP ``Tool`` entries.

    Shape checked against the MCP schema, protocol revision 2026-07-28
    (https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2026-07-28/schema.ts):
    ``name``, ``description``, a required ``inputSchema`` whose root is
    ``type: "object"``, ``annotations.readOnlyHint``, and project data under
    ``_meta``. No MCP server exists in this repository yet; this is the data a
    server would advertise. The schema itself says clients should never make
    tool-use decisions from annotations of untrusted servers, which is why the
    control is :func:`guard`, not the hint.
    """
    manifest: list[dict[str, object]] = []
    for tool in sorted(ALLOWED.values(), key=lambda t: t.binary):
        args_schema: dict[str, object] = {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                f"argv after '{tool.binary}'. Must satisfy the read-only grammar; "
                "anything else is refused, not rewritten."
            ),
        }
        if tool.verbs is not None:
            args_schema["minItems"] = 1
        manifest.append(
            {
                "name": tool.binary,
                "description": tool.description,
                "inputSchema": {
                    "type": "object",
                    "properties": {"args": args_schema},
                    "required": ["args"],
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": True},
                "_meta": {
                    f"{_META}docs": tool.doc_url,
                    f"{_META}subcommands": sorted(tool.verbs) if tool.verbs else [],
                    f"{_META}options": sorted(tool.options),
                    f"{_META}excluded": dict(tool.excluded),
                },
            }
        )
    return manifest
