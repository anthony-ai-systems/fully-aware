#!/usr/bin/env bash
# install-launchagent.sh -- install (and arm) the Fully Aware boot-pack LaunchAgent.
#
# Fully Aware lane M4, cadence ruling SS6.5: regenerate state/BOOT-PACK.md daily
# at 05:45, before the 6am brief. Lane M5 changed the scheduled command from the
# assembler alone to tools/morning-pack.sh, which regenerates every repo surface
# FIRST (so the 24h-staleness surfaces stay fresh) and THEN runs the read-only
# assembler.
#
# ARMING IS ANTHONY'S, POST-MERGE. The plist points at code on a branch; run
# this ONLY after feat/m5-surface-coverage merges to main. It copies the plist
# into ~/Library/LaunchAgents/ and (re)loads it via launchctl. Running it arms
# the schedule immediately. The currently-armed M4 agent (assembler-only) stays
# live until you rerun this script post-merge.
#
# D30 discipline: generate-surface.py and assemble-boot-pack.py stay read-only;
# the morning-pack.sh wrapper writes only the gitignored state/ outputs
# (BOOT-PACK.md, boot-pack.json, surfaces cache, logs).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.anthonyflores.fully-aware.boot-pack"
SRC="${REPO_ROOT}/launchd/${LABEL}.plist"
DEST_DIR="${HOME}/Library/LaunchAgents"
DEST="${DEST_DIR}/${LABEL}.plist"

if [[ ! -f "${SRC}" ]]; then
    echo "install-launchagent: missing plist at ${SRC}" >&2
    exit 1
fi

# Ensure the gitignored log dir exists (the assembler writes only under state/).
mkdir -p "${REPO_ROOT}/state/logs"
mkdir -p "${DEST_DIR}"

echo "install-launchagent: copying ${SRC}"
echo "                  -> ${DEST}"
cp "${SRC}" "${DEST}"

# Reload cleanly (unload is best-effort; ignore 'not loaded').
launchctl unload "${DEST}" 2>/dev/null || true
launchctl load "${DEST}"

echo "install-launchagent: loaded ${LABEL} (daily 05:45)."
echo "  verify:  launchctl list | grep ${LABEL}"
echo "  run now: launchctl start ${LABEL}"
echo "  disarm:  launchctl unload ${DEST}"
