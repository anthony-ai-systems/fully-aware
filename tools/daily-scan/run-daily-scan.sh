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
# D30 discipline: this script writes only under the gitignored state/ tree --
# outputs, per-stage raw logs, and the transient prompt-input files it composes
# (those go to state/daily-scan/raw/, never /tmp). Two side effects are outside
# that tree and are not ours to relocate: the codex CLI records its own rollouts
# under ~/.codex, and stage 0 runs tools/morning-pack.sh, which writes the boot
# pack. The Codex sandbox is read-only in both model stages; the report files are
# written by the codex CLI itself via -o (the last-message file), never by
# model-run shell. The runner's only edit to a report is the `Daily brief --
# <date>` line it prepends to the brief before copying it to LATEST.md.
#
#   tools/daily-scan/run-daily-scan.sh          # one real run
#   DAILY_SCAN_STUB=1 tools/daily-scan/run-daily-scan.sh   # plumbing only, no tokens
#
# Stub mode stubs ALL THREE model calls and stage 0 as well, so a stub run spends
# no tokens and does not regenerate the boot pack. It still writes the state/
# outputs, markers and logs -- that plumbing is the thing being exercised.

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
MANIFEST="${REPO_ROOT}/tools/configs/seed-manifest.json"
LOCK_DIR="${OUT_DIR}/.lock"
PY="${FULLY_AWARE_PYTHON:-/usr/bin/python3}"

DATE="$(date +%F)"
SCAN_FILE="${OUT_DIR}/${DATE}-scan.md"
REVIEW_FILE="${OUT_DIR}/${DATE}-review.md"
BRIEF_FILE="${OUT_DIR}/${DATE}-brief.md"
LATEST_FILE="${OUT_DIR}/LATEST.md"
PR_STATE_FILE="${RAW_DIR}/${DATE}-pr-state.md"

# Model pins: explicit, never inherited from ~/.codex/config.toml or ~/.claude.
CODEX_MODEL="${DAILY_SCAN_CODEX_MODEL:-gpt-5.6-sol}"
CODEX_EFFORT="${DAILY_SCAN_CODEX_EFFORT:-high}"
FABLE_MODEL="${DAILY_SCAN_FABLE_MODEL:-claude-fable-5}"

# Watchdog budgets (seconds).
PACK_TIMEOUT="${DAILY_SCAN_PACK_TIMEOUT:-600}"    # stage 0: 10 min
PR_TIMEOUT="${DAILY_SCAN_PR_TIMEOUT:-120}"        # pre-stage-1 gh collection: 2 min
SCAN_TIMEOUT="${DAILY_SCAN_SCAN_TIMEOUT:-900}"    # stage 1: 15 min
REVIEW_TIMEOUT="${DAILY_SCAN_REVIEW_TIMEOUT:-600}" # stage 2: 10 min
SUM_TIMEOUT="${DAILY_SCAN_SUM_TIMEOUT:-600}"      # stage 3: 10 min

# Retention.
RETAIN_DAYS="${DAILY_SCAN_RETAIN_DAYS:-30}"       # dated outputs + raw logs
LOG_MAX_BYTES="${DAILY_SCAN_LOG_MAX_BYTES:-1048576}"  # rotate daily-scan.log at ~1 MB
THREAD_MAX_DAYS="${DAILY_SCAN_THREAD_MAX_DAYS:-30}"   # rotate the rolling thread monthly

STUB="${DAILY_SCAN_STUB:-0}"
STUB_THREAD_ID="00000000-0000-4000-8000-0000000stub"

mkdir -p "${OUT_DIR}" "${RAW_DIR}" "${LOG_DIR}"

log() {
    printf '[%s] daily-scan: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" | tee -a "${LOG}"
}

# --- log rotation ---------------------------------------------------------
# One append-only log per lane grows without bound; roll it before writing this
# run's first line so a single .1 generation is always the whole recent past.
if [ -f "${LOG}" ]; then
    log_bytes="$(wc -c < "${LOG}" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "${log_bytes}" ] && [ "${log_bytes}" -gt "${LOG_MAX_BYTES}" ] 2>/dev/null; then
        mv -f "${LOG}" "${LOG}.1" 2>/dev/null || true
    fi
fi

