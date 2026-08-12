#!/bin/bash
# M3 taste-distiller arming script — ANTHONY-RUN ONLY (launchctl and
# ~/.claude/settings.json edits are never a session's call). One shot,
# idempotent. Does four things:
#   1. creates the macroseat namespace in imprint's operator data root and
#      seeds entity-registry.json from the example IF absent (the example holds
#      placeholders; put real entities in the local copy only — client names
#      never land in a networked repo, P25)
#   2. installs the macro-seat Stop hook into ~/.claude/settings.json
#      (idempotent-marker mechanism, marker: macroseat-managed-hook)
#   3. installs + bootstraps the com.macroseat.taste-distiller LaunchAgent
#      (RunAtLoad + every 15 min)
#   4. prints verification + measured hook overhead (<200ms acceptance bar)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LA="$HOME/Library/LaunchAgents"
GUI="gui/$(id -u)"
LABEL="com.macroseat.taste-distiller"
SETTINGS="$HOME/.claude/settings.json"
MARKER="macroseat-managed-hook"
HOOK_CMD="python3 $HERE/distill_spool_hook.py # $MARKER"

# -- 1. macroseat namespace + registry seed ----------------------------------
CFG="${IMPRINT_CONFIG:-$HOME/.config/imprint/config.json}"
ROOT="$(python3 -c "
import json,os
cfg=json.load(open('$CFG'))
print(os.path.join(cfg['data_root'],cfg['operator_slug'],'macroseat'))")"
mkdir -p "$ROOT"
if [ ! -f "$ROOT/entity-registry.json" ]; then
  cp "$HERE/entity-registry.example.json" "$ROOT/entity-registry.json"
  echo "seeded: $ROOT/entity-registry.json (PLACEHOLDERS — edit with real entities)"
else
  echo "exists: $ROOT/entity-registry.json (left untouched)"
fi

# -- 2. Stop hook (idempotent by marker) -------------------------------------
python3 - "$SETTINGS" "$HOOK_CMD" "$MARKER" <<'PY'
import json, sys
settings_path, hook_cmd, marker = sys.argv[1:4]
with open(settings_path) as fh:
    settings = json.load(fh)
hooks = settings.setdefault("hooks", {}).setdefault("Stop", [])
# Remove any prior macroseat-managed entry, then append the current one.
for group in hooks:
    group["hooks"] = [h for h in group.get("hooks", [])
                      if marker not in h.get("command", "")]
hooks[:] = [g for g in hooks if g.get("hooks")]
hooks.append({"matcher": "", "hooks": [
    {"type": "command", "command": hook_cmd, "timeout": 10}]})
with open(settings_path, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
print("installed: macro-seat Stop hook in", settings_path)
PY

# -- 3. LaunchAgent ----------------------------------------------------------
mkdir -p "$HOME/.optimus/logs"
cp "$HERE/$LABEL.plist" "$LA/$LABEL.plist"
launchctl bootout "$GUI/$LABEL" 2>/dev/null || true  # re-run safety
launchctl bootstrap "$GUI" "$LA/$LABEL.plist"
echo "armed: $LABEL (RunAtLoad + 900s)"

# -- 4. verify ---------------------------------------------------------------
echo
echo "verify:"
launchctl list | grep -F "$LABEL" || echo "  WARNING: $LABEL not in launchctl list"
T0=$(python3 -c 'import time; print(time.time())')
echo '{"session_id":"arm-smoke-test","transcript_path":"/dev/null","cwd":"'"$HOME"'"}' \
  | python3 "$HERE/distill_spool_hook.py"
T1=$(python3 -c 'import time; print(time.time())')
python3 -c "print('  hook overhead: %.0f ms (bar: <200)' % (($T1-$T0)*1000))"
tail -1 "$ROOT/distill-queue.ndjson"
echo
echo "The RunAtLoad tick fires now; watch ~/.optimus/logs/taste-distiller.log."
echo "The arm-smoke-test queue line will ledger as transcript_missing — harmless."
