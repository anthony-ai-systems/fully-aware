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
- `not_before`: an optional date before which the item is left alone.
- `source`: where the item came from.
- `added`: the date the item was written down here.
- `repo`, `remote`, and `pr_check`: optional details for repository repairs.

## The morning loop

`tools/morning-pack.sh` refreshes the surfaces, then runs
`tools/verify-defects.py` before assembling the boot pack. Every check gets 60
seconds and runs from the home folder. Today's results go to
`state/defects-status.json` and the readable list `state/DEFECTS.md`.

A failed check leaves the item open and does not stop the pack. A check that
cannot finish is an error. Placeholders and later-dated items are skipped.

The defect summary is then section 0 of the full boot pack, the first line of
the boot digest, and the summary line near the top of `state/DEFECTS.md`.

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