# --- concurrency lock -----------------------------------------------------
# mkdir is the atomic primitive: it either creates the directory or fails, with
# no test-then-create window. A second run (launchd firing while yesterday's
# 15-minute scan is still going, or Anthony re-running by hand) must exit
# QUIETLY and successfully -- two codex threads writing the same dated files is
# the failure this prevents.
#
# Liveness is decided by the OWNER'S PID, never by mtime. The mtime version of
# this lock failed two adversarial rounds: a lock's mtime is set when it is
# created and never touched again, so a run that legitimately outlives the
# watchdog budget gets its lock broken under it, while a crashed run's lock is
# obeyed for the whole budget (a `kill -9` parked the lane for 53 minutes).
# `kill -0` asks the kernel instead of guessing from a timestamp.
#
# The other half of the design is BREAK-AND-EXIT: whoever clears a dead owner's
# lock does NOT then run. Breaking and acquiring in one pass is what let several
# racers come out of an 8-way start believing they held the lock (34 of 115
# trials, measured). Clearing is a one-shot service to the NEXT invocation --
# launchd's next fire, or a second manual run. A crash therefore costs one extra
# run: the first clears, the second scans.
LOCK_PID_FILE="${LOCK_DIR}/pid"
LOCK_STALE_TMP="${LOCK_DIR}.stale.$$"

pid_alive() {
    case "${1:-}" in
        ''|*[!0-9]*) return 1 ;;
    esac
    kill -0 "$1" 2>/dev/null
}

read_pid() {  # read_pid <pid-file>
    tr -d '[:space:]' < "$1" 2>/dev/null
}

# The acquirer writes its pid one statement after the mkdir, so a racer can
# arrive in between and see an empty lock. Settle briefly before calling a
# pid-less lock dead: half a second of patience here removes the only window in
# which a live run's lock can be misread as abandoned.
read_owner_pid_settled() {
    local i owner
    for i in 1 2 3 4 5 6 7 8 9 10; do
        owner="$(read_pid "${LOCK_PID_FILE}")"
        [ -n "${owner}" ] && { printf '%s' "${owner}"; return 0; }
        sleep 0.05
    done
    return 0
}

# `mv a b` moves a INSIDE b when b already exists -- a give-back that lands one
# level down would report success while leaving the lock slot free. Refuse when
# the slot is taken, and re-check for the nested name in case it was taken
# between the test and the rename.
give_back_lock() {
    [ -e "${LOCK_DIR}" ] && return 1
    mv "${LOCK_STALE_TMP}" "${LOCK_DIR}" 2>/dev/null || return 1
    if [ -d "${LOCK_DIR}/$(basename "${LOCK_STALE_TMP}")" ]; then
        mv "${LOCK_DIR}/$(basename "${LOCK_STALE_TMP}")" "${LOCK_STALE_TMP}" 2>/dev/null || true
        return 1
    fi
    return 0
}

# Release only what we still own. A run whose lock was cleared out from under it
# (see the loud warning below) must not delete the successor's lock on the way
# out.
release_lock() {
    [ "$(read_pid "${LOCK_PID_FILE}")" = "$$" ] || return 0
    rm -rf "${LOCK_DIR}" 2>/dev/null || true
}

if mkdir "${LOCK_DIR}" 2>/dev/null; then
    printf '%s\n' "$$" > "${LOCK_PID_FILE}"
    # Subshells reset traps, so only the top-level script releases the lock.
    trap release_lock EXIT
else
    lock_owner="$(read_owner_pid_settled)"
    if pid_alive "${lock_owner}"; then
        log "LOCK held by live pid ${lock_owner}; exiting 0 without running"
        exit 0
    fi
    # Re-read at the LAST possible moment. Between the read above and this line
    # another racer can have broken the same dead lock and a third can have
    # acquired a fresh one -- moving THAT aside would leave the slot open for a
    # fourth run while its owner is still working. Requiring the pid to be
    # unchanged and still dead shrinks the window from "however long the
    # scheduler took" to the gap between these two syscalls.
    if [ "$(read_pid "${LOCK_PID_FILE}")" != "${lock_owner}" ] || pid_alive "$(read_pid "${LOCK_PID_FILE}")"; then
        log "LOCK changed hands while we looked at it; exiting 0"
        exit 0
    fi
    # One rename() decides the breaker: exactly one racer can move the old lock
    # aside, everyone else's mv fails and they leave.
    if ! mv "${LOCK_DIR}" "${LOCK_STALE_TMP}" 2>/dev/null; then
        log "LOCK stale-break lost to another run; exiting 0"
        exit 0
    fi
    # Re-verify AFTER the move. If the owner turns out to be alive we lost a
    # tight race against an acquirer that had not yet written its pid; hand the
    # lock straight back.
    moved_owner="$(read_pid "${LOCK_STALE_TMP}/pid")"
    if pid_alive "${moved_owner}"; then
        if give_back_lock; then
            log "LOCK owner ${moved_owner} is alive after all; lock returned, exiting 0"
        else
            rm -rf "${LOCK_STALE_TMP}" 2>/dev/null || true
            log "LOCK WARNING -- cleared a live lock (pid ${moved_owner}) and could not return it; one-time overlap possible"
        fi
        exit 0
    fi
    rm -rf "${LOCK_STALE_TMP}" 2>/dev/null || true
    log "LOCK stale lock (dead pid ${lock_owner:-unknown}) cleared; re-run to start a scan"
    exit 0
