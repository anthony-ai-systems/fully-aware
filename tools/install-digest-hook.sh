#!/usr/bin/env bash
# install-digest-hook.sh -- register session-digest-hook.sh as a SessionStart hook.
#
# Adds ONE entry to hooks.SessionStart in ~/.claude/settings.json:
#
#     bash /Users/anthonyflores/code/fully-aware/tools/session-digest-hook.sh
#
# so a fresh session opens with the slim boot digest instead of nothing (or with
# a stale-digest warning; the hook body decides -- see session-digest-hook.sh).
#
# Idempotent: if an entry already runs session-digest-hook.sh, this prints that
# and changes nothing. Additive: every OTHER SessionStart entry is left byte-for-
# byte alone, including the imprint-local-managed hooks -- this script never
# reorders, rewrites, or removes an entry it did not add.
#
# It rewrites ~/.claude/settings.json (json load -> append -> dump, indent 2),
# after copying it to a timestamped backup alongside it. The backup is taken
# only when there is actually a change to write, so re-running leaves no litter.
#
# ARMING IS ANTHONY'S. Nothing in this repo runs this script; run it by hand
# when you want the digest at boot. Undo = delete the entry (or restore the
# backup this prints).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="${REPO_ROOT}/tools/session-digest-hook.sh"
SETTINGS="${HOME}/.claude/settings.json"
PY="${FULLY_AWARE_PYTHON:-/usr/bin/python3}"

if [[ ! -f "${HOOK}" ]]; then
    echo "install-digest-hook: missing hook body at ${HOOK}" >&2
    exit 1
fi

if [[ ! -f "${SETTINGS}" ]]; then
    echo "install-digest-hook: no settings file at ${SETTINGS} -- refusing to create one" >&2
    exit 1
fi

"${PY}" - "${SETTINGS}" "bash ${HOOK}" <<'PYEOF'
import datetime
import json
import shutil
import sys

settings_path, command = sys.argv[1], sys.argv[2]
marker = "session-digest-hook.sh"

with open(settings_path, "r", encoding="utf-8") as fh:
    settings = json.load(fh)

hooks = settings.setdefault("hooks", {})
session_start = hooks.setdefault("SessionStart", [])

# Already installed? Match on the script name, not the full command, so a
# hand-edited invocation (different interpreter, extra flags) still counts.
for entry in session_start:
    for hook in (entry or {}).get("hooks", []) or []:
        if marker in str(hook.get("command", "")):
            print("install-digest-hook: already installed -- no change.")
            print("  existing: %s" % hook.get("command"))
            sys.exit(0)

backup = "%s.bak-%s" % (settings_path,
                        datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
shutil.copy2(settings_path, backup)

# Appended as its own entry (matcher "" = all session starts), which is how the
# existing SessionStart hooks are shaped -- nothing already there is touched.
session_start.append({
    "matcher": "",
    "hooks": [{"type": "command", "command": command}],
})

with open(settings_path, "w", encoding="utf-8") as fh:
    json.dump(settings, fh, indent=2, ensure_ascii=False)
    fh.write("\n")

print("install-digest-hook: SessionStart hook installed.")
print("  command: %s" % command)
print("  backup:  %s" % backup)
print("  entries: %d SessionStart entr(y/ies) now registered." % len(session_start))
PYEOF

echo "install-digest-hook: verify with -- ${PY} -c \"import json;print(json.load(open('${SETTINGS}'))['hooks']['SessionStart'])\""
echo "install-digest-hook: the hook only shows a digest that exists; generate one with tools/boot-digest.py"
