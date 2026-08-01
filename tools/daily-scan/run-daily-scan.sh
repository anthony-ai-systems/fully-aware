#!/usr/bin/env bash
# run-daily-scan.sh -- the Codex-managed daily scan pipeline for Fully Aware.
#
# Three model stages behind one scheduled entry point:
#
#   stage 0  freshness  -- BOOT-PACK.md older than 60 min? re-run morning-pack.sh
#   stage 1  scan       -- Codex (gpt-5.6-sol, READ-ONLY sandbox) scans the 7
#                          canonical repos + surfaces + boot pack
#   stage 2  review     -- Fable (claude -p) judges the scan: kills weak findings,
#                          ranks what survives
#   stage 3  summarize  -- Codex, RESUMING THE SAME THREAD, folds scan + review
#                          into the daily brief
#
# The thread is the point. Stage 1 records the Codex session id in
# state/daily-scan/thread-id and every later run resumes THAT id, so the scanner
# accumulates memory of prior days ("NEW SINCE YESTERDAY" is real, not inferred)
# and Anthony can drop into the same conversation with
# `codex resume $(cat state/daily-scan/thread-id)`.
#
# Degrade-not-abort, same discipline as morning-pack.sh: every stage has its own
# watchdog, a failed stage writes a .FAILED marker with the reason, dependent
# stages are SKIPPED (logged, not silently), and the script ALWAYS exits 0. A
# scheduled run that half-worked must never look like a launchd failure -- the
# log and the markers are the report.
#
# D30 discipline: nothing here writes outside the gitignored state/ tree. The
# Codex sandbox is read-only in both stages; the report files are written by the
# codex CLI itself via -o (the last-message file), never by model-run shell.
#
#   tools/daily-scan/run-daily-scan.sh          # one real run
#   DAILY_SCAN_STUB=1 tools/daily-scan/run-daily-scan.sh   # plumbing only, no tokens

set -uo pipefail  # NOT -e: a failed stage must degrade, not abort.

# launchd spawns with a stripped PATH; resolve everything explicitly
# (same lesson as the SAGA agents: absolute paths or ENOENT).
export PATH="${HOME}/.local/bin:${HOME}/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROMPT_DIR="${REPO_ROOT}/tools/daily-scan"
OUT_DIR="${REPO_ROOT}/state/daily-scan"
LOG_DIR="${REPO_ROOT}/state/logs"
LOG="${LOG_DIR}/daily-scan.log"
RAW_DIR="${OUT_DIR}/raw"
BOOT_PACK="${REPO_ROOT}/state/BOOT-PACK.md"
THREAD_FILE="${OUT_DIR}/thread-id"

DATE="$(date +%F)"
SCAN_FILE="${OUT_DIR}/${DATE}-scan.md"
REVIEW_FILE="${OUT_DIR}/${DATE}-review.md"
BRIEF_FILE="${OUT_DIR}/${DATE}-brief.md"
LATEST_FILE="${OUT_DIR}/LATEST.md"

# Model pins: explicit, never inherited from ~/.codex/config.toml or ~/.claude.
CODEX_MODEL="${DAILY_SCAN_CODEX_MODEL:-gpt-5.6-sol}"
CODEX_EFFORT="${DAILY_SCAN_CODEX_EFFORT:-high}"
FABLE_MODEL="${DAILY_SCAN_FABLE_MODEL:-claude-fable-5}"

# Watchdog budgets (seconds).
PACK_TIMEOUT="${DAILY_SCAN_PACK_TIMEOUT:-600}"    # stage 0: 10 min
SCAN_TIMEOUT="${DAILY_SCAN_SCAN_TIMEOUT:-900}"    # stage 1: 15 min
REVIEW_TIMEOUT="${DAILY_SCAN_REVIEW_TIMEOUT:-600}" # stage 2: 10 min
SUM_TIMEOUT="${DAILY_SCAN_SUM_TIMEOUT:-600}"      # stage 3: 10 min