fi

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
# TERMs (then KILLs) the work on expiry, reap both.
#
# The kill is by PROCESS GROUP, not by pid. `codex` and `claude` spawn their own
# children (node, sandbox helpers, git); killing the direct child and its
# immediate children by `pkill -P` leaves grandchildren alive, holding the log
# fds open and burning tokens after the watchdog has already given up. Job
# control (`set -m`) makes each backgrounded job a process-group leader whose
# pgid equals its pid, so `kill -TERM -$pid` reaches the entire tree in one call.

terminate_tree() {
    local pid="$1"
    # Negative pid = the whole process group. -P fallbacks cover the case where
    # job control was unavailable and the child never became a group leader.
    kill -TERM "-${pid}" 2>/dev/null || true
    pkill -TERM -P "${pid}" 2>/dev/null || true
    kill -TERM "${pid}" 2>/dev/null || true
    sleep 10
    kill -KILL "-${pid}" 2>/dev/null || true
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

    # Everything from the launch to the reap runs in ONE subshell whose stderr
    # is redirected to a per-stage watchdog log. When the watchdog kills the
    # job, bash's job control announces it ("Terminated: 15") on the stderr of
    # the frame that ran `wait` -- under launchd that is daily-scan.err.log, so
    # a working watchdog looked like a crashing one. Nothing real is lost:
    # every staged function owns its own redirection, so the only stderr this
    # frame can produce is bash's own chatter, and it is kept (in raw/, beside
    # the CLI logs) rather than discarded, in case it is ever something else.
    local wd_log="${RAW_DIR}/${DATE}.${label}.watchdog.log"

    (
        # Monitor mode only for the launch, so the staged work lands in its own
        # process group; restored immediately so the rest of this subshell (and
        # the sleeper below) keeps the plain non-interactive behaviour.
        set -m
        "$@" &
        work_pid=$!
        set +m

        # The sleeper detaches from our stdout: it outlives nothing, but while
        # it lives it would otherwise hold the inherited pipe open and hang
        # anything reading this script's output (`run-daily-scan.sh | grep ...`)
        # for the full watchdog budget. It logs by appending to ${LOG} directly.
        (
            sleep "${secs}"
            if kill -0 "${work_pid}" 2>/dev/null; then
                printf '[%s] daily-scan: WATCHDOG %s -- no exit after %ss; terminating pid %s\n' \
                    "$(date '+%Y-%m-%dT%H:%M:%S%z')" "${label}" "${secs}" "${work_pid}" >> "${LOG}"
                printf 'TIMEOUT\n' > "${RAW_DIR}/${DATE}.${label}.timeout"
                terminate_tree "${work_pid}"
            fi
        ) >/dev/null 2>&1 &
        wd_pid=$!

        wait "${work_pid}"
        rc=$?

        # Kill the sleeper FIRST: killing the subshell alone orphans its `sleep`,
        # which then lingers for the whole budget (a 15-minute zombie per stage).
        pkill -P "${wd_pid}" 2>/dev/null || true
        kill "${wd_pid}" 2>/dev/null || true
        wait "${wd_pid}" 2>/dev/null || true

        exit "${rc}"
    ) 2>>"${wd_log}"
    local rc=$?

    # An empty watchdog log is the normal case; do not leave 5 of them per day.
    [ -s "${wd_log}" ] || rm -f "${wd_log}"

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
    IMPRINT_CAPTURE_ORIGIN=automation codex exec \
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
        IMPRINT_CAPTURE_ORIGIN=automation codex exec resume "$4" \
            -m "${CODEX_MODEL}" \
            -c model_reasoning_effort="${CODEX_EFFORT}" \
            -c sandbox_mode="read-only" \
            -o "$2" \
            - <"$1"
    ) > "$3" 2>&1
}

