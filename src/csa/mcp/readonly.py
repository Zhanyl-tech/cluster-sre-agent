"""Read-only tool surface, enforced in the server rather than the prompt.

A prompt that says "only use read-only commands" is a request, not a control.
It fails open: the model is free to ignore it, and a jailbreak, a confused
tool-call, or an ordinary hallucination is enough to run `scontrol update` on a
production controller. Anything that could drain a node must be impossible to
express, not merely discouraged.

So the allowlist lives here, in code, and is checked on every invocation:

* Only binaries on :data:`ALLOWED` may run at all.
* Each binary carries a set of forbidden subcommands and flags — ``scontrol``
  is permitted for ``show`` and rejected for ``update``, ``delete``,
  ``reconfigure``, ``shutdown``, and so on.
* Shell metacharacters are rejected outright, so ``sinfo; rm -rf /`` cannot be
  smuggled through an argument. Commands are executed without a shell anyway,
  making this defence in depth rather than the only barrier.
* The check is enforced in :func:`guard`, which every execution path calls,
  and the tests drive it with real mutating commands rather than asserting on
  the prompt text.

The threat model is deliberately not "malicious user". It is an agent that has
read a confusing log line at 3am and is about to do something decisive.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field

#: Characters that could chain, redirect, or substitute another command.
SHELL_METACHARACTERS = re.compile(r"[;&|<>`$(){}\[\]\\\n\r]")


@dataclass(frozen=True, slots=True)
class Tool:
    """One permitted binary and the shape of its safe usage."""

    binary: str
    description: str
    #: Subcommands that mutate. Rejected in the first argument position.
    forbidden_subcommands: frozenset[str] = field(default_factory=frozenset)
    #: Flags that mutate or execute, wherever they appear.
    forbidden_flags: frozenset[str] = field(default_factory=frozenset)


#: Every tool the agent may call. Adding to this list is a deliberate act; the
#: default answer to "should the agent be able to run this?" is no.
ALLOWED: dict[str, Tool] = {
    "sinfo": Tool("sinfo", "Node and partition state"),
    "squeue": Tool("squeue", "Job queue"),
    "sacct": Tool("sacct", "Accounting history"),
    "sdiag": Tool("sdiag", "Scheduler diagnostics — cycle counts, RPC and agent queues"),
    "sprio": Tool("sprio", "Priority breakdown per pending job"),
    "sshare": Tool("sshare", "Fairshare state"),
    "scontrol": Tool(
        "scontrol",
        "Cluster state, read paths only",
        # scontrol is the sharpest object in the drawer: `show` is harmless and
        # `update` drains nodes. Permitted narrowly rather than excluded, since
        # `scontrol show config` is often the fastest route to a diagnosis.
        forbidden_subcommands=frozenset(
            {
                "update",
                "delete",
                "create",
                "reconfigure",
                "shutdown",
                "takeover",
                "requeue",
                "requeuehold",
                "hold",
                "release",
                "suspend",
                "resume",
                "cancel",
                "setdebug",
                "setdebugflags",
                "write",
                "reboot",
            }
        ),
    ),
    "sacctmgr": Tool(
        "sacctmgr",
        "Accounting associations and limits, read paths only",
        forbidden_subcommands=frozenset(
            {"add", "create", "modify", "update", "delete", "remove", "archive", "dump", "load"}
        ),
    ),
}

#: Flags forbidden on every tool. Each is a way to turn a read into a write or
#: a shell.
GLOBAL_FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {"-e", "--exec", "--command", "--wrap", "-o0", "--signal"}
)


class ToolDenied(PermissionError):
    """The requested command is not on the read-only surface."""


def guard(command: list[str]) -> None:
    """Raise :class:`ToolDenied` unless ``command`` is provably read-only.

    Called by every execution path. Raising rather than sanitising is
    deliberate: silently stripping a forbidden flag would let the agent believe
    it had acted when it had not, which is its own failure mode.
    """
    if not command:
        raise ToolDenied("empty command")

    binary = command[0].rsplit("/", maxsplit=1)[-1]
    tool = ALLOWED.get(binary)
    if tool is None:
        raise ToolDenied(
            f"{binary!r} is not on the read-only tool surface; "
            f"permitted: {', '.join(sorted(ALLOWED))}"
        )

    for arg in command:
        if SHELL_METACHARACTERS.search(arg):
            raise ToolDenied(f"argument {arg!r} contains shell metacharacters")

    args = command[1:]
    if args:
        first = args[0].lower().lstrip("-")
        if first in tool.forbidden_subcommands:
            raise ToolDenied(
                f"{binary} {args[0]!r} mutates cluster state and is not permitted; "
                f"this agent recommends actions, it does not take them"
            )
        # scontrol accepts `NodeName=x State=DRAIN` without a subcommand word,
        # so any key=value pair that assigns state is refused as well.
        mutating_verb = any(a.lower().lstrip("-") in tool.forbidden_subcommands for a in args)
        for arg in args:
            assigns_state = "=" in arg and arg.split("=", 1)[0].lower() in {
                "state",
                "nodename",
                "jobid",
            }
            if assigns_state and mutating_verb:
                raise ToolDenied(f"{binary} assignment {arg!r} would mutate state")

    forbidden = tool.forbidden_flags | GLOBAL_FORBIDDEN_FLAGS
    for arg in args:
        if arg.lower() in forbidden:
            raise ToolDenied(f"flag {arg!r} is not permitted on {binary}")


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Outcome of a guarded invocation."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def run(command: list[str], *, timeout: float = 30.0) -> ToolResult:
    """Run a read-only command after guarding it.

    Executed without a shell, so metacharacters have no interpreter even if the
    guard were bypassed. A timeout is reported rather than raised, because a
    command that hangs is itself diagnostic information — the storage-stall
    family is identified precisely by commands that block instead of failing.
    """
    guard(command)
    binary = shutil.which(command[0])
    if binary is None:
        return ToolResult(tuple(command), 127, "", f"{command[0]}: not found")

    try:
        completed = subprocess.run(  # noqa: S603
            [binary, *command[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(tuple(command), -1, "", f"timed out after {timeout}s", timed_out=True)

    return ToolResult(tuple(command), completed.returncode, completed.stdout, completed.stderr)


def tool_manifest() -> list[dict[str, object]]:
    """The tool surface, in the shape an MCP server advertises.

    Kept as plain data so the allowlist can be tested, diffed and reviewed
    without standing up a server or importing an MCP SDK.
    """
    return [
        {
            "name": tool.binary,
            "description": tool.description,
            "readOnly": True,
            "forbidden": sorted(tool.forbidden_subcommands | tool.forbidden_flags),
        }
        for tool in sorted(ALLOWED.values(), key=lambda t: t.binary)
    ]
