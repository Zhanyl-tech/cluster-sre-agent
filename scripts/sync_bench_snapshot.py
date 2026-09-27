"""Copy the parts of slurm-rca-bench the graph tests read, at one pinned commit.

Why a snapshot rather than a dependency: the scenarios live in the
benchmark's ``scenarios/`` directory, outside its Python package, so a pip or
git-URL install would not ship them; and CI must not need network access to a
sibling repository. So the files are copied verbatim, from a named commit
(``git show <commit>:<path>``, never the working tree, so uncommitted edits
cannot leak in), with a SHA-256 per file recorded in ``SOURCE.json``. The
tests refuse to run against a snapshot whose hashes do not match.

The build therefore does **not** follow the benchmark automatically. When the
benchmark changes, re-run this script against the new commit and re-run the
tests; ``--check`` reports drift without writing anything.

    python scripts/sync_bench_snapshot.py ../slurm-rca-bench --ref origin/main
    python scripts/sync_bench_snapshot.py ../slurm-rca-bench --ref origin/main --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

#: Where the upstream lives. Recorded, not fetched: the script reads a local
#: checkout so it never needs credentials or network access.
UPSTREAM = "https://github.com/Zhanyl-tech/slurm-rca-bench"

DEST = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "slurm_rca_bench"

#: The vocabulary lives here, as a Python dict literal the tests read with
#: ``ast`` (never import) so vendored code is never executed.
SPEC = "src/slurmrca/spec.py"


def _git(repo: Path, *args: str) -> bytes:
    git = shutil.which("git")
    if git is None:
        sys.exit("git not found on PATH")
    # Fixed argv, no shell; the only caller-controlled values are a directory
    # and a ref, both passed as separate arguments.
    return subprocess.run(  # noqa: S603
        [git, "-C", str(repo), *args], check=True, capture_output=True
    ).stdout


def _wanted(repo: Path, commit: str) -> list[str]:
    listing = _git(repo, "ls-tree", "-r", "--name-only", commit).decode().splitlines()
    scenarios = sorted(p for p in listing if p.startswith("scenarios/") and p.endswith(".yaml"))
    if SPEC not in listing:
        sys.exit(f"{SPEC} not found at {commit}")
    return [*scenarios, SPEC]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path, help="local checkout of slurm-rca-bench")
    parser.add_argument("--ref", default="HEAD", help="commit-ish to snapshot (default HEAD)")
    parser.add_argument("--check", action="store_true", help="report drift, write nothing")
    args = parser.parse_args(argv)

    commit = _git(args.repo, "rev-parse", "--verify", f"{args.ref}^{{commit}}").decode().strip()
    files = {
        path: _git(args.repo, "show", f"{commit}:{path}") for path in _wanted(args.repo, commit)
    }
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}

    if args.check:
        recorded = json.loads((DEST / "SOURCE.json").read_text())
        if recorded["files"] == hashes:
            print(f"snapshot matches {commit}")
            return 0
        changed = sorted(set(recorded["files"].items()) ^ set(hashes.items()))
        print(f"snapshot differs from {commit}:")
        for path in sorted({p for p, _ in changed}):
            print(f"  {path}")
        return 1

    if DEST.exists():
        shutil.rmtree(DEST)
    for path, data in files.items():
        target = DEST / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    source = {
        "upstream": UPSTREAM,
        "commit": commit,
        "note": (
            "Verbatim copies made by scripts/sync_bench_snapshot.py with `git show "
            "<commit>:<path>`. Do not edit by hand; the tests check these hashes."
        ),
        "license": "MIT (same author); see the upstream LICENSE",
        "files": hashes,
    }
    (DEST / "SOURCE.json").write_text(json.dumps(source, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(files)} files from {commit} to {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
