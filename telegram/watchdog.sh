#!/usr/bin/env bash
# watchdog.sh — 5-minute health check for the Telegram "Ryan's Assistant" bridge.
#
# Run by com.anthonyflores.fully-aware.telegram-watchdog (StartInterval 300).
# Layer 2 of the keep-alive design: layer 1 is launchd KeepAlive on the bridge
# itself, which restarts a crashed process within seconds. This layer catches
# what KeepAlive cannot — a process that is alive but wedged (health command
# failing twice in a row) — and writes a health line every pass so
# state/logs/telegram-watchdog.log is an audit trail of the connection.
#
# Discipline: writes only to gitignored state/; the only thing it ever
# restarts is the configured launchd label on this Mac.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${REPO_ROOT}/state/telegram-watchdog.env"
LOG_DIR="${REPO_ROOT}/state/logs"
LOG="${LOG_DIR}/telegram-watchdog.log"
FAILCOUNT_FILE="${REPO_ROOT}/state/telegram-watchdog.failcount"
SUPERVISOR_DEFAULT_LABEL="com.anthonyflores.fully-aware.telegram-assistant"

mkdir -p "${LOG_DIR}"
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${LOG}"; }

if [[ ! -f "${CONFIG}" ]]; then
    log "ERROR no config at ${CONFIG}; copy telegram/watchdog.env.example there and fill it in"
    exit 1
fi
# shellcheck disable=SC1090
source "${CONFIG}"

PATTERN="${ASSISTANT_PROCESS_PATTERN:-}"
if [[ -z "${PATTERN}" ]]; then
    log "ERROR ASSISTANT_PROCESS_PATTERN is empty in ${CONFIG}"
    exit 1
fi
LABEL="${ASSISTANT_LAUNCHD_LABEL:-${SUPERVISOR_DEFAULT_LABEL}}"

restart_bridge() {
    local reason="$1"
    log "RESTART (${reason}) -> launchctl kickstart -k gui/$(id -u)/${LABEL}"
    launchctl kickstart -k "gui/$(id -u)/${LABEL}" >> "${LOG}" 2>&1 \
        || log "ERROR kickstart failed; is ${LABEL} loaded? (run telegram/install-telegram-watchdog.sh)"
    rm -f "${FAILCOUNT_FILE}"
}

# --- check 1: is the bridge process alive at all? ---
if ! pgrep -f "${PATTERN}" > /dev/null 2>&1; then
    restart_bridge "process '${PATTERN}' not running"
    exit 0
fi

# --- check 2 (optional): can this Mac reach the Telegram Bot API? ---
telegram="skipped"
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    http="$(curl -s -m 10 -o /dev/null -w '%{http_code}' \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" || true)"
    case "${http}" in
        200)     telegram="ok" ;;
        401|404) telegram="BAD-TOKEN(http ${http})" ;;      # config problem — restarting won't fix it
        *)       telegram="unreachable(http ${http:-0})" ;; # network outage — log it, never restart-loop
    esac
fi

# --- check 3 (optional): end-to-end health command; two strikes force a restart ---
health="skipped"
if [[ -n "${ASSISTANT_HEALTH_CMD:-}" ]]; then
    if bash -c "${ASSISTANT_HEALTH_CMD}" > /dev/null 2>&1; then
        health="ok"
        rm -f "${FAILCOUNT_FILE}"
    else
        fails=$(( $(cat "${FAILCOUNT_FILE}" 2>/dev/null || echo 0) + 1 ))
        echo "${fails}" > "${FAILCOUNT_FILE}"
        health="failing(${fails}/2)"
        if (( fails >= 2 )); then
            restart_bridge "health cmd failed ${fails}x in a row (process up but wedged)"
            exit 0
        fi
    fi
fi

log "OK process=up telegram=${telegram} health=${health}"
