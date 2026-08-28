#!/usr/bin/env bash
# nightly-fix.sh -- the launchd wrapper for the nightly defect-fix lane.
#
# launchd hands a job a bare PATH (/usr/bin:/bin:/usr/sbin:/sbin), which has no
# Homebrew, no gh, and no codex. Everything this lane does depends on those three,
# so the PATH is fixed here, in one place, before the executor starts.
#
# The wrapper is deliberately thin: selection, safety and logging all live in
# tools/nightly-fix.py. Arguments are forwarded, so a hand run can add --item.
#
# NOT ARMED by default. See launchd/com.anthonyflores.fully-aware.nightly-fix.plist
# and tools/install-nightly-fix.sh.

set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

mkdir -p "${REPO_ROOT}/state/logs"

echo "nightly-fix.sh: starting $(date '+%Y-%m-%d %H:%M:%S') in ${REPO_ROOT}"

exec /usr/bin/python3 tools/nightly-fix.py --live "$@"
