# daily-scan -- the Codex-managed daily scan pipeline

One scheduled entry point that produces one artifact: `state/daily-scan/LATEST.md`,
a <=300-word brief opening with a single headline line. Three model stages behind
it -- Codex scans, Fable reviews, Codex summarizes -- with the scan and the
summary sharing **one rolling Codex thread** that Anthony can drop into and
continue any day.

The headline line is written to be lifted verbatim into the boot digest. That
consumer is **pending**: nothing reads `LATEST.md` automatically until
`feat/boot-digest` lands. Today the brief is opened by hand.

The thread is the design. A fresh scan every morning has no memory, so "what is
new" is guesswork; resuming the same session means the scanner genuinely knows
what it said yesterday. It also means the daily brief is a conversation Anthony
can join rather than a report he can only read.

## Pipeline

```
  06:15 launchd (com.anthonyflores.fully-aware.daily-scan)
    |
    v
  LOCK ..................... atomic mkdir state/daily-scan/.lock
    |                          held by a live run -> log + exit 0
    |                          older than the total watchdog budget -> broken
    v
  RETENTION ................ prune dated outputs + raw logs older than 30d,
    |                          rotate daily-scan.log at ~1 MB,
    |                          retire thread-id if it is older than 30d
    v
  stage 0  FRESHNESS ......... state/BOOT-PACK.md older than 60 min?
    |                          yes -> tools/morning-pack.sh   [watchdog 600s]
    |                          no  -> skip
    v
  PR STATE ................. gh pr list --limit 10 per manifest repo    [120s]
    |                          run by the RUNNER, not the model -- the codex
    |                          sandbox is offline; appended to the stage 1
    |                          prompt as data
    v
  stage 1  SCAN .............. codex exec           [read-only sandbox, 900s]
    |                          gpt-5.6-sol, prompt = tools/daily-scan/scan-prompt.md
    |                          resumes state/daily-scan/thread-id if present,
    |                          else starts fresh and records the new id
    |                          -> state/daily-scan/<date>-scan.md
    |
    |  (stage 1 failed? stages 2 + 3 are SKIPPED, run still exits 0)
    v
  stage 2  REVIEW ............ claude -p --model claude-fable-5      [600s]
    |                          prompt = review-prompt.md + the scan
    |                          -> state/daily-scan/<date>-review.md
    |                          missing / logged-out / timed out ->
    |                             "REVIEW SKIPPED: <reason>", pipeline continues
    v
  stage 3  SUMMARIZE ......... codex exec resume <thread-id>          [600s]
                               SAME thread, prompt = summarize-prompt.md + review
                               -> state/daily-scan/<date>-brief.md
                               -> copied to state/daily-scan/LATEST.md
```

Every stage has its own watchdog and degrades rather than aborts. The script
**always exits 0**: a half-finished run is reported in the log and in the
per-stage marker files, never as a launchd failure. Markers *and today's
artifacts* are cleared at the start of each run, so a manual re-run is never
gated by the morning's failure and can never leave this morning's dead scan
sitting beside yesterday's brief looking like a matched pair.

Watchdogs kill by **process group**, not by pid: `codex` and `claude` spawn
their own trees, and killing only the direct child leaves grandchildren running
after the deadline. Each stage is launched under job control so its pgid is its
pid, and the watchdog signals `-$pid`.

Two runs never overlap. The lock is an atomic `mkdir` of
`state/daily-scan/.lock`, released by an `EXIT` trap; a second run (launchd
firing while a 15-minute scan is still going, or a hand re-run on top of it)
logs `LOCK held by a live run` and exits 0. A lock older than the sum of every
watchdog budget cannot belong to a live run, so it is broken rather than obeyed.

## File map

