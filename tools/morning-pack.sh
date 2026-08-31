#!/usr/bin/env bash
# morning-pack.sh -- regenerate every repo surface, then assemble the boot pack.
#
# Fully Aware lane M5 (surface freshness): surfaces go STALE at 24h, but the armed
# LaunchAgent only ran the assembler -- so a daily-assembled pack would render
# permanently-stale surfaces. This wrapper is the orchestration layer: it FIRST
# regenerates every repo's surface via generate-surface.py (into the Fully Aware-
# local state/surfaces/ cache, so nothing is written into any other repo), THEN
# runs the read-only assembler over the fresh surfaces.
#
# Degrade-not-abort: a per-repo surface-generation FAILURE (hard exit, missing
# repo, unresolvable config) is logged and skipped -- it never aborts the pack.
# (A merely DEGRADED surface still exits 0 and is written with its markers
# inline; the assembler renders those as WARNINGs. That is expected, not a fail.)
#
# D30 discipline is preserved: generate-surface.py and assemble-boot-pack.py stay
# strictly read-only; this wrapper does the orchestration and writes only the
# gitignored state/ outputs (surfaces cache, boot pack, logs).
#
# Only surface-config/v1 configs under tools/configs/ are processed. Non-repo
# configs (seed-manifest.json, ratification-backlog.json) are skipped by schema.
#
# Any args passed to this script are forwarded to assemble-boot-pack.py -- and an
# argument-carrying run is treated as a PREVIEW: it skips the boot-digest step
# entirely (see the digest block below). The LaunchAgent passes no args.

set -uo pipefail  # NOT -e: a per-repo generation failure must degrade, not abort.

# The armed LaunchAgent invokes this script with launchd's bare PATH
# (/usr/bin:/bin:/usr/sbin:/sbin), which lacks Homebrew -- so `gh` is invisible
# and every surface degrades with gh_missing (open_prs + cold-load probes).
# Prepend the Homebrew bin dirs so scheduled runs see the same tools as a shell,
# and ~/.local/bin with them: uv, uvx and codex live only there, and this is the
# ONE environment contract the three lanes share (tools/nightly-fix.sh,
# tools/daily-scan/run-daily-scan.sh and tools/verify-defects.py all build the
# same prefix). A step added here that reaches for uvx must not exit 127 under
# launchd while passing by hand.
export PATH="/opt/homebrew/bin:/usr/local/bin:${HOME}/.local/bin:${PATH}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIGS_DIR="${REPO_ROOT}/tools/configs"
SURFACES_DIR="${REPO_ROOT}/state/surfaces"
LOG_DIR="${REPO_ROOT}/state/logs"
# Written when the defect register step fails, removed when it succeeds. The
# digest reads it, so a failed register is a line a session sees rather than a
# warning buried in a launchd log while the pack shows yesterday's counts.
DEFECTS_FAILED="${REPO_ROOT}/state/defects-status.FAILED"
PY="${FULLY_AWARE_PYTHON:-/usr/bin/python3}"
GEN="${REPO_ROOT}/tools/generate-surface.py"
ASM="${REPO_ROOT}/tools/assemble-boot-pack.py"

mkdir -p "${SURFACES_DIR}" "${LOG_DIR}"

ok=0
failed=0
skipped=0