STUB="${DAILY_SCAN_STUB:-0}"
STUB_THREAD_ID="00000000-0000-4000-8000-0000000stub"

mkdir -p "${OUT_DIR}" "${RAW_DIR}" "${LOG_DIR}"

log() {
    printf '[%s] daily-scan: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" | tee -a "${LOG}"
}

# A stage that fails leaves a marker naming the reason; later stages read the
# marker (not an exit code that has long since been discarded) to decide whether
# to run. Markers are per-date, so yesterday's failure never gates today.
fail_stage() {
    local stage="$1" reason="$2"
    printf '%s\n' "${reason}" > "${OUT_DIR}/${DATE}.${stage}.FAILED"
    log "${stage} FAILED -- ${reason}"
}

stage_failed() {
    [ -f "${OUT_DIR}/${DATE}.$1.FAILED" ]
}

# A stage skipped because a dependency failed is not itself a failure; it gets
# its own marker so a consumer of state/daily-scan/ can tell "no brief because
# the scan died" from "no brief because the summarizer broke".
skip_stage() {
    printf '%s\n' "$2" > "${OUT_DIR}/${DATE}.$1.SKIPPED"
    log "$1 SKIPPED -- $2"
}

# --- watchdog -------------------------------------------------------------
# There is no GNU `timeout` on this Mac. Same bash-native pattern the SAGA
# codex-composer agent uses: background the work, background a sleeper that
# TERMs (then KILLs) the whole process tree on expiry, reap both.

terminate_tree() {
    local pid="$1"
    pkill -TERM -P "${pid}" 2>/dev/null || true
    kill -TERM "${pid}" 2>/dev/null || true
    sleep 10
    pkill -KILL -P "${pid}" 2>/dev/null || true
    kill -KILL "${pid}" 2>/dev/null || true
}

# run_watchdog <seconds> <label> <cmd...>  -- returns the command's exit status
# (or 124 if the watchdog fired). Commands are shell FUNCTIONS that own their
# own redirection; nothing they set in their own scope escapes, so anything the
# parent needs (session ids) is parsed out of the raw log afterwards.
run_watchdog() {
    local secs="$1" label="$2"
    shift 2

    "$@" &
    local work_pid=$!

    # The sleeper detaches from our stdout: it outlives nothing, but while it
    # lives it would otherwise hold the inherited pipe open and hang anything
    # reading this script's output (`run-daily-scan.sh | grep ...`) for the full
    # watchdog budget. It logs by appending to ${LOG} directly.
    (
        sleep "${secs}"
        if kill -0 "${work_pid}" 2>/dev/null; then
            printf '[%s] daily-scan: WATCHDOG %s -- no exit after %ss; terminating pid %s\n' \
                "$(date '+%Y-%m-%dT%H:%M:%S%z')" "${label}" "${secs}" "${work_pid}" >> "${LOG}"
            printf 'TIMEOUT\n' > "${RAW_DIR}/${DATE}.${label}.timeout"
            terminate_tree "${work_pid}"
        fi
    ) >/dev/null 2>&1 &
    local wd_pid=$!

    wait "${work_pid}"
    local rc=$?

    # Kill the sleeper FIRST: killing the subshell alone orphans its `sleep`,
    # which then lingers for the whole budget (a 15-minute zombie per stage).
    pkill -P "${wd_pid}" 2>/dev/null || true
    kill "${wd_pid}" 2>/dev/null || true
    wait "${wd_pid}" 2>/dev/null || true

    if [ -f "${RAW_DIR}/${DATE}.${label}.timeout" ]; then
        rm -f "${RAW_DIR}/${DATE}.${label}.timeout"
        return 124
    fi
    return "${rc}"
}

