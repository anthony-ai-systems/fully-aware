# CLAUDE.md — Fully Aware session anchor

This repo is the working folder for Fully Aware sessions (folder = activity). A
fresh Fable macro session boots here regardless of the task's true cwd.

## Boot protocol

Boot loads `state/BOOT-PACK.md` (with its `boot-pack.json` sidecar) as the single
primary artifact and answers the cold-load bar from the pack alone. The pack is
generated daily at 05:45 local by `tools/morning-pack.sh`, run by the installed
and armed LaunchAgent (`launchd/com.anthonyflores.fully-aware.boot-pack.plist`,
lane M4 cadence ruling §6.5). A pack whose `Generated:` line is not from today
means the agent did not run — re-run the wrapper by hand rather than boot from a
stale pack.

## Rules

- **Tools are read-only, D30-class.** `tools/next_session.py` and
  `tools/generate-surface.py` are standalone, stateless, non-enforcing,
  report-only, manually invoked. They never fetch, mutate, commit, push, or
  merge anything in any repo.
- **Merge is Anthony's.** Nothing here merges, pushes, or auto-ratifies. Refresh
  automation proposes PRs at most.
- **`state/` is local-only** and gitignored (holds the boot pack, surfaces cache,
  and other session scratch). Never commit it.
- **Coupling by data contract only.** Shareable repos never import from this
  repo; the published schemas `surface/v1` and `next-session/v2` are the only
  interface.