for cfg in "${CONFIGS_DIR}"/*.json; do
    [ -e "${cfg}" ] || continue
    meta="$("${PY}" - "${cfg}" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print("%s\t%s" % (d.get("schema", ""), d.get("environment", "")))
except Exception:
    print("\t")
PYEOF
)"
    schema="${meta%%$'\t'*}"
    env="${meta#*$'\t'}"

    if [ "${schema}" != "surface-config/v1" ]; then
        echo "morning-pack: skip $(basename "${cfg}") (schema=${schema:-none}, not a surface-config)"
        skipped=$((skipped + 1))
        continue
    fi

    [ -n "${env}" ] || env="$(basename "${cfg}" .json)"
    out="${SURFACES_DIR}/${env}.json"

    echo "morning-pack: generating surface for ${env}"
    if "${PY}" "${GEN}" --config "${cfg}" --out "${out}"; then
        ok=$((ok + 1))
    else
        rc=$?
        echo "morning-pack: WARNING surface generation FAILED for ${env} (exit ${rc}); continuing" >&2
        failed=$((failed + 1))
    fi
done

echo "morning-pack: surfaces regenerated -- ${ok} ok, ${failed} failed, ${skipped} non-repo config(s) skipped."

# Defect register loop: run every verify in registers/defects.json and write
# state/defects-status.json + state/DEFECTS.md. The assembler folds the status
# into section 0 and the digest puts the count on its first line, so this step
# runs BEFORE the assembler -- and on preview runs too, because it only ever
# writes the two gitignored state/ files (a preview that skipped it would render
# yesterday's counts).
#
# Degrade-not-abort, same contract as the surface loop: a broken register or a
# crashed loop logs a WARNING and the pack proceeds over whatever status file
# already exists. A FAILING verify is not a failure here -- it is the data.
#
# But it must not be SILENT: a failure here alone used to leave a fresh-looking
# pack carrying yesterday's counts with no mark on it, and the gate quietly
# stopped enforcing 36 hours later. So the failure leaves a marker file behind,
# which the digest turns into a line on its first screen.
echo "morning-pack: verifying the defect register"
# FULLY_AWARE_PUSH=1 arms the push edge (tools/notify.py) for this one call:
# a defect newly at open-P0 reaches the phone. Tests and hand runs never set it.
if FULLY_AWARE_PUSH=1 "${PY}" "${REPO_ROOT}/tools/verify-defects.py"; then
    rm -f "${DEFECTS_FAILED}"
else
    defects_rc=$?
    printf 'verify-defects FAILED exit=%s at=%s\n' \
        "${defects_rc}" "$(date "+%Y-%m-%dT%H:%M:%S%z")" > "${DEFECTS_FAILED}"
    echo "morning-pack: WARNING defect verify FAILED (exit ${defects_rc}); marker at ${DEFECTS_FAILED}; continuing" >&2
fi

# Imprint bulk export (audit P1-1): dump the captured-judgment store to a local
# markdown file so sessions have an on-disk bulk channel (grep on demand) and
# the digest step below can fold a compact IMPRINT summary in. The export
# is megabytes of raw records -- it stays in gitignored state/, never in the
# pack itself. Doctrine note: this is CONTENT alongside the surface/v1 +
# next-session/v2 contract, an audit-sanctioned deviation (2026-08-07).
#
# Degrade-not-abort, same as the surface loop: a dead imprint venv or a failed
# export logs a WARNING and the pack proceeds without a refreshed store file.
IMPRINT_PY="/Users/anthonyflores/.local/lib/imprint-local/venv/bin/python"
if [ -x "${IMPRINT_PY}" ]; then
    echo "morning-pack: exporting imprint store"
    "${IMPRINT_PY}" -m imprint.cli export --format markdown \
        --output "${REPO_ROOT}/state/imprint-store.md" \
        || echo "morning-pack: WARNING imprint export FAILED (exit $?); continuing" >&2
else
    echo "morning-pack: WARNING imprint venv missing (${IMPRINT_PY}); skipping imprint export" >&2
fi

# Imprint health persistence (xray finding 2026-08-16): the health verdict is
# stdout-only inside imprint by design; this lane change persists it to the
# gitignored state/ cache so external scanners (x-ray file_json probe) can read
# it. Same degrade-not-abort contract as the export above.
if [ -x "${IMPRINT_PY}" ]; then
    echo "morning-pack: persisting imprint health JSON"
    "${IMPRINT_PY}" -m imprint.cli health > "${REPO_ROOT}/state/imprint-health.json" \
        || echo "morning-pack: WARNING imprint health persist FAILED (exit $?); continuing" >&2
fi

echo "morning-pack: assembling boot pack"
"${PY}" "${ASM}" "$@"
asm_rc=$?

# Slim boot digest over the pack the assembler just wrote: a few-hundred-token
# attention summary a fresh session absorbs at boot, pointing at the full pack
# for everything else. It READS state/boot-pack.json and never rewrites it.
#
# Degrade-not-abort, same as the per-repo surface step above: a missing or
# unparseable pack makes the digest a silent no-op (exit 0), and a hard digest
# failure is logged and skipped -- the pack is the deliverable, the digest is a
# convenience over it.
#
# ONLY on an argument-free run. Args are forwarded to the assembler, and the
# assembler's argument surface includes PREVIEW modes (--stdout, --out-json to
# a scratch path) that deliberately do not update state/boot-pack.json. Firing
# the digest after one of those would rewrite state/BOOT-DIGEST.md from a pack
# the run never refreshed -- a preview silently mutating boot state. The armed
# LaunchAgent passes no args, so the daily path is unaffected.
if [ $# -eq 0 ]; then
    echo "morning-pack: generating boot digest"
    "${PY}" "${REPO_ROOT}/tools/boot-digest.py" || {
        rc=$?
        echo "morning-pack: WARNING boot digest FAILED (exit ${rc}); continuing" >&2
    }
else
    echo "morning-pack: args passed, skipping boot digest"
fi

# The pack is what this wrapper is judged on: exit with the ASSEMBLER's status,
# not the digest's.
exit ${asm_rc}
