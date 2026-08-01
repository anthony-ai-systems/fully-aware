#!/usr/bin/env bash
# install-daily-scan-launchagent.sh -- install (and arm) the daily-scan LaunchAgent.
#
# Schedules tools/daily-scan/run-daily-scan.sh at 06:15 daily, 30 minutes behind
# the 05:45 boot-pack agent so the pack is already fresh when the scanner reads it.
#
# ARMING IS ANTHONY'S, POST-MERGE. The plist points at code on the
# feat/codex-daily-scan branch; run this ONLY after that branch merges to main.
# It copies the plist into ~/Library/LaunchAgents/ and (re)loads it via launchctl.
# Running it arms the schedule immediately.
#
# D30 discipline: the pipeline runs Codex under a read-only sandbox and writes
# only the gitignored state/ outputs (scan, review, brief, LATEST.md, thread-id,
# logs). It never commits or pushes.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="com.anthony.fully-aware.daily-scan"
SRC="${REPO_ROOT}/launchd/${LABEL}.plist"
DEST_DIR="${HOME}/Library/LaunchAgents"
DEST="${DEST_DIR}/${LABEL}.plist"

if [[ ! -f "${SRC}" ]]; then
    echo "install-daily-scan-launchagent: missing plist at ${SRC}" >&2
    exit 1
fi

if ! command -v codex >/dev/null 2>&1; then
    # Not fatal -- stage 1 degrades and says so -- but arming a scan with no
    # scanner is almost certainly not what you meant.
    echo "install-daily-scan-launchagent: WARNING codex not on PATH; stage 1 will fail-and-skip" >&2
fi

# Ensure the gitignored output dirs exist (the pipeline writes only under state/).
mkdir -p "${REPO_ROOT}/state/logs" "${REPO_ROOT}/state/daily-scan/raw"
mkdir -p "${DEST_DIR}"

echo "install-daily-scan-launchagent: copying ${SRC}"
echo "                             -> ${DEST}"
cp "${SRC}" "${DEST}"

# Reload cleanly (unload is best-effort; ignore 'not loaded').
launchctl unload "${DEST}" 2>/dev/null || true
launchctl load "${DEST}"

echo "install-daily-scan-launchagent: loaded ${LABEL} (daily 06:15)."
echo "  verify:  launchctl list | grep ${LABEL}"
echo "  run now: launchctl start ${LABEL}"
echo "  disarm:  launchctl unload ${DEST}"
echo "  log:     ${REPO_ROOT}/state/logs/daily-scan.log"
