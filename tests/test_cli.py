"""The CLI, and the README's claims about it, must match what the code does.

The README quotes `csa causes` output and the graph's edge counts. Both went
stale once (it said "13 edges, 62% measured" over a 12-edge graph), so these
tests read the README and compare it with the real thing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from csa import __version__
from csa.cli import main
from csa.graph import Confidence, DependencyGraph

README = (Path(__file__).parent.parent / "README.md").read_text()
GRAPH = DependencyGraph()


def invoke(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(main, list(args), color=False)
    return result.exit_code, result.output


def _readme_block(after: str) -> str:
    """The fenced block whose first line is ``after``, without that line."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in README.splitlines():
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
        elif current is not None:
            current.append(line)
    for lines in blocks:
        if lines and lines[0] == after:
            return "\n".join(lines[1:])
    raise AssertionError(f"no README block starting with {after!r}")


class TestReadmeAgrees:
    def test_the_causes_transcript_is_real_output(self) -> None:
        code, output = invoke("causes", "slurm.scheduler")
        assert code == 0
        assert _readme_block("$ csa causes slurm.scheduler").strip() == output.strip()

    def test_every_edge_count_in_the_readme_is_current(self) -> None:
        claims = re.findall(r"(\d+) edges, (\d+) measured \((\d+)%\)", README)
        assert claims, "the README should quote the graph's size"
        measured = GRAPH.evidence_counts[Confidence.MEASURED]
        expected = (str(len(GRAPH.edges)), str(measured), str(round(100 * GRAPH.measured_fraction)))
        assert all(claim == expected for claim in claims), claims
        limitation = re.search(r"(\d+) of (\d+) edges measured \((\d+)%\)", README)
        assert limitation is not None
        assert limitation.groups() == (expected[1], expected[0], expected[2])

    def test_stats_prints_what_the_readme_quotes(self) -> None:
        code, output = invoke("stats")
        assert code == 0
        measured = GRAPH.evidence_counts[Confidence.MEASURED]
        pct = round(100 * GRAPH.measured_fraction)
        assert f"{len(GRAPH.edges)} edges, {measured} measured ({pct}%)" in output


class TestCauses:
    def test_degrades_does_not_contradict_itself(self) -> None:
        # The first version listed slurmdbd as a cause and then printed
        # "ruled out by measurement: slurm.slurmdbd" in the same answer.
        code, output = invoke("causes", "slurm.scheduler", "--severity", "degrades")
        assert code == 0
        assert re.search(r"measured\s+slurm\.slurmdbd\s+1 hop", output)
        assert "ruled out" not in output

    def test_halts_lists_the_accounting_path_as_ruled_out(self) -> None:
        _, output = invoke("causes", "slurm.scheduler")
        ruled_out = output.split("ruled out at halts by measurement", 1)[1]
        for component in ("slurm.slurmdbd", "db.mysql", "storage.shared_fs"):
            assert component in ruled_out

    def test_state_save_is_offered_with_its_untested_mode_not_ruled_out(self) -> None:
        # It was printed under "ruled out at halts by measurement" on the
        # strength of an error-mode measurement that says nothing about a stall.
        _, output = invoke("causes", "slurm.scheduler")
        causes, ruled_out = output.split("ruled out at halts by measurement", 1)
        assert re.search(r"inferred\s+storage\.state_save\s+2 hop", causes)
        assert "only through an untested failure mode: a StateSaveLocation *stall*" in causes
        assert "storage.state_save" not in ruled_out

    def test_caveats_of_every_measured_cap_are_printed(self) -> None:
        # slurmdbd, the database and the filesystem also reach the scheduler
        # through slurmdbd -> slurmctld; that measured cap's caveat must show.
        _, output = invoke("causes", "slurm.scheduler")
        caveats = output.split("not covered by those measurements:", 1)[1]
        assert "slurm.slurmdbd → slurm.scheduler:" in caveats
        assert "slurm.slurmdbd → slurm.slurmctld:" in caveats

    def test_a_component_with_no_cause_says_so(self) -> None:
        code, output = invoke("causes", "storage.shared_fs")
        assert code == 0
        assert "no component in the graph can produce that symptom" in output

    def test_unknown_component_is_an_error(self) -> None:
        code, output = invoke("causes", "slurm.nonsense")
        assert code == 1
        assert "unknown component" in output
        assert "slurm.scheduler" in output


class TestCheck:
    @pytest.mark.parametrize(
        "command",
        [
            ["sinfo", "-N"],
            ["sinfo", "-n", "node[01-04]"],
            ["scontrol", "show", "node", "c1"],
            ["sacctmgr", "show", "assoc"],
            ["sacct", "--delimiter=|", "-p"],
        ],
    )
    def test_reads_exit_zero(self, command: list[str]) -> None:
        code, output = invoke("check", *command)
        assert code == 0, output
        assert "ALLOWED" in output

    @pytest.mark.parametrize(
        "command",
        [
            ["scontrol", "update", "NodeName=ALL", "State=DRAIN"],
            ["scontrol", "-o", "shutdown"],
            ["scontrol", "shutd"],
            ["scontrol"],
            ["sacctmgr", "-i", "delete", "user", "x"],
            ["sdiag", "-r"],
            ["sdiag", "--res"],
            ["scancel", "1"],
            ["/usr/bin/sinfo"],
        ],
    )
    def test_writes_exit_one(self, command: list[str]) -> None:
        code, output = invoke("check", *command)
        assert code == 1
        assert "DENIED" in output

    def test_check_reports_where_the_tool_would_run_from(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        bindir.chmod(0o755)
        monkeypatch.setenv("CSA_SLURM_BIN", str(bindir))
        _, output = invoke("check", "sinfo")
        assert "not installed in the trusted directories" in output
        fake = bindir / "sinfo"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        _, output = invoke("check", "sinfo")
        assert f"would run {fake.resolve()}" in output

    def test_an_untrusted_binary_fails_the_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        bindir.chmod(0o755)
        fake = bindir / "sinfo"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o777)
        monkeypatch.setenv("CSA_SLURM_BIN", str(bindir))
        code, output = invoke("check", "sinfo")
        assert code == 1
        assert "would not run" in output


class TestTools:
    def test_every_tool_is_listed_with_its_exclusions(self) -> None:
        code, output = invoke("tools")
        assert code == 0
        for name in (
            "sinfo",
            "squeue",
            "sacct",
            "sdiag",
            "sprio",
            "sshare",
            "scontrol",
            "sacctmgr",
        ):
            assert name in output
        assert "-r/--reset" in output
        assert "runawayjobs" in output

    def test_a_misconfigured_trusted_path_is_shown_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CSA_SLURM_BIN", "relative/bin")
        code, output = invoke("tools")
        assert code == 0
        assert "misconfigured" in output


def test_version() -> None:
    code, output = invoke("--version")
    assert code == 0
    assert __version__ in output