# --- model shims ----------------------------------------------------------
# DAILY_SCAN_STUB=1 swaps both model calls for canned output so the plumbing
# (dates, thread-id capture + reuse, skip logic, LATEST.md copy) is exercisable
# end-to-end without spending a token. The stub still emits a `session id:` line
# in the raw log, because that parse is exactly the fragile part.

# codex_fresh <prompt_file> <out_file> <raw_log>
# Starts a NEW thread. -o is written by the codex CLI process, not by sandboxed
# model shell, so a read-only sandbox still produces the report file.
codex_fresh() {
    if [ "${STUB}" = "1" ]; then
        printf 'stub-codex-scan\n' > "$3"
        printf 'session id: %s\n' "${STUB_THREAD_ID}" >> "$3"
        printf 'STUB SCAN %s\n' "${DATE}" > "$2"
        return 0
    fi
    codex exec \
        -C "${REPO_ROOT}" \
        -s read-only \
        -m "${CODEX_MODEL}" \
        -c model_reasoning_effort="${CODEX_EFFORT}" \
        -o "$2" \
        - <"$1" > "$3" 2>&1
}

# codex_resume <prompt_file> <out_file> <raw_log> <thread_id>
# `codex exec resume` takes NEITHER -C NOR -s: the working root comes from cwd,
# and the sandbox falls back to config.toml (which is danger-full-access on this
# machine). Both have to be forced -- cwd via the subshell, sandbox via -c.
codex_resume() {
    if [ "${STUB}" = "1" ]; then
        printf 'stub-codex-resume %s\n' "$4" > "$3"
        printf 'session id: %s\n' "$4" >> "$3"
        printf 'STUB BRIEF %s\n' "${DATE}" > "$2"
        return 0
    fi
    (
        cd "${REPO_ROOT}" || exit 1
        codex exec resume "$4" \
            -m "${CODEX_MODEL}" \
            -c model_reasoning_effort="${CODEX_EFFORT}" \
            -c sandbox_mode="read-only" \
            -o "$2" \
            - <"$1"
    ) > "$3" 2>&1
}

# fable_review <prompt_file> <out_file> <raw_log>
fable_review() {
    if [ "${STUB}" = "1" ]; then
        printf 'stub-fable-review\n' > "$3"
        printf 'STUB REVIEW %s\n' "${DATE}" > "$2"
        return 0
    fi
    claude -p --model "${FABLE_MODEL}" <"$1" >"$2" 2>"$3"
}

# The codex banner prints `session id: <uuid>` before the first turn; that is the
# only deterministic handle on the thread (`resume --last` races with every other
# codex run on this machine, and there are always several).
extract_session_id() {
    sed -n 's/^session id: *//p' "$1" | head -1 | tr -d '[:space:]'
}

log "=== run start (date=${DATE}, stub=${STUB}, codex-model=${CODEX_MODEL}) ==="

# Clear TODAY's markers up front. Without this, a second run on the same day
# reads the first run's failure and skips stages that would now succeed -- which
# is exactly the case where Anthony is re-running by hand to fix something.
rm -f "${OUT_DIR}/${DATE}".*.FAILED "${OUT_DIR}/${DATE}".*.SKIPPED

# --- stage 0: boot-pack freshness ----------------------------------------
log "stage0 START -- boot-pack freshness check"
if [ -f "${BOOT_PACK}" ] && [ -n "$(find "${BOOT_PACK}" -maxdepth 0 -mmin -60 2>/dev/null)" ]; then
    log "stage0 OK -- BOOT-PACK.md is fresh (<60 min old); skipping morning-pack"
else
    log "stage0 -- BOOT-PACK.md missing or >60 min old; running tools/morning-pack.sh"
    run_morning_pack() {
        "${REPO_ROOT}/tools/morning-pack.sh" >> "${LOG}" 2>&1
    }
    if run_watchdog "${PACK_TIMEOUT}" stage0 run_morning_pack; then
        log "stage0 OK -- boot pack regenerated"
    else
        rc=$?
        # Stale inputs degrade the scan; they do not invalidate it. Warn and go on.
        fail_stage stage0 "morning-pack.sh exit ${rc} (scan proceeds against a stale boot pack)"
    fi