| path | what |
| --- | --- |
| `tools/daily-scan/run-daily-scan.sh` | the pipeline. Only executable in the lane. |
| `tools/daily-scan/scan-prompt.md` | stage 1 brief: inputs to read, five strict output sections, read-only rules. The runner appends the collected PR block to it. |
| `tools/daily-scan/review-prompt.md` | stage 2 brief: kill weak findings, rank survivors, top 3 with WHY, <=400 words. |
| `tools/daily-scan/summarize-prompt.md` | stage 3 brief: headline / TOP 3 / NEW SINCE YESTERDAY / one next action, <=300 words. |
| `tools/daily-scan/install-daily-scan-launchagent.sh` | copies the plist to `~/Library/LaunchAgents` and loads it. **Arming is Anthony's, post-merge.** |
| `launchd/com.anthonyflores.fully-aware.daily-scan.plist` | 06:15 daily schedule. |
| `state/daily-scan/<date>-scan.md` | raw Codex scan. |
| `state/daily-scan/<date>-review.md` | Fable's review, or a one-line `REVIEW SKIPPED: <reason>`. |
| `state/daily-scan/<date>-brief.md` | the day's brief. |
| `state/daily-scan/LATEST.md` | copy of the newest brief. **Consumer pending:** the boot digest is meant to surface its first line, but that reader does not exist until `feat/boot-digest` lands. Until then LATEST.md is read by hand. |
| `state/daily-scan/thread-id` | the rolling Codex session id. Delete it to start a new thread. |
| `state/daily-scan/<date>.<stage>.FAILED` | a stage broke; contents are the reason. |
| `state/daily-scan/<date>.<stage>.SKIPPED` | a stage did not run; contents are why. |
| `state/daily-scan/raw/` | per-stage raw CLI logs (the session-id banner, model stderr), the collected PR-state block, and the composed prompt inputs. |
| `state/daily-scan/.lock` | held for the duration of a run; an atomic `mkdir`, removed by an `EXIT` trap. |
| `state/logs/daily-scan.log` | the timestamped run log -- start here. Rotated to `.log.1` at ~1 MB. |

Everything the pipeline itself writes is under `state/`, which is gitignored --
including the transient prompt inputs, which are composed into
`state/daily-scan/raw/` rather than `/tmp` so a bad scan can be reproduced
exactly. Codex runs under a **read-only sandbox** in both model stages; the
report files are written by the codex CLI itself (`-o`, the last-message file),
never by model-run shell.

Two side effects are outside `state/` and worth naming rather than papering
over: stage 0 re-runs `tools/morning-pack.sh` when the boot pack is stale, which
rewrites `state/BOOT-PACK.md` and whatever else that script owns, and the codex
CLI records its own session **rollouts under `~/.codex`** (that is what makes
the thread resumable at all). Neither is optional; neither touches git.

## Retention

| what | rule |
| --- | --- |
| `<date>-scan.md` / `-review.md` / `-brief.md`, `.FAILED` / `.SKIPPED` markers | pruned at run start, older than 30 days (`DAILY_SCAN_RETAIN_DAYS`) |
| `state/daily-scan/raw/*` (CLI logs, PR block, prompt inputs) | same 30-day sweep |
| `state/logs/daily-scan.log` | rotated to `daily-scan.log.1` once it passes ~1 MB (`DAILY_SCAN_LOG_MAX_BYTES`) |
| `LATEST.md`, `thread-id` | never swept -- current state, not history |
| the rolling thread | **rotate monthly.** If `thread-id` has not been touched in 30 days (`DAILY_SCAN_THREAD_MAX_DAYS`) the run retires it and starts fresh. Also rotate by hand -- `: > state/daily-scan/thread-id` -- if a resume raw log shows a context-limit error; an unbounded thread eventually hits the model's context window mid-scan and takes the day with it. |

## Resuming the thread

The whole point. Interactively, from anywhere:

```bash
codex resume "$(cat /Users/anthonyflores/code/fully-aware/state/daily-scan/thread-id)"
```

Codex hides non-interactive sessions from the resume picker, but resuming a
specific id works regardless -- no `--include-non-interactive` needed when you
name the id. To ask a one-shot question without opening the TUI:

```bash
cd /Users/anthonyflores/code/fully-aware
codex exec resume "$(cat state/daily-scan/thread-id)" \
  -m gpt-5.6-sol -c sandbox_mode="read-only" \
  -o /tmp/codex-last.md - <<< 'Expand on the second upgrade candidate.'
```

Both continue the same conversation the scanner has been having since the thread
started; the session id does not change when you resume. Delete `thread-id` to
force a clean thread on the next run (you lose the accumulated day-over-day
memory, so do it deliberately).

`codex exec resume` accepts neither `-C` nor `-s`: the working root comes from the
current directory and the sandbox falls back to `~/.codex/config.toml` (which is
`danger-full-access` on this machine). The pipeline forces both -- cwd via a
subshell, sandbox via `-c sandbox_mode="read-only"`. Any hand-written resume
should do the same.

