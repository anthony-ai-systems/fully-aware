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

- **Tools are read-only, D30-class.** `tools/next_session.py`,
  `tools/generate-surface.py`, `tools/assemble-boot-pack.py`, and
  `tools/boot-digest.py` are standalone, stateless, non-enforcing, report-only.
  They never run `git fetch`, mutate, commit, push, or merge anything in any
  repo; their only writes land in gitignored `state/`. Network access is
  limited to read-only `gh` queries (open-PR counts, cold-load probes) —
  behind-origin counts therefore reflect the last out-of-band fetch, not live
  origin state.
- **Two LaunchAgents are armed** — the tools above are scheduled, not only
  manual. `boot-pack` (05:45 daily, lane M4 §6.5) runs `tools/morning-pack.sh`:
  regenerates every surface into `state/surfaces/`, runs the imprint CLI bulk
  export to `state/imprint-store.md` (audit-sanctioned content channel,
  2026-08-07), assembles the pack, writes the digest. `daily-scan` (06:15
  daily) runs the Codex/Fable scan pipeline under `tools/daily-scan/`, which
  spends model tokens and writes `state/daily-scan/`. Both write only
  gitignored `state/` (plus the codex CLI's own `~/.codex` rollouts); neither
  commits, pushes, or merges anything.
- **Merge is Anthony's.** Nothing here merges, pushes, or auto-ratifies. Refresh
  automation proposes PRs at most.
- **`state/` is local-only** and gitignored (holds the boot pack, surfaces cache,
  and other session scratch). Never commit it.
- **Coupling by data contract only.** Shareable repos never import from this
  repo; the published schemas `surface/v1` and `next-session/v2` are the only
  interface.