fi

# --- stage 1: codex scan --------------------------------------------------
log "stage1 START -- codex scan"
SCAN_PROMPT="${PROMPT_DIR}/scan-prompt.md"
SCAN_RAW="${RAW_DIR}/${DATE}-scan.raw.log"

if [ "${STUB}" != "1" ] && ! command -v codex >/dev/null 2>&1; then
    fail_stage stage1 "codex not found on PATH"
elif [ ! -f "${SCAN_PROMPT}" ]; then
    fail_stage stage1 "missing scan prompt at ${SCAN_PROMPT}"
else
    # One rolling thread: resume the recorded id if we have one, otherwise start
    # fresh and record whatever id codex hands back. A resume that fails (session
    # pruned, id corrupt) falls back to a fresh thread rather than losing the day.
    thread_id=""
    [ -s "${THREAD_FILE}" ] && thread_id="$(tr -d '[:space:]' < "${THREAD_FILE}")"

    scan_rc=1
    if [ -n "${thread_id}" ]; then
        log "stage1 -- resuming thread ${thread_id}"
        run_watchdog "${SCAN_TIMEOUT}" stage1 codex_resume "${SCAN_PROMPT}" "${SCAN_FILE}" "${SCAN_RAW}" "${thread_id}"
        scan_rc=$?
        if [ "${scan_rc}" -ne 0 ]; then
            log "stage1 WARNING -- resume of ${thread_id} failed (exit ${scan_rc}); starting a fresh thread"
            thread_id=""
        fi
    fi

    if [ -z "${thread_id}" ]; then
        log "stage1 -- starting a fresh codex thread"
        run_watchdog "${SCAN_TIMEOUT}" stage1 codex_fresh "${SCAN_PROMPT}" "${SCAN_FILE}" "${SCAN_RAW}"
        scan_rc=$?
    fi

    if [ "${scan_rc}" -eq 124 ]; then
        fail_stage stage1 "codex scan hit the ${SCAN_TIMEOUT}s watchdog"
    elif [ "${scan_rc}" -ne 0 ]; then
        fail_stage stage1 "codex scan exited ${scan_rc} (raw log: ${SCAN_RAW})"
    elif [ ! -s "${SCAN_FILE}" ]; then
        fail_stage stage1 "codex scan produced no output at ${SCAN_FILE}"
    else
        new_id="$(extract_session_id "${SCAN_RAW}")"
        if [ -n "${new_id}" ]; then
            printf '%s\n' "${new_id}" > "${THREAD_FILE}"
            log "stage1 OK -- scan at ${SCAN_FILE}; thread ${new_id}"
        else
            # Recoverable: the scan is good, only the handle is missing. Tomorrow
            # starts a fresh thread instead of resuming -- worth a loud line.
            log "stage1 OK -- scan at ${SCAN_FILE}; WARNING no session id in ${SCAN_RAW}"
        fi
    fi
fi

# --- stage 2: fable review ------------------------------------------------
log "stage2 START -- fable review"
REVIEW_PROMPT="${PROMPT_DIR}/review-prompt.md"
REVIEW_RAW="${RAW_DIR}/${DATE}-review.raw.log"

skip_review=""
if stage_failed stage1; then
    skip_review="stage 1 failed, nothing to review"
elif [ ! -f "${REVIEW_PROMPT}" ]; then
    skip_review="missing review prompt at ${REVIEW_PROMPT}"
elif [ "${STUB}" != "1" ] && ! command -v claude >/dev/null 2>&1; then
    skip_review="claude CLI not on PATH"
fi

if [ -n "${skip_review}" ]; then
    # A skipped review is a first-class outcome, not an error: stage 3 still runs
    # and must be able to see, in the file, that the ranking is unreviewed.
    printf 'REVIEW SKIPPED: %s\n' "${skip_review}" > "${REVIEW_FILE}"
    skip_stage stage2 "${skip_review}"
