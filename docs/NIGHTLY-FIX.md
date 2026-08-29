# The nightly fix lane

## What it does each night

At 02:30 it reads `state/defects-status.json`, picks the oldest defect it may
fix, and clones that repo fresh under `~/code/.nightly-fix/`. Codex gets 40
minutes for the smallest correct fix. If the defect records a pull-request
check, the lane runs it in the clone and stops unless it exits 0. Before
committing it reads every changed and untracked path and refuses the lot if one
is named like a credential, is over a megabyte, or lands outside the clone. It
then commits, pushes one branch, and opens one pull request.

## What Codex is allowed to reach

Codex's shell is sandboxed to the clone, and that flag covers shell commands
only. So for that one run the lane also switches off every tool the global
config names: each MCP server by name, the whole plugin system (which is what
removes the mail, chat, notes, drive, GitHub and computer-history connectors),
and the app, browser, computer-use, code-mode, memory and chronicle features.
It proves that before spending anything by listing Codex's servers with the
same settings and refusing to start unless every one says disabled. It never
edits the global config. Reads are the gap: a write-sandbox does not stop Codex
reading this Mac, which is why the rail above is on what gets committed. The
item's own check runs inside a second sandbox with no network at all.

## What it never does

The code refuses these: editing an existing checkout, pushing to `main` or
`master`, force-pushing, rewriting history, merging, and fast-forwards or syncs
of any checkout. Merge is always Anthony's. It also refuses a recorded check
that names a path outside the clone, uses `..`, pushes, calls `gh`, or reaches
the Mac (`sudo`, `launchctl`); that item waits.

## How an item becomes eligible

Status `open`, owner `codex`, fix scope `repo-pr`, size `S`, a GitHub remote,
no provisional flag, severity below P0 (a P0 is never fixed unattended).
Skipped: deferred items, items tried in the last 72 hours, and items whose last
pull request is still waiting. Oldest `open_since` wins. The lane stands down
entirely if the status file is over 36 hours old. Record a `pr_check`.

## How to arm it after merge

Not armed by this branch. After it merges, run `tools/install-nightly-fix.sh`
with no argument for a dry run, `--apply` to arm the 02:30 job, `--remove` to
disarm it. Run `/usr/bin/python3 tools/nightly-fix.py --trial` first; it
pushes nothing.

## How to read the morning log

Start with `state/nightly-fix/YYYYMMDD-<id>.md`: the selection, the outcome,
and the pull-request URL or why there is none. `.prompt.md`, `.codex.log` and
`.last.md` hold the prompt, command output and Codex's last message. The
launchd exit code is the fast signal: 0 worked or stood down on purpose, 2 is a
config or safety refusal, 3 and up mean it tried and could not finish, one
number per reason (see `OUTCOME_EXIT_CODES`).
