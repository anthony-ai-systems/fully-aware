#!/usr/bin/env bash
# session-digest-hook.sh -- SessionStart hook body: show the slim boot digest.
#
# A fresh session boots without knowing what the daily pack found. This prints
# state/BOOT-DIGEST.md (a few hundred tokens, written by tools/boot-digest.py at
# the tail of the 05:45 morning-pack run) into the session's opening context.
#
# Three cases, and nothing else:
#   fresh digest (<36h)  -- print it, bounded to the first 4000 bytes.
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
SCAN_LATEST="/Users/anthonyflores/code/fully-aware/state/daily-scan/LATEST.md"
STALE_SECONDS=$((36 * 3600))

[ -f "${DIGEST}" ] || exit 0

# mtime in epoch seconds, portably. GNU coreutils spells it -c %Y; BSD stat
# (macOS, where this hook is installed) spells it -f %m. Each rejects the
# other's flag, and GNU's -f means "filesystem status" -- it can exit 0 with
# unrelated fields on stdout -- so only an all-digits answer counts.
mtime="$(stat -c %Y "${DIGEST}" 2>/dev/null)"
case "${mtime}" in
    ''|*[!0-9]*) mtime="$(stat -f %m "${DIGEST}" 2>/dev/null)" ;;
esac
case "${mtime}" in
    ''|*[!0-9]*) exit 0 ;;
esac

age=$(( $(date +%s) - mtime ))
if [ "${age}" -ge "${STALE_SECONDS}" ]; then
    echo "fully-aware boot digest is stale (>36h) -- check the boot-pack LaunchAgent (com.anthonyflores.fully-aware.boot-pack)"
    exit 0
fi

# head -c, not cat: the generator caps the digest at ~500 tokens plus a
# byte-capped IMPRINT section (<=4000 B, boot-digest.py IMPRINT_CAP_BYTES), but
# this hook is a CONSUMER of a file it does not control (hand-edited,
# half-written, or a future generator with a looser cap). 12000 bytes is ~2x
# the summed generator caps (~2000 B core + 4000 B imprint) -- generous for
# anything legitimate, bounded against dumping a large file into every
# session's opening context.
head -c 12000 "${DIGEST}"

# Ordering gap (2026-08-08 audit): the digest is written at 05:45 and the daily
# scan lands at 06:15, so the "Daily brief:" line baked into the digest is
# always the PREVIOUS day's -- and reads exactly like today's. Close it at the
# point of consumption by quoting the live LATEST.md here. Its first line is the
# brief's own date stamp (docs/DAILY-SCAN.md), so a date before today is the
# stale-scan signal, visible without opening the file.
#
# Same discipline as everything above: absolute path, bounded output, and every
# path exits 0 -- a missing or unreadable brief prints nothing at all.
if [ -f "${SCAN_LATEST}" ]; then
    # head -n 1 | head -c: line-bounded AND byte-bounded, both POSIX. A file
    # with no trailing newline still yields its one line.
    brief="$(head -n 1 "${SCAN_LATEST}" 2>/dev/null | head -c 200)"
    [ -n "${brief}" ] && echo "Daily brief (live): ${brief}"
fi
exit 0