else
    review_input="$(mktemp)"
    cat "${REVIEW_PROMPT}" > "${review_input}"
    printf '\n\n---\n\n## SCAN UNDER REVIEW (%s)\n\n' "${DATE}" >> "${review_input}"
    cat "${SCAN_FILE}" >> "${review_input}"

    run_watchdog "${REVIEW_TIMEOUT}" stage2 fable_review "${review_input}" "${REVIEW_FILE}" "${REVIEW_RAW}"
    review_rc=$?
    rm -f "${review_input}"

    if [ "${review_rc}" -eq 124 ]; then
        printf 'REVIEW SKIPPED: claude hit the %ss watchdog\n' "${REVIEW_TIMEOUT}" > "${REVIEW_FILE}"
        skip_stage stage2 "claude hit the ${REVIEW_TIMEOUT}s watchdog"
    elif [ "${review_rc}" -ne 0 ] || [ ! -s "${REVIEW_FILE}" ]; then
        # Almost always not-logged-in or quota; the stderr log has the detail.
        printf 'REVIEW SKIPPED: claude exited %s (see %s)\n' "${review_rc}" "${REVIEW_RAW}" > "${REVIEW_FILE}"
        skip_stage stage2 "claude exited ${review_rc} (raw log: ${REVIEW_RAW})"
    else
        log "stage2 OK -- review at ${REVIEW_FILE}"
    fi
fi

# --- stage 3: codex summarize --------------------------------------------
log "stage3 START -- codex summarize"
SUM_PROMPT="${PROMPT_DIR}/summarize-prompt.md"
SUM_RAW="${RAW_DIR}/${DATE}-brief.raw.log"

thread_id=""
[ -s "${THREAD_FILE}" ] && thread_id="$(tr -d '[:space:]' < "${THREAD_FILE}")"

skip_sum=""
if stage_failed stage1; then
    skip_sum="stage 1 failed, nothing to summarize"
elif [ ! -f "${SUM_PROMPT}" ]; then
    skip_sum="missing summarize prompt at ${SUM_PROMPT}"
elif [ -z "${thread_id}" ]; then
    skip_sum="no codex thread id recorded at ${THREAD_FILE}"
fi

if [ -n "${skip_sum}" ]; then
    skip_stage stage3 "${skip_sum}"
else
    sum_input="$(mktemp)"
    cat "${SUM_PROMPT}" > "${sum_input}"
    printf '\n\n---\n\n## FABLE REVIEW OF TODAY (%s)\n\n' "${DATE}" >> "${sum_input}"
    cat "${REVIEW_FILE}" >> "${sum_input}"

    run_watchdog "${SUM_TIMEOUT}" stage3 codex_resume "${sum_input}" "${BRIEF_FILE}" "${SUM_RAW}" "${thread_id}"
    sum_rc=$?
    rm -f "${sum_input}"

    if [ "${sum_rc}" -eq 124 ]; then
        fail_stage stage3 "codex summarize hit the ${SUM_TIMEOUT}s watchdog"
    elif [ "${sum_rc}" -ne 0 ]; then
        fail_stage stage3 "codex summarize exited ${sum_rc} (raw log: ${SUM_RAW})"
    elif [ ! -s "${BRIEF_FILE}" ]; then
        fail_stage stage3 "codex summarize produced no output at ${BRIEF_FILE}"
    else
        cp "${BRIEF_FILE}" "${LATEST_FILE}"
        log "stage3 OK -- brief at ${BRIEF_FILE}; LATEST.md updated"
        log "headline: $(head -1 "${BRIEF_FILE}")"
    fi
fi

log "=== run end (date=${DATE}) -- resume the thread with: codex resume ${thread_id:-<none recorded>} ==="
exit 0
