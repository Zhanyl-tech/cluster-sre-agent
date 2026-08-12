"""The read-only surface must hold against real mutating commands.

These drive :func:`guard` with the actual strings an agent would emit to drain
a node or cancel a job. Asserting that a *prompt* forbids something proves
nothing; the control has to be the code path every execution goes through.
"""

from __future__ import annotations

import pytest

from csa.mcp.readonly import ALLOWED, ToolDenied, guard, run, tool_manifest


class TestPermitted:
    @pytest.mark.parametrize(
        "command",
        [
            ["sinfo"],
            ["sinfo", "-N", "--noheader", "-o", "%N %t"],
            ["squeue", "-h", "-o", "%T"],
            ["sacct", "-n", "-X"],
            ["sdiag"],
            ["sprio", "-l"],
            ["sshare", "-a"],
            ["scontrol", "show", "config"],
            ["scontrol", "show", "node", "c1"],
            ["sacctmgr", "show", "assoc"],
        ],
    )
    def test_read_commands_pass(self, command: list[str]) -> None:
        guard(command)


class TestMutationsRefused:
    """Every one of these is something an agent might plausibly try at 3am."""

    @pytest.mark.parametrize(
        "command",
        [
            ["scontrol", "update", "NodeName=c1", "State=DRAIN"],
            ["scontrol", "update", "NodeName=ALL", "State=RESUME"],
            ["scontrol", "reconfigure"],
            ["scontrol", "shutdown"],
            ["scontrol", "requeue", "1234"],
            ["scontrol", "reboot", "c1"],
            ["sacctmgr", "modify", "account", "root", "set", "GrpJobs=0"],
            ["sacctmgr", "delete", "user", "zhanyl"],
        ],
    )
    def test_state_changing_subcommands_are_denied(self, command: list[str]) -> None:
        with pytest.raises(ToolDenied):
            guard(command)

    @pytest.mark.parametrize(
        "binary",
        ["scancel", "sbatch", "srun", "salloc", "rm", "systemctl", "kubectl", "bash", "sh"],
    )
    def test_unlisted_binaries_are_denied(self, binary: str) -> None:
        # scancel is the interesting one: it is a Slurm command, it is what an
        # agent reaches for, and it destroys a user's running work.
        with pytest.raises(ToolDenied, match="not on the read-only tool surface"):
            guard([binary, "1234"])


class TestInjection:
    @pytest.mark.parametrize(
        "command",
        [
            ["sinfo", ";", "rm", "-rf", "/"],
            ["sinfo", "; scancel -u root"],
            ["squeue", "-o", "%T && scontrol update NodeName=ALL State=DOWN"],
            ["sacct", "$(scancel 1)"],
            ["sinfo", "`scancel 1`"],
            ["sinfo", "-o", "%N\nscancel 1"],
            ["sinfo", "|", "sh"],
        ],
    )
    def test_shell_metacharacters_are_rejected(self, command: list[str]) -> None:
        with pytest.raises(ToolDenied, match="metacharacters"):
            guard(command)

    def test_path_prefixes_do_not_bypass_the_allowlist(self) -> None:
        # An absolute path must be resolved to its binary name before checking,
        # or /usr/bin/scancel walks straight through.
        with pytest.raises(ToolDenied):
            guard(["/usr/bin/scancel", "1"])
        guard(["/usr/bin/sinfo"])

    def test_empty_command_is_denied(self) -> None:
        with pytest.raises(ToolDenied):
            guard([])


class TestScontrolAssignments:
    """scontrol takes key=value pairs, not only subcommand words."""

    def test_state_assignment_with_update_is_denied(self) -> None:
        with pytest.raises(ToolDenied):
            guard(["scontrol", "update", "NodeName=c1", "State=DOWN", "Reason=agent"])

    def test_show_with_a_node_name_is_still_allowed(self) -> None:
        guard(["scontrol", "show", "node", "c1"])


class TestExecution:
    def test_run_refuses_before_executing(self) -> None:
        with pytest.raises(ToolDenied):
            run(["scancel", "1"])

    def test_missing_binary_is_reported_not_raised(self) -> None:
        # sinfo is permitted but absent off-cluster. A clean 127 keeps the
        # agent's error handling identical whether or not Slurm is installed.
        result = run(["sinfo"])
        assert result.exit_code in (0, 127)


class TestManifest:
    def test_every_tool_is_advertised_read_only(self) -> None:
        manifest = tool_manifest()
        assert manifest
        assert all(entry["readOnly"] is True for entry in manifest)
        assert {entry["name"] for entry in manifest} == set(ALLOWED)

    def test_no_write_capable_binary_is_on_the_surface(self) -> None:
        # A regression guard: if someone adds sbatch "just for testing", this
        # fails before it reaches a cluster.
        dangerous = {"scancel", "sbatch", "srun", "salloc", "scrontab"}
        assert not (set(ALLOWED) & dangerous)