# fable_review <prompt_file> <out_file> <raw_log>
#
# PROVENANCE (audit P1-6): `claude -p` boots with Anthony's user-level hooks --
# including the imprint SessionStart injection and the fully-aware boot digest.
# The review seat is therefore SEASONED with captured-judgment memory and boot
# state, not a clean-room reader of the scan. Declared deliberately rather than
# suppressed: the seat reviews Anthony's repos, and Anthony's captured judgment
# is signal for that job. If a clean-room seat is ever wanted instead, strip
# the hooks for this invocation rather than deleting this note.
fable_review() {
    if [ "${STUB}" = "1" ]; then
        printf 'stub-fable-review\n' > "$3"
        printf 'STUB REVIEW %s\n' "${DATE}" > "$2"
        return 0
    fi
    IMPRINT_CAPTURE_ORIGIN=automation claude -p --model "${FABLE_MODEL}" <"$1" >"$2" 2>"$3"
}

# The codex banner prints `session id: <uuid>` before the first turn; that is the
# only deterministic handle on the thread (`resume --last` races with every other
# codex run on this machine, and there are always several).
extract_session_id() {
    sed -n 's/^session id: *//p' "$1" | head -1 | tr -d '[:space:]'
}

log "=== run start (date=${DATE}, stub=${STUB}, codex-model=${CODEX_MODEL}) ==="

# Clear TODAY's markers AND today's artifacts up front. Without this, a second
# run on the same day reads the first run's failure and skips stages that would
# now succeed -- exactly the case where Anthony is re-running by hand to fix
# something. The artifacts go too: a rerun that dies at stage 1 must not leave
# this morning's dead scan sitting next to yesterday's live brief, looking like
# a matched pair.
rm -f "${OUT_DIR}/${DATE}".*.FAILED "${OUT_DIR}/${DATE}".*.SKIPPED \
      "${SCAN_FILE}" "${REVIEW_FILE}" "${BRIEF_FILE}"

# --- retention ------------------------------------------------------------
# Dated outputs and raw logs age out at ${RETAIN_DAYS} days. LATEST.md and
# thread-id are named explicitly out of the sweep -- they are current state, not
# history, and thread-id in particular must survive any amount of idleness short
# of the monthly rotation below.
find "${OUT_DIR}" -maxdepth 1 -type f \
    \( -name '*-scan.md' -o -name '*-review.md' -o -name '*-brief.md' \
       -o -name '*.FAILED' -o -name '*.SKIPPED' \) \
    -mtime "+${RETAIN_DAYS}" -delete 2>/dev/null || true
find "${RAW_DIR}" -maxdepth 1 -type f -mtime "+${RETAIN_DAYS}" -delete 2>/dev/null || true

# Thread rotation. A rolling thread is the design, but an unbounded one
# eventually hits the model's context limit mid-scan and takes the day with it.
# Monthly is the rule: a thread whose id file has not been touched in
# ${THREAD_MAX_DAYS} days is retired and stage 1 starts fresh.
if [ -s "${THREAD_FILE}" ] && [ -z "$(find "${THREAD_FILE}" -maxdepth 0 -mtime "-${THREAD_MAX_DAYS}" 2>/dev/null)" ]; then
    log "thread ROTATED -- ${THREAD_FILE} older than ${THREAD_MAX_DAYS} days; starting a fresh thread"
    : > "${THREAD_FILE}"
fi

# --- stage 0: boot-pack freshness ----------------------------------------
log "stage0 START -- boot-pack freshness check"
if [ "${STUB}" = "1" ]; then
    # Stub mode must not regenerate the boot pack: morning-pack.sh is a real
    # writer, and "plumbing only" has to mean it.
    log "stage0 STUBBED -- freshness check and morning-pack.sh skipped"
elif [ -f "${BOOT_PACK}" ] && [ -n "$(find "${BOOT_PACK}" -maxdepth 0 -mmin -60 2>/dev/null)" ]; then
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

