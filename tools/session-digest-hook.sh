#!/usr/bin/env bash
# session-digest-hook.sh -- SessionStart hook body: show the slim boot digest.
#
# A fresh session boots without knowing what the daily pack found. This prints
# state/BOOT-DIGEST.md (a few hundred tokens, written by tools/boot-digest.py at
# the tail of the 05:45 morning-pack run) into the session's opening context.
#
# Three cases, and nothing else:
#   fresh digest (<36h)  -- cat it.
#   stale digest (>=36h) -- one line saying so; a stale digest is worse than
#                           none, because it reads like current state.
#   no digest            -- print nothing. The hook is advisory; a session with
#                           no pack is a normal session, not a broken one.
#
# ALWAYS exits 0. A SessionStart hook that fails is a session that fails to
# start, and no advisory summary is worth that. Absolute paths throughout: the
# hook runs with an arbitrary cwd (folder = activity, but not this folder).
#
# The digest is ADVISORY STATE, NOT LAW -- SAGA doctrine, repo CLAUDE.md, and
# merge-is-Anthony's bind regardless of anything it says.

set -uo pipefail  # NOT -e: every path here must still exit 0.

DIGEST="/Users/anthonyflores/code/fully-aware/state/BOOT-DIGEST.md"
STALE_SECONDS=$((36 * 3600))

[ -f "${DIGEST}" ] || exit 0

# BSD stat (macOS): -f %m is the mtime in epoch seconds.
mtime="$(stat -f %m "${DIGEST}" 2>/dev/null)" || exit 0
[ -n "${mtime}" ] || exit 0

age=$(( $(date +%s) - mtime ))
if [ "${age}" -ge "${STALE_SECONDS}" ]; then
    echo "fully-aware boot digest is stale (>36h) -- check the daily-scan LaunchAgent"
    exit 0
fi

cat "${DIGEST}"
exit 0
