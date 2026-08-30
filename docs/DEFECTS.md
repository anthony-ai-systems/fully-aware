# Defects

`registers/defects.json` is the single list of known things that are broken. It
records the facts and the check for each problem, not today's result.

Each item has:

- `id`: a short, unique name.
- `severity`: P0 blocks unattended work, P1 means a lane is broken or
  misleading, and P2 is cleanup.
- `system`: the affected system.
- `owner`: who acts next. Exactly one of `anthony`, `session` (a supervised
  session on this Mac), `codex` (the nightly lane), or `jay` (an outside
  collaborator). Anything else is grouped under "Waiting on someone else".
- `fix_scope`: exactly one of `repo-pr` (a pull request from a fresh clone),
  `local` (this Mac or an existing checkout), `decision` (a ruling or a merge),
  or `external` (someone else).
- `size`: `S` (the only size the nightly lane takes) or `M` (needs a session).
- `since`: the first known broken date.
- `symptom`: one sentence describing what is wrong.
- `verify`: a read-only shell check that exits 0 when the problem is fixed.
- `fix_hint`: the next useful repair step.
- `provisional`: whether the current check is only a placeholder.
- `accepted`: an optional sentence saying the item will not be fixed, why, and
  under which ruling.
- `not_before`: an optional date before which the item is left alone.
- `source`: where the item came from.
- `added`: the date the item was written down here.
- `repo`, `remote`, and `pr_check`: optional details for repository repairs.

## The morning loop

`tools/morning-pack.sh` refreshes the surfaces, then runs
`tools/verify-defects.py` before assembling the boot pack. Every check gets 60
seconds and runs from the home folder. Accepted items are skipped along with
placeholders and later-dated ones. Today's results go to
`state/defects-status.json` and the readable list `state/DEFECTS.md`.

A failed check leaves the item open and does not stop the pack. A check that
cannot finish is an error. Placeholders and later-dated items are skipped. If
the register step itself fails, the loop leaves `state/defects-status.FAILED`
behind with the exit code and the time, and the boot digest says so on its first
line instead of quietly showing yesterday's counts as if they were today's.

The defect summary is then section 0 of the full boot pack, the first line of
the boot digest, and the summary line near the top of `state/DEFECTS.md`. When
the status file is older than the pack it was folded into, both places carry its
age, so old counts never read as this morning's.

## Dates and counting fixes

Every date here is a local date, the one on the wall clock, not a UTC date. An
evening run and the next morning's run therefore report the same age for the
same defect, and the defect gate, which works from the local date too, computes
the same number.

"Fixed since yesterday" counts only what the run just learned: an item whose fix
date falls after the day the previous status file was written. On a first run,
or after the status file is lost, the answer is zero. Checks that happen to pass
the first time they ever run are not fixes, and two runs on the same day never
report the same fix twice.

## How a check must behave

A check has to fail when it cannot see what it is looking at. A missing folder,
a checkout that moved, a repository the token can no longer read, a listing that
never loaded: all of those leave the item open. Reporting "fixed" because the
target vanished is the one failure this register cannot tolerate, because
nothing downstream can tell that answer apart from a real repair.

Where a check reads an absence as proof (a deleted file, a removed repository),
it must first prove it can still see the container that absence sits in.

## Accepting something instead of fixing it

Some problems are real, understood, and still not going to be worked: the only
repair would cross a line a ruling has drawn. Writing one sentence in the item's
`accepted` field records that decision, with the ruling date, and the item stops
being work: its check never runs, it never counts as open, it never reaches the
defect gate, and it never appears in today's list. It keeps its place in the
register under its own heading, "Accepted, not being fixed", so the exposure is
still written down; clearing the field puts it straight back in play.

## Private values

Anything that would name a client (a repository, a fork, a URL, a list of pull
requests) stays out of the register: this repo is public, and rule P25 keeps
client names out of a networked repo. Those values live in
`~/.config/fully-aware/defects.env`, one `KEY=VALUE` per line with `DEFECT_`
names, and `tools/verify-defects.py` hands them to every check as environment
variables. A check that needs one must exit 1 when the value is empty, so a
missing file shows the item as still open, never as fixed.

## Adding and closing an item

Add one object to `registers/defects.json`. Write the symptom as one plain
sentence and give a read-only `verify` command that exits 0 only when the
problem is fixed. Use a provisional item only while no real check exists.

When an item's check passes on two consecutive mornings, the next supervised
session removes it from the register. The status keeps the date it first
passed, so the morning summary can report recent fixes.
