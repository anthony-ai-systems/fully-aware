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
# Prepend the Homebrew bin dirs so scheduled runs see the same tools as a shell.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIGS_DIR="${REPO_ROOT}/tools/configs"
SURFACES_DIR="${REPO_ROOT}/state/surfaces"
LOG_DIR="${REPO_ROOT}/state/logs"
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