# --- pre-stage-1: open-PR state ------------------------------------------
# The scanner CANNOT run `gh` itself. Codex's read-only sandbox also blocks the
# network, so a sandboxed `gh pr list` dies with "error connecting to
# api.github.com" -- verified, not theorised. So the runner collects PR state
# out here, where the network exists, and hands it to the model as DATA appended
# to the prompt. Failure is soft in every direction: no gh, no auth, a repo that
# is not a GitHub remote, or the whole thing hitting its own short watchdog just
# means the scan runs without a PR block and says so.
#
# PR scope is pinned to each surface config's canonical `gh_repo` when declared
# (`gh pr list -R`): a rich-lineage fork's checkout remotes must not decide
# which repo's PRs count as evidence (atlas read upstream's PRs instead of the
# six canonical ones, 2026-08-10 scan). Checkout inference is the fallback for
# repos whose config declares no gh_repo.
log "prs START -- collecting open-PR state for the scan prompt"

manifest_pr_targets() {
    # One line per manifest repo: repo_path<TAB>gh_repo. gh_repo comes from the
    # surface config sitting next to the manifest (tools/configs/<env>.json);
    # empty when the config is missing or declares none.
    "${PY}" - "${MANIFEST}" <<'PYEOF' 2>/dev/null || true
import json, os, sys
try:
    with open(sys.argv[1]) as fh:
        data = json.load(fh)
except Exception:
    sys.exit(0)
cfg_dir = os.path.dirname(sys.argv[1])
for repo in data.get("repos", []):
    path = repo.get("repo_path")
    if not path:
        continue
    gh_repo = ""
    env = repo.get("environment", "")
    if env:
        try:
            with open(os.path.join(cfg_dir, env + ".json")) as fh:
                gh_repo = json.load(fh).get("gh_repo", "") or ""
        except Exception:
            gh_repo = ""
    print("%s\t%s" % (path, gh_repo))
PYEOF
}

collect_pr_state() {
    {
        printf '## OPEN PULL REQUESTS (collected by the runner, %s)\n\n' "${DATE}"
        printf 'Collected OUTSIDE the sandbox with `gh pr list --limit 10` per manifest repo,\n'
        printf 'pinned to the surface config'\''s canonical `gh_repo` where one is declared\n'
        printf '(shown in the heading); checkout-inferred otherwise.\n'
        printf 'This is your PR state -- you cannot reach the network yourself. Treat it as\n'
        printf 'evidence, and say so if a repo block reports an error instead of a list.\n\n'
        while IFS=$'\t' read -r repo_path gh_repo; do
            [ -n "${repo_path}" ] || continue
            if [ -n "${gh_repo}" ]; then
                printf '### %s (gh: %s)\n\n```\n' "${repo_path}" "${gh_repo}"
                gh pr list -R "${gh_repo}" --limit 10 2>&1 || printf '(gh pr list failed -- see the line above)\n'
            else
                printf '### %s\n\n```\n' "${repo_path}"
                if [ ! -d "${repo_path}" ]; then
                    printf '(repo path does not exist)\n'
                else
                    (cd "${repo_path}" && gh pr list --limit 10 2>&1) || printf '(gh pr list failed -- see the line above)\n'
                fi
            fi
            printf '```\n\n'
        done < <(manifest_pr_targets)
    } > "${PR_STATE_FILE}" 2>&1
}

if [ "${STUB}" = "1" ]; then
    printf '## OPEN PULL REQUESTS (STUBBED %s)\n\n(no gh calls in stub mode)\n' "${DATE}" > "${PR_STATE_FILE}"
    log "prs STUBBED -- wrote a placeholder block to ${PR_STATE_FILE}"
elif ! command -v gh >/dev/null 2>&1; then
    printf '## OPEN PULL REQUESTS (%s)\n\nUNAVAILABLE: `gh` is not on the runner PATH.\n' "${DATE}" > "${PR_STATE_FILE}"
    log "prs SKIPPED -- gh not on PATH; scan proceeds without PR state"
elif [ ! -f "${MANIFEST}" ]; then
    printf '## OPEN PULL REQUESTS (%s)\n\nUNAVAILABLE: manifest missing at %s.\n' "${DATE}" "${MANIFEST}" > "${PR_STATE_FILE}"
    log "prs SKIPPED -- missing manifest at ${MANIFEST}"
