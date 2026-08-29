# The defect gate

Written rules only advise. This hook enforces one of them: while a serious
defect sits unfixed, no session starts new background work.

## What it blocks

When a P0 defect has been open seven days or more, the gate stops a session
from starting new background helper sessions: the Task, Agent and Workflow
tools only. Read, edit and run are untouched, so a blocked session can still
fix things by hand.

Only a genuinely open P0 counts: an item whose verify errored, whose verify is
a placeholder, or that is deferred to a later date does not block, and neither
does anything at P1 or P2. Why: a P0 means unattended work is already unsafe,
and spawning more overnight lanes on top of one multiplies the damage. Seven
days is where the register stops being a to-do list and starts being a lie.

## Clearing the block by actually fixing it

The gate reads the morning job's status snapshot, not the register, so repairing
the defect does not lift the block on its own. Rebuild the snapshot and the gate
reopens on the next call:

    /usr/bin/python3 ~/code/fully-aware/tools/verify-defects.py

That is one command, it runs every check in the register, and it rewrites
`state/defects-status.json`, which is the only file this hook looks at. The
block message prints the same line, so nobody has to remember it.

## Declaring a fix session

If this is the session that will fix it, say so:

    mkdir -p ~/.claude/defect-fix-mode && touch ~/.claude/defect-fix-mode/<session_id>

The block message prints that line with your real session id in it. The marker
lasts twelve hours, then the gate closes again. A marker named `ALL` opens the
gate for every session on this Mac, same twelve hours.

If the tool call arrives with no session id at all, there is no per-session
marker to create, so the block message prints the `ALL` line instead. It never
prints a placeholder you cannot type.

## Turning it off, and failing open

`DEFECT_GATE_DISABLE=1` is the emergency off; `--remove --apply` on the
installer takes it out properly. The hook runs from `~/.claude/hooks/`, so it
reads one fixed file and no worktree copy:
`/Users/anthonyflores/code/fully-aware/state/defects-status.json`. The gate
also fails open on its own: if that file is missing, unreadable, or more than
36 hours old, it passes silently, as does any unexpected error inside the
hook. A nightly job that did not run must never jam a session shut.

## Installing it

`bash tools/install-defect-gate.sh` is a dry run that prints what it would
change. `--apply` copies the hook to `~/.claude/hooks/`, overwriting any
earlier copy, and adds one PreToolUse entry to each settings file, backing up
each settings file before it changes it; run twice, it changes nothing the
second time. Arming is Anthony's; nothing here runs it.
