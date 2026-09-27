#!/bin/sh
# The read-only surface is a safety control, so prove it holds from the CLI as
# well as from the unit tests: CI fails if any write below is permitted or any
# read below is refused. The writes include, verbatim, every command an audit
# found getting through the first, denylist-based guard (AUDITED_BYPASSES in
# tests/test_readonly.py, which fails if one is missing here), plus a few more.
#
# Usage: scripts/smoke-guard.sh            (uses `csa` on PATH)
#        CSA=.venv/bin/csa scripts/smoke-guard.sh
set -eu

CSA="${CSA:-csa}"
failed=0

allowed() {
  if ! "$CSA" check "$@" >/dev/null; then
    echo "::error::guard refused a read: $*"
    failed=1
  fi
}

denied() {
  if "$CSA" check "$@" >/dev/null 2>&1; then
    echo "::error::guard allowed a write: $*"
    failed=1
  fi
}

allowed sinfo -N
allowed sinfo -n 'node[01-04]'
allowed squeue -h -o '%T'
allowed sacct --delimiter='|' -p
allowed scontrol show node 'gpu[001-008]'
allowed sacctmgr show assoc
allowed sdiag

# the audit's list
denied scontrol -o shutdown
denied scontrol -v reconfigure
denied scontrol -d delete PartitionName=debug
denied scontrol -Q update PartitionName=debug MaxNodes=0
denied scontrol -M c1 create reservation starttime=now duration=60 flags=maint nodes=ALL
denied scontrol --clusters=c1 create reservation starttime=now flags=maint nodes=ALL
denied scontrol top 1234
denied scontrol uhold 1234
denied scontrol schedloglevel 1
denied scontrol fsdampeningfactor 5
denied scontrol power down c1
denied scontrol token username=root lifespan=99999
denied scontrol cancel_reboot c1
denied scontrol upd PartitionName=debug State=DOWN
denied scontrol shutd
denied scontrol
denied sacctmgr -i delete user zhanyl
denied sacctmgr -i modify account root set GrpJobs=0
denied sacctmgr shutdown
denied sacctmgr reconfigure
denied sacctmgr clear stats
denied sacctmgr
denied sdiag -r
denied /tmp/evil/sinfo
denied ./sinfo
# and more: an update, abbreviations each tool really expands to a write
# (scontrol u -> update, sacctmgr shutd -> shutdown), prefixes of sdiag's
# --reset, runawayjobs, a write-capable binary and an absolute path
denied scontrol update NodeName=ALL State=DRAIN
denied scontrol u NodeName=ALL State=DRAIN
denied sacctmgr shutd
denied sacctmgr show runawayjobs
denied sdiag --reset
denied sdiag --res
denied scancel 1
denied /usr/bin/sinfo

if [ "$failed" -ne 0 ]; then
  exit 1
fi
echo "guard smoke: all listed reads allowed, all listed writes denied"
