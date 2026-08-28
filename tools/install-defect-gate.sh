#!/usr/bin/env bash
# install-defect-gate.sh -- register the defect gate as a PreToolUse hook.
#
# Two things happen:
#   1. tools/hooks/defect_gate.py is COPIED to ~/.claude/hooks/defect_gate.py,
#      so the live hook does not depend on this checkout still existing.
#   2. Every settings root on this Mac (~/.claude and each ~/.claude-accounts/*/
#      that has a settings.json) gains ONE entry in hooks.PreToolUse:
#
#        {"matcher": "Task|Agent|Workflow",
#         "hooks": [{"type": "command",
#                    "command": "python3 ~/.claude/hooks/defect_gate.py"}]}
#
# DRY RUN IS THE DEFAULT. Nothing changes until you pass --apply. Every write is
# preceded by a same-day backup of the settings file it touches, and the whole
# thing is idempotent: a root that already has the entry is left alone, so
# running --apply twice leaves exactly one entry.
#
# Usage:
#   bash tools/install-defect-gate.sh              # dry run (default)
#   bash tools/install-defect-gate.sh --apply      # do it
#   bash tools/install-defect-gate.sh --remove     # undo (dry run unless --apply)
#   bash tools/install-defect-gate.sh --root DIR   # pretend DIR is $HOME (tests)
#
# ARMING IS ANTHONY'S. Nothing in this repo runs this script.

set -euo pipefail

MODE="dry-run"          # dry-run | apply | remove | remove-dry
ROOT="${HOME}"
PY="${FULLY_AWARE_PYTHON:-/usr/bin/python3}"
REMOVE="no"
APPLY="no"

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) APPLY="no" ;;
        --apply)   APPLY="yes" ;;
        --remove)  REMOVE="yes" ;;
        --root)
            shift
            [ $# -gt 0 ] || { echo "install-defect-gate: --root needs a directory" >&2; exit 1; }
            ROOT="$1"
            ;;
        -h|--help)
            sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "install-defect-gate: unknown argument '$1' (try --help)" >&2
            exit 1
            ;;
    esac
    shift
done

if [ "$REMOVE" = "yes" ]; then
    if [ "$APPLY" = "yes" ]; then MODE="remove"; else MODE="remove-dry"; fi
else
    if [ "$APPLY" = "yes" ]; then MODE="apply"; else MODE="dry-run"; fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${SCRIPT_DIR}/hooks/defect_gate.py"
HOOK_DIR="${ROOT}/.claude/hooks"
HOOK_DEST="${HOOK_DIR}/defect_gate.py"
COMMAND="python3 ${HOOK_DEST}"
STAMP="$(date +%Y%m%d)"

if [ ! -f "${SOURCE}" ]; then
    echo "install-defect-gate: missing hook body at ${SOURCE}" >&2
    exit 1
fi

case "${MODE}" in
    dry-run)    echo "install-defect-gate: DRY RUN -- nothing will change. Re-run with --apply to do it." ;;
    apply)      echo "install-defect-gate: APPLY -- installing the gate." ;;
    remove-dry) echo "install-defect-gate: DRY RUN of --remove -- nothing will change. Add --apply to do it." ;;
    remove)     echo "install-defect-gate: REMOVE -- uninstalling the gate." ;;
esac
echo "  matcher: Task|Agent|Workflow"
echo "  command: ${COMMAND}"

