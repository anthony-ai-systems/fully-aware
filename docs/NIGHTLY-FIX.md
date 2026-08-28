# The nightly fix lane

## What it does each night

At 02:30 it reads `state/defects-status.json`, picks the oldest defect it may
fix, and clones that repo fresh under `~/code/.nightly-fix/`. Codex gets 40
minutes for the smallest correct fix. If the defect records a pull-request
check, the lane runs it in the clone and stops unless it exits 0. It then
commits, pushes one branch, and opens one pull request. No change, or a failed
check, is normal and not a crash: the lane records what happened, leaves the
defect open, and keeps the clone.

## What it never does

The code refuses these: editing an existing checkout, pushing to `main` or
`master`, force-pushing, rewriting history, merging, and fast-forwards or syncs
of any checkout. Merge is always Anthony's, and so is the ruling on a sync. It
also refuses a recorded check that names a path outside the clone, uses `..`,
pushes, calls `gh`, or reaches the Mac (`sudo`, `launchctl`); that item waits.
Codex is told to add no new dependencies, but that is advice, not a rail: the
check runs outside its sandbox, with network.

## How an item becomes eligible

The morning check must have found it still broken (status `open`), with owner
`codex`, fix scope `repo-pr`, size `S`, a GitHub remote, and no provisional
flag. Deferred items, and items tried in the last 72 hours, are skipped; the
oldest `open_since` wins. A pull-request check is not required: an item without
one still gets a pull request, guarded only by the repo's own tests. Record a
`pr_check` on anything this lane may take.

## How to arm it after merge

The LaunchAgent is not armed by this branch. After the branch merges, run:

    bash tools/install-nightly-fix.sh             # dry run; changes nothing
    bash tools/install-nightly-fix.sh --apply     # arm the 02:30 job
    bash tools/install-nightly-fix.sh --remove    # disarm it

Run `/usr/bin/python3 tools/nightly-fix.py --trial` first; it pushes nothing.

## How to read the morning log

Start with `state/nightly-fix/YYYYMMDD-<id>.md`, for example
`20260828-MAP-1.md` (no dashes in the date). It gives the selection, the
outcome, and the pull-request URL or why there is none. `.prompt.md`,
`.codex.log`, and `.last.md` hold the prompt, command output, and Codex's last
message. `attempts.json` records the 72-hour backoff; launchd saves what the
job printed in `state/logs/nightly-fix.out.log` and
`state/logs/nightly-fix.err.log`.
