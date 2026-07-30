#!/usr/bin/env bash
# install-telegram-watchdog.sh — install (and arm) the Telegram-assistant
# keep-alive on THIS Mac. Run it on the desktop, never from a cloud session.
#
# Installs up to two LaunchAgents:
#   com.anthonyflores.fully-aware.telegram-assistant — KeepAlive supervisor
#       that runs ASSISTANT_START_CMD and restarts it within seconds of a
#       crash (skipped when ASSISTANT_LAUNCHD_LABEL says the bridge already
#       has its own agent).
#   com.anthonyflores.fully-aware.telegram-watchdog — runs telegram/watchdog.sh
#       every 300s: health line to state/logs/, wedge detection, and a
#       belt-and-braces restart if the process is somehow gone.
#
# Config lives in gitignored state/telegram-watchdog.env (it may hold the bot
# token). A first run without one copies the example there and exits so you
# can fill it in.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${REPO_ROOT}/state/telegram-watchdog.env"
DEST_DIR="${HOME}/Library/LaunchAgents"
SUP_LABEL="com.anthonyflores.fully-aware.telegram-assistant"
WD_LABEL="com.anthonyflores.fully-aware.telegram-watchdog"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "install-telegram-watchdog: launchd is macOS-only; run this on the desktop." >&2
    exit 1
fi

mkdir -p "${REPO_ROOT}/state/logs" "${DEST_DIR}"

if [[ ! -f "${CONFIG}" ]]; then
    cp "${REPO_ROOT}/telegram/watchdog.env.example" "${CONFIG}"
    echo "install-telegram-watchdog: created ${CONFIG}"
    echo "Fill in ASSISTANT_START_CMD + ASSISTANT_PROCESS_PATTERN (see telegram/README.md), then rerun."
    exit 1
fi
# shellcheck disable=SC1090
source "${CONFIG}"

if [[ -z "${ASSISTANT_PROCESS_PATTERN:-}" ]]; then
    echo "install-telegram-watchdog: ASSISTANT_PROCESS_PATTERN is empty in ${CONFIG}" >&2
    exit 1
fi
if [[ -z "${ASSISTANT_LAUNCHD_LABEL:-}" && -z "${ASSISTANT_START_CMD:-}" ]]; then
    echo "install-telegram-watchdog: set ASSISTANT_START_CMD (or ASSISTANT_LAUNCHD_LABEL" >&2
    echo "if the bridge already has its own agent) in ${CONFIG}" >&2
    exit 1
fi

xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<< "$1"; }

# --- supervisor agent (only when this kit owns the bridge process) ---
if [[ -z "${ASSISTANT_LAUNCHD_LABEL:-}" ]]; then
    START_CMD_XML="$(xml_escape "${ASSISTANT_START_CMD}")"
    WORKDIR_XML="$(xml_escape "${ASSISTANT_WORKDIR:-${HOME}}")"
    cat > "${DEST_DIR}/${SUP_LABEL}.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${SUP_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>exec ${START_CMD_XML}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${WORKDIR_XML}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>${REPO_ROOT}/state/logs/telegram-assistant.out.log</string>
    <key>StandardErrorPath</key>
    <string>${REPO_ROOT}/state/logs/telegram-assistant.err.log</string>
</dict>
</plist>
PLIST
    launchctl unload "${DEST_DIR}/${SUP_LABEL}.plist" 2>/dev/null || true
    launchctl load "${DEST_DIR}/${SUP_LABEL}.plist"
    echo "install-telegram-watchdog: loaded ${SUP_LABEL} (KeepAlive supervisor)."
else
    echo "install-telegram-watchdog: bridge has its own agent (${ASSISTANT_LAUNCHD_LABEL}); no supervisor installed."
fi

# --- watchdog agent (every 300s, per the 5-minute cadence ruling) ---
cat > "${DEST_DIR}/${WD_LABEL}.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${WD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${REPO_ROOT}/telegram/watchdog.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${REPO_ROOT}/state/logs/telegram-watchdog.out.log</string>
    <key>StandardErrorPath</key>
    <string>${REPO_ROOT}/state/logs/telegram-watchdog.err.log</string>
</dict>
</plist>
PLIST
launchctl unload "${DEST_DIR}/${WD_LABEL}.plist" 2>/dev/null || true
launchctl load "${DEST_DIR}/${WD_LABEL}.plist"
echo "install-telegram-watchdog: loaded ${WD_LABEL} (every 5 minutes)."

echo ""
echo "  verify agents:   launchctl list | grep fully-aware.telegram"
echo "  health log:      tail -f ${REPO_ROOT}/state/logs/telegram-watchdog.log"
echo "  bridge output:   tail -f ${REPO_ROOT}/state/logs/telegram-assistant.err.log"
echo "  force restart:   launchctl kickstart -k gui/\$(id -u)/${ASSISTANT_LAUNCHD_LABEL:-${SUP_LABEL}}"
echo "  disarm all:      launchctl unload ${DEST_DIR}/${SUP_LABEL}.plist ${DEST_DIR}/${WD_LABEL}.plist"
echo ""
echo "Now send the bot a message from Telegram to confirm the round trip."