# ---------------------------------------------------------------- hook body --
case "${MODE}" in
    dry-run)
        if [ -f "${HOOK_DEST}" ] && cmp -s "${SOURCE}" "${HOOK_DEST}"; then
            echo "  hook body ${HOOK_DEST}: already current, no change"
        else
            echo "  hook body ${HOOK_DEST}: would copy from ${SOURCE}"
        fi
        ;;
    apply)
        mkdir -p "${HOOK_DIR}"
        if [ -f "${HOOK_DEST}" ] && cmp -s "${SOURCE}" "${HOOK_DEST}"; then
            echo "  hook body ${HOOK_DEST}: already current, no change"
        else
            cp "${SOURCE}" "${HOOK_DEST}"
            chmod +x "${HOOK_DEST}"
            echo "  hook body ${HOOK_DEST}: copied from ${SOURCE}"
        fi
        ;;
    remove-dry)
        if [ -f "${HOOK_DEST}" ]; then
            echo "  hook body ${HOOK_DEST}: would delete"
        else
            echo "  hook body ${HOOK_DEST}: absent, no change"
        fi
        ;;
    remove)
        if [ -f "${HOOK_DEST}" ]; then
            rm -f "${HOOK_DEST}"
            echo "  hook body ${HOOK_DEST}: deleted"
        else
            echo "  hook body ${HOOK_DEST}: absent, no change"
        fi
        ;;
esac

# ------------------------------------------------------------ settings roots --
ROOTS=""
if [ -f "${ROOT}/.claude/settings.json" ]; then
    ROOTS="${ROOT}/.claude/settings.json"
fi
if [ -d "${ROOT}/.claude-accounts" ]; then
    for account in "${ROOT}"/.claude-accounts/*/; do
        [ -d "${account}" ] || continue
        [ -f "${account}settings.json" ] || continue
        ROOTS="${ROOTS}
${account}settings.json"
    done
fi

if [ -z "${ROOTS}" ]; then
    echo "  no settings.json found under ${ROOT} -- nothing to register"
    exit 0
fi

echo "${ROOTS}" | while IFS= read -r settings; do
    [ -n "${settings}" ] || continue
    "${PY}" - "${MODE}" "${COMMAND}" "${settings}" "${STAMP}" <<'PYEOF'
import json
import os
import shutil
import sys

mode, command, settings_path, stamp = sys.argv[1:5]
matcher = "Task|Agent|Workflow"
backup = "%s.pre-defect-gate-%s.bak" % (settings_path, stamp)


def report(verb):
    print("  %s: %s" % (settings_path, verb))


try:
    with open(settings_path, "r", encoding="utf-8") as handle:
        settings = json.load(handle)
except Exception as exc:                       # never guess at a broken file
    report("UNREADABLE (%s) -- skipped" % exc.__class__.__name__)
    sys.exit(0)

pre = (settings.get("hooks") or {}).get("PreToolUse") or []
present = [
    entry for entry in pre
    if any(str((hook or {}).get("command", "")) == command
           for hook in ((entry or {}).get("hooks") or []))
]


def write(new_pre):
    shutil.copy2(settings_path, backup)
    hooks = settings.setdefault("hooks", {})
    hooks["PreToolUse"] = new_pre
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


if mode in ("dry-run", "apply"):
    if present:
        report("already installed (%d entr%s) -- no change"
               % (len(present), "y" if len(present) == 1 else "ies"))
        sys.exit(0)
    if mode == "dry-run":
        report("would add 1 PreToolUse entry (matcher %s); would back up to %s"
               % (matcher, os.path.basename(backup)))
        sys.exit(0)
    write(list(pre) + [{
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}],
    }])
    report("added 1 PreToolUse entry (matcher %s); backup %s"
           % (matcher, os.path.basename(backup)))
else:                                          # remove-dry | remove
    if not present:
        report("not installed -- no change")
        sys.exit(0)
    if mode == "remove-dry":
        report("would remove %d PreToolUse entr%s; would back up to %s"
               % (len(present), "y" if len(present) == 1 else "ies",
                  os.path.basename(backup)))
        sys.exit(0)
    keep = []
    for entry in pre:
        hooks_in = (entry or {}).get("hooks") or []
        remaining = [h for h in hooks_in
                     if str((h or {}).get("command", "")) != command]
        if not remaining:
            continue                            # entry existed only for us
        if len(remaining) != len(hooks_in):
            entry = dict(entry)
            entry["hooks"] = remaining
        keep.append(entry)
    write(keep)
    report("removed %d PreToolUse entr%s; backup %s"
           % (len(present), "y" if len(present) == 1 else "ies",
              os.path.basename(backup)))
PYEOF
done

echo "install-defect-gate: done (${MODE})."
