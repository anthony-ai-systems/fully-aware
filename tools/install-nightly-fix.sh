#!/usr/bin/env bash
# install-nightly-fix.sh -- arm (or disarm) the nightly defect-fix LaunchAgent.
#
# The lane is NOT armed by default and must not be armed until the defect-register
# branch has merged into the real fully-aware checkout. Arming means: at 02:30
# every night this Mac clones a repo, spends Codex tokens, pushes a branch, and
# opens a pull request without anyone present. Merge stays Anthony's.
#
#   bash tools/install-nightly-fix.sh              # dry run: says what it would do
#   bash tools/install-nightly-fix.sh --apply      # copy the plist and load it
#   bash tools/install-nightly-fix.sh --remove     # unload it and delete the copy
#
# Editing the plist in this repo changes nothing on its own; launchd reads only
# the copy in ~/Library/LaunchAgents/, so re-run --apply after any plist edit.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.anthonyflores.fully-aware.nightly-fix"
SRC="${REPO_ROOT}/launchd/${LABEL}.plist"
DEST_DIR="${HOME}/Library/LaunchAgents"
DEST="${DEST_DIR}/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

MODE="dry-run"
for arg in "$@"; do
    case "${arg}" in
        --apply)   MODE="apply" ;;
        --remove)  MODE="remove" ;;
        --dry-run) MODE="dry-run" ;;
        -h|--help)
            sed -n '2,16p' "${BASH_SOURCE[0]}"
            exit 0 ;;
        *)
            echo "install-nightly-fix: unknown argument ${arg}" >&2
            exit 2 ;;
    esac
done

if [[ "${MODE}" != "remove" && ! -f "${SRC}" ]]; then
    echo "install-nightly-fix: missing plist at ${SRC}" >&2
    exit 1
fi

is_loaded() {
    # This is a single-label lookup, not a service-state dump.
    launchctl list "${LABEL}" >/dev/null 2>&1
}

case "${MODE}" in
dry-run)
    echo "install-nightly-fix: DRY RUN. Nothing has been changed."
    echo
    echo "  it would copy:   ${SRC}"
    echo "               ->  ${DEST}"
    echo "  it would ensure: ${REPO_ROOT}/state/logs exists"
    if is_loaded; then
        echo "  it would unload: launchctl bootout ${DOMAIN}/${LABEL}   (it is loaded now)"
    else
        echo "  no unload needed: ${LABEL} is not loaded"
    fi
    echo "  it would load:   launchctl bootstrap ${DOMAIN} ${DEST}"
    echo
    if [[ -f "${DEST}" ]]; then
        echo "  an installed copy already exists at ${DEST}"
    else
        echo "  no installed copy exists yet, so the lane is not armed"
    fi
    echo
    echo "  once armed it runs at 02:30 daily and opens at most one pull request."
    echo "  run with --apply to arm it, --remove to disarm it."
    ;;
apply)
    mkdir -p "${DEST_DIR}"
    mkdir -p "${REPO_ROOT}/state/logs"
    echo "install-nightly-fix: copying ${SRC}"
    echo "                  -> ${DEST}"
    cp "${SRC}" "${DEST}"
    if is_loaded; then
        echo "install-nightly-fix: unloading the running copy first"
        launchctl bootout "${DOMAIN}/${LABEL}"
    fi
    launchctl bootstrap "${DOMAIN}" "${DEST}"
    echo "install-nightly-fix: armed ${LABEL} (daily 02:30)."
    echo "  verify:  launchctl list | grep ${LABEL}"
    echo "  run now: launchctl kickstart -k ${DOMAIN}/${LABEL}"
    echo "  disarm:  bash tools/install-nightly-fix.sh --remove"
    ;;
remove)
    if is_loaded; then
        echo "install-nightly-fix: unloading ${LABEL}"
        launchctl bootout "${DOMAIN}/${LABEL}"
    else
        echo "install-nightly-fix: ${LABEL} was not loaded"
    fi
    if [[ -f "${DEST}" ]]; then
        rm -f "${DEST}"
        echo "install-nightly-fix: removed ${DEST}"
    else
        echo "install-nightly-fix: nothing installed at ${DEST}"
    fi
    echo "install-nightly-fix: the lane is disarmed."
    ;;
esac
