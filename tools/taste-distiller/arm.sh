#!/bin/bash
# Arm (or disarm) the macro-seat taste distiller. ANTHONY RUNS THIS — never a
# session (merge/arm discipline, same pattern as state/atlas-v2-arm.sh).
#
#   tools/taste-distiller/arm.sh          # arm: hook + spool dir + registry + LaunchAgent
#   tools/taste-distiller/arm.sh disarm   # unload agent + unregister hook (data untouched)
#
# Registry seeding: if state/entity-registry.seed.json exists (gitignored —
# client names never enter the committed tree, P25) and the operator root has
# no entity-registry.json yet, the seed is copied in. Edit the live registry
# at <operator_root>/entity-registry.json afterwards; the seed is only a
# bootstrap.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST_SRC="$REPO/launchd/com.macroseat.taste-distiller.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.macroseat.taste-distiller.plist"
LABEL="com.macroseat.taste-distiller"
OP_ROOT="$(/usr/bin/python3 -c "
import sys; sys.path.insert(0, '$REPO/tools/taste-distiller')
from common import operator_root; print(operator_root())")"

if [[ "${1:-arm}" == "disarm" ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    /usr/bin/python3 "$REPO/tools/taste-distiller/register_hook.py" unregister
    echo "disarmed: LaunchAgent unloaded, hook unregistered. Spool/ledger/registry left in place at $OP_ROOT"
    exit 0
fi

echo "== macro-seat taste distiller: arming =="

# 1. Spool dir + registry seed
mkdir -p "$OP_ROOT/spool"
if [[ ! -f "$OP_ROOT/entity-registry.json" ]]; then
    if [[ -f "$REPO/state/entity-registry.seed.json" ]]; then
        cp "$REPO/state/entity-registry.seed.json" "$OP_ROOT/entity-registry.json"
        chmod 600 "$OP_ROOT/entity-registry.json"
        echo "registry: seeded $OP_ROOT/entity-registry.json"
    else
        echo "registry: NO SEED FOUND — distiller will scope everything global until $OP_ROOT/entity-registry.json exists"
    fi
else
    echo "registry: already present, not touching it"
fi

# 2. Stop hook (idempotent marker mechanism)
/usr/bin/python3 "$REPO/tools/taste-distiller/register_hook.py" register

# 3. Hook overhead acceptance check (<200 ms, spec §2.4)
SPOOL_TMP="$(mktemp -d)"
START=$(/usr/bin/python3 -c 'import time; print(time.time())')
echo '{"session_id":"arm-smoke","transcript_path":"/tmp/none.jsonl","cwd":"/tmp"}' \
    | MACROSEAT_SPOOL_DIR="$SPOOL_TMP" /usr/bin/python3 "$REPO/tools/taste-distiller/spool_hook.py"
END=$(/usr/bin/python3 -c 'import time; print(time.time())')
MS="$(/usr/bin/python3 -c "print(round(($END - $START) * 1000))")"
rm -rf "$SPOOL_TMP"
echo "hook overhead: ${MS} ms (budget 200)"
[[ "$MS" -lt 200 ]] || { echo "FAIL: hook over budget"; exit 1; }

# 4. LaunchAgent
mkdir -p "$HOME/.optimus/logs" "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
sleep 2
launchctl list | grep "$LABEL" || { echo "FAIL: agent not loaded"; exit 1; }

echo "== armed. Worker fires every 15 min; first pass drains any quiet sessions already spooled. =="
echo "Backfill (optional, separate step): /usr/bin/python3 $REPO/tools/taste-distiller/backfill.py --dry-run"
