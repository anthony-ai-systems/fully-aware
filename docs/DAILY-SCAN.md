# daily-scan -- the Codex-managed daily scan pipeline

One scheduled entry point that produces one artifact: `state/daily-scan/LATEST.md`,
a <=300-word brief opening with a single headline line. Three model stages behind
it -- Codex scans, Fable reviews, Codex summarizes -- with the scan and the
summary sharing **one rolling Codex thread** that Anthony can drop into and
continue any day.

The thread is the design. A fresh scan every morning has no memory, so "what is
new" is guesswork; resuming the same session means the scanner genuinely knows
what it said yesterday. It also means the daily brief is a conversation Anthony
can join rather than a report he can only read.

## Pipeline

```
  06:15 launchd (com.anthony.fully-aware.daily-scan)
    |
    v
  stage 0  FRESHNESS ......... state/BOOT-PACK.md older than 60 min?
    |                          yes -> tools/morning-pack.sh   [watchdog 600s]
    |                          no  -> skip
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
per-stage marker files, never as a launchd failure. Markers are per-date and are
cleared at the start of each run, so a manual re-run is never gated by the
morning's failure.

## File map

| path | what |
| --- | --- |
| `tools/daily-scan/run-daily-scan.sh` | the pipeline. Only executable in the lane. |
| `tools/daily-scan/scan-prompt.md` | stage 1 brief: inputs to read, five strict output sections, read-only rules. |
| `tools/daily-scan/review-prompt.md` | stage 2 brief: kill weak findings, rank survivors, top 3 with WHY, <=400 words. |
| `tools/daily-scan/summarize-prompt.md` | stage 3 brief: headline / TOP 3 / NEW SINCE YESTERDAY / one next action, <=300 words. |
| `tools/daily-scan/install-daily-scan-launchagent.sh` | copies the plist to `~/Library/LaunchAgents` and loads it. **Arming is Anthony's, post-merge.** |
| `launchd/com.anthony.fully-aware.daily-scan.plist` | 06:15 daily schedule. |
| `state/daily-scan/<date>-scan.md` | raw Codex scan. |
| `state/daily-scan/<date>-review.md` | Fable's review, or a one-line `REVIEW SKIPPED: <reason>`. |
| `state/daily-scan/<date>-brief.md` | the day's brief. |
| `state/daily-scan/LATEST.md` | copy of the newest brief -- the boot digest reads the first line. |
| `state/daily-scan/thread-id` | the rolling Codex session id. Delete it to start a new thread. |
| `state/daily-scan/<date>.<stage>.FAILED` | a stage broke; contents are the reason. |
| `state/daily-scan/<date>.<stage>.SKIPPED` | a stage did not run; contents are why. |
| `state/daily-scan/raw/` | per-stage raw CLI logs (the session-id banner, model stderr). |
| `state/logs/daily-scan.log` | the timestamped run log -- start here. |

Everything the pipeline writes is under `state/`, which is gitignored. Codex runs
under a **read-only sandbox** in both stages; the report files are written by the
codex CLI itself (`-o`, the last-message file), never by model-run shell.

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
| stage 3 skipped, `no codex thread id recorded` | `thread-id` empty or unwritten | delete it and re-run; stage 1 will record a fresh one |

Logs: `state/logs/daily-scan.log` (the run log, all stages), plus
`daily-scan.out.log` / `daily-scan.err.log` from launchd itself, plus the raw
per-stage CLI logs under `state/daily-scan/raw/`.

## Running it by hand

```bash
cd /Users/anthonyflores/code/fully-aware

tools/daily-scan/run-daily-scan.sh              # one real run (~5-20 min)

DAILY_SCAN_STUB=1 tools/daily-scan/run-daily-scan.sh   # plumbing only, zero tokens
```

Stub mode swaps both model calls for canned output -- including a fake `session
id:` banner line, since that parse is the fragile part -- and exercises the
dates, thread-id capture and reuse, skip cascade, and `LATEST.md` copy. Use it
after any edit to the script.

Overrides (all optional): `DAILY_SCAN_CODEX_MODEL`, `DAILY_SCAN_CODEX_EFFORT`,
`DAILY_SCAN_FABLE_MODEL`, and the four watchdog budgets
`DAILY_SCAN_{PACK,SCAN,REVIEW,SUM}_TIMEOUT` (seconds).

## Arming

Post-merge only, and it is Anthony's call:

```bash
tools/daily-scan/install-daily-scan-launchagent.sh
launchctl list | grep com.anthony.fully-aware.daily-scan
launchctl start com.anthony.fully-aware.daily-scan   # fire once, now
```

Disarm with `launchctl unload ~/Library/LaunchAgents/com.anthony.fully-aware.daily-scan.plist`.