## Failure modes

| symptom | cause | where to look |
| --- | --- | --- |
| no `<date>-brief.md`, no `LATEST.md` update | stage 1 died, so 2 and 3 skipped | `<date>.stage1.FAILED`, `<date>.stage3.SKIPPED` |
| `REVIEW SKIPPED: claude CLI not on PATH` | `claude` missing under launchd's stripped PATH | the plist runs `/bin/bash -lc`; check the login shell PATH |
| `REVIEW SKIPPED: claude exited 1` | usually not logged in, or quota | `state/daily-scan/raw/<date>-review.raw.log` |
| `stage1 FAILED -- ... watchdog` | scan exceeded 15 min | raise `DAILY_SCAN_SCAN_TIMEOUT`, or tighten `scan-prompt.md` |
| `WARNING no session id in ...` | codex banner changed shape | `raw/<date>-scan.raw.log`; the parse is `sed -n 's/^session id: *//p'` |
| `stage0 FAILED -- morning-pack.sh exit N` | surface generation broke | the scan still runs, against a stale pack; see `state/logs/daily-scan.log` |
| stage 3 skipped, `no codex thread id recorded` | `thread-id` empty or unwritten, or stage 1's resume failed and truncated it | re-run; stage 1 starts a fresh thread and records the new id |
| the run logged `LOCK held by a live run` and did nothing | a previous run is still going | `state/logs/daily-scan.log` for the live run's stage lines; wait it out, or remove `state/daily-scan/.lock` if you are sure nothing is running |
| PR sections all say `UNAVAILABLE` | `gh` missing from the runner's PATH, or not authenticated | `state/daily-scan/raw/<date>-pr-state.md`; try `gh pr list` by hand in one of the manifest repos |
| the scan complains it cannot reach GitHub | the model tried `gh` itself despite the prompt | it cannot -- the sandbox is offline. The runner's block is the only PR source; check it landed in `raw/<date>-scan-input.*` |

Logs: `state/logs/daily-scan.log` (the run log, all stages), plus
`daily-scan.out.log` / `daily-scan.err.log` from launchd itself, plus the raw
per-stage CLI logs under `state/daily-scan/raw/`.

## Running it by hand

```bash
cd /Users/anthonyflores/code/fully-aware

tools/daily-scan/run-daily-scan.sh              # one real run (~5-20 min)

DAILY_SCAN_STUB=1 tools/daily-scan/run-daily-scan.sh   # plumbing only, zero tokens
```

Stub mode swaps every model call for canned output -- including a fake `session
id:` banner line, since that parse is the fragile part -- and also stubs stage 0
and the `gh` collection, so it spends no tokens, makes no network calls, and
does not regenerate the boot pack. It is not, however, side-effect free: it
still writes this date's `-scan` / `-review` / `-brief` files, `LATEST.md`, the
markers, `thread-id`, and the raw logs, because that plumbing (dates, thread-id
capture and reuse, lock, retention, skip cascade, `LATEST.md` copy) is exactly
what it exists to exercise. **Stub over real output and you overwrite the day's
real brief.** Point it at a scratch checkout, or expect to re-run for real. Use
it after any edit to the script.

Overrides (all optional): `DAILY_SCAN_CODEX_MODEL`, `DAILY_SCAN_CODEX_EFFORT`,
`DAILY_SCAN_FABLE_MODEL`, the five watchdog budgets
`DAILY_SCAN_{PACK,PR,SCAN,REVIEW,SUM}_TIMEOUT` (seconds), and the retention
knobs `DAILY_SCAN_RETAIN_DAYS`, `DAILY_SCAN_LOG_MAX_BYTES`,
`DAILY_SCAN_THREAD_MAX_DAYS`.

## Arming

Post-merge only, and it is Anthony's call:

```bash
tools/daily-scan/install-daily-scan-launchagent.sh
launchctl list | grep com.anthonyflores.fully-aware.daily-scan
launchctl start com.anthonyflores.fully-aware.daily-scan   # fire once, now
```

Disarm with `launchctl unload ~/Library/LaunchAgents/com.anthonyflores.fully-aware.daily-scan.plist`.