else
    run_watchdog "${PR_TIMEOUT}" prs collect_pr_state
    pr_rc=$?
    if [ "${pr_rc}" -eq 124 ]; then
        printf '## OPEN PULL REQUESTS (%s)\n\nUNAVAILABLE: collection hit the %ss watchdog.\n' "${DATE}" "${PR_TIMEOUT}" > "${PR_STATE_FILE}"
        log "prs TIMEOUT -- ${PR_TIMEOUT}s watchdog fired; scan proceeds without PR state"
    elif [ "${pr_rc}" -ne 0 ] || [ ! -s "${PR_STATE_FILE}" ]; then
        printf '## OPEN PULL REQUESTS (%s)\n\nUNAVAILABLE: collection exited %s.\n' "${DATE}" "${pr_rc}" > "${PR_STATE_FILE}"
        log "prs WARNING -- collection exited ${pr_rc}; scan proceeds without PR state"
    else
        log "prs OK -- PR state at ${PR_STATE_FILE} ($(grep -c '^### ' "${PR_STATE_FILE}" 2>/dev/null || echo 0) repos)"
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
    # The model gets prompt + collected PR state as one input file. Transient,
    # but it lives in raw/ with the logs (never /tmp) so a bad scan can be
    # reproduced exactly, and the retention sweep ages it out with everything else.
    scan_input="$(mktemp "${RAW_DIR}/${DATE}-scan-input.XXXXXX")"
    cat "${SCAN_PROMPT}" > "${scan_input}"
    if [ -s "${PR_STATE_FILE}" ]; then
        printf '\n\n---\n\n' >> "${scan_input}"
        cat "${PR_STATE_FILE}" >> "${scan_input}"
    fi

    # One rolling thread: resume the recorded id if we have one, otherwise start
    # fresh and record whatever id codex hands back. A resume that fails (session
    # pruned, id corrupt) falls back to a fresh thread rather than losing the day.
    thread_id=""
    [ -s "${THREAD_FILE}" ] && thread_id="$(tr -d '[:space:]' < "${THREAD_FILE}")"

    scan_rc=1
    if [ -n "${thread_id}" ]; then
        log "stage1 -- resuming thread ${thread_id}"
        run_watchdog "${SCAN_TIMEOUT}" stage1 codex_resume "${scan_input}" "${SCAN_FILE}" "${SCAN_RAW}" "${thread_id}"
        scan_rc=$?
        if [ "${scan_rc}" -ne 0 ]; then
            log "stage1 WARNING -- resume of ${thread_id} failed (exit ${scan_rc}); starting a fresh thread"
            # Truncate NOW, not after the fresh run. The dead id must not survive
            # this line: if the fresh scan then fails to yield a new one, stage 3
            # would otherwise resume the same corpse and "succeed" against it.
            : > "${THREAD_FILE}"
            thread_id=""
        fi
    fi

    if [ -z "${thread_id}" ]; then
        log "stage1 -- starting a fresh codex thread"
        run_watchdog "${SCAN_TIMEOUT}" stage1 codex_fresh "${scan_input}" "${SCAN_FILE}" "${SCAN_RAW}"
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
    review_input="$(mktemp "${RAW_DIR}/${DATE}-review-input.XXXXXX")"
    cat "${REVIEW_PROMPT}" > "${review_input}"
    printf '\n\n---\n\n## SCAN UNDER REVIEW (%s)\n\n' "${DATE}" >> "${review_input}"
    cat "${SCAN_FILE}" >> "${review_input}"

    run_watchdog "${REVIEW_TIMEOUT}" stage2 fable_review "${review_input}" "${REVIEW_FILE}" "${REVIEW_RAW}"
    review_rc=$?

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
    sum_input="$(mktemp "${RAW_DIR}/${DATE}-brief-input.XXXXXX")"
    cat "${SUM_PROMPT}" > "${sum_input}"
    printf '\n\n---\n\n## FABLE REVIEW OF TODAY (%s)\n\n' "${DATE}" >> "${sum_input}"
    cat "${REVIEW_FILE}" >> "${sum_input}"

    run_watchdog "${SUM_TIMEOUT}" stage3 codex_resume "${sum_input}" "${BRIEF_FILE}" "${SUM_RAW}" "${thread_id}"
    sum_rc=$?

    if [ "${sum_rc}" -eq 124 ]; then
        fail_stage stage3 "codex summarize hit the ${SUM_TIMEOUT}s watchdog"
    elif [ "${sum_rc}" -ne 0 ]; then
        fail_stage stage3 "codex summarize exited ${sum_rc} (raw log: ${SUM_RAW})"
    elif [ ! -s "${BRIEF_FILE}" ]; then
        fail_stage stage3 "codex summarize produced no output at ${BRIEF_FILE}"
    else
        # LATEST.md is the only undated artifact in the lane, which makes a
        # STALE one invisible: if stage 3 fails or is skipped, yesterday's brief
        # sits there reading exactly like today's. Stamp the date as the brief's
        # first line before the copy, so every reader -- Anthony by hand, or the
        # boot digest when it lands -- sees which day this is without stat(1).
        brief_headline="$(head -1 "${BRIEF_FILE}")"
        # mktemp, not a fixed per-date name. A shared name is a shared inode: two
        # runs that overlap for even a moment can have one truncating the file
        # the other is `cat`ing INTO -- observed as an 11.3 GB self-feeding
        # stamped tmp. A pid-unique name cannot be aliased no matter how the lock
        # behaves. It lives in raw/ so the retention sweep ages it out if a run
        # dies between the write and the rename. mktemp is 0600, so the mode is
        # restored after the rename -- otherwise 0600 would ride the `mv` into
        # the brief and the `cp` into LATEST.md.
        stamped="$(mktemp "${RAW_DIR}/${DATE}-brief-stamped.XXXXXX" 2>/dev/null)"
        if [ -n "${stamped}" ] && { printf 'Daily brief -- %s\n\n' "${DATE}"; cat "${BRIEF_FILE}"; } > "${stamped}"; then
            mv -f "${stamped}" "${BRIEF_FILE}"
            chmod 644 "${BRIEF_FILE}" 2>/dev/null || true
        else
            [ -n "${stamped}" ] && rm -f "${stamped}"
            log "stage3 WARNING -- could not date-stamp ${BRIEF_FILE}; copying it unstamped"
        fi
        # W9: top-10 hard cap + dead-man lanes section (AUTONOMY-AUDIT-2026-08-18).
        # Deterministic post-processing, no model in the loop. Failure is soft
        # but LOUD: a broken cap degrades to an uncapped brief; a broken
        # dead-man check degrades to an explicit failure marker IN the brief.
        # The section may never be absent, and never silently green.
        DEADMAN_PY="${PROMPT_DIR}/deadman_lanes.py"
        if "${PY}" "${DEADMAN_PY}" cap --brief "${BRIEF_FILE}" --max-items 10 2>>"${LOG}"; then
            log "stage3 -- top-10 cap applied to ${BRIEF_FILE}"
        else
            log "stage3 WARNING -- top-10 cap failed; brief left uncapped"
        fi
        # Capture to a tmp first: a helper that crashes mid-print must not
        # leave half a section in the brief. mktemp in raw/ per the stamped-tmp
        # rationale above; the retention sweep ages out any orphan.
        deadman_out="$(mktemp "${RAW_DIR}/${DATE}-deadman.XXXXXX" 2>/dev/null)"
        if [ -n "${deadman_out}" ] \
            && launchctl list 2>>"${LOG}" \
               | "${PY}" "${DEADMAN_PY}" deadman \
                     --map "${REPO_ROOT}/state/automation-map.json" \
                     --launchctl-list - > "${deadman_out}" 2>>"${LOG}" \
            && [ -s "${deadman_out}" ]; then
            { printf '\n'; cat "${deadman_out}"; } >> "${BRIEF_FILE}"
            log "stage3 -- dead-man lanes section appended"
        else
            printf '\n### LANES THAT DID NOT RUN\n\nDEAD-MAN CHECK FAILED — see daily-scan log.\n' >> "${BRIEF_FILE}"
            log "stage3 WARNING -- dead-man check failed; explicit failure marker appended to the brief"
        fi
        [ -n "${deadman_out}" ] && rm -f "${deadman_out}" 2>/dev/null

        cp "${BRIEF_FILE}" "${LATEST_FILE}"
        log "stage3 OK -- brief at ${BRIEF_FILE}; LATEST.md updated"
        log "headline: ${brief_headline}"
    fi
fi

log "=== run end (date=${DATE}) -- resume the thread with: codex resume ${thread_id:-<none recorded>} ==="
exit 0
