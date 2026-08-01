# Fully Aware

Fully Aware is a personal orchestration data layer over all of Anthony's
environments. It is a standing, sessionless orchestration substrate: every
environment exposes a uniform, provenance-tagged state surface, a unified
next-session view normalizes divergent handoff schemas, and a boot-pack folds
those signals into one starting context for the seat. Three artifacts make it
real: the **next-session parser** (`next-session/v2`), the **surface generator**
(`surface/v1`), and the **boot-pack assembler** (`boot-pack/v1`, lane M4).

## Standing rule — coupling by data contract only

Shareable / productizable repos (saga-protocol, saga-mission-control,
marketing-os) **never import code from this repo**. Coupling is by data contract
only: the published schemas `surface/v1`, `next-session/v2`, and `boot-pack/v1`.
This keeps the shareable repos clean for sharing and productization while Fully
Aware keeps its own working folder.

## Provenance

Relocated 2026-07-23 from `saga-mission-control` PRs #564 / #565, branch tips
`ef0c533` (M1 next-session parser) / `745b2c1` (M2 surface generator).

## Spec

Binding design spec:
`anthony-wiki-vault/build-plans/macro-seat-spec-2026-07-23.md`.

## Layout

- `tools/` — `next_session.py` (parser CLI), `generate-surface.py` (surface
  generator CLI), `assemble-boot-pack.py` (boot-pack assembler CLI),
  `morning-pack.sh` (the scheduled entry point: regenerate every surface, then
  assemble the pack), `configs/` (central per-environment surface configs, the
  hand-maintained `seed-manifest.json` + `ratification-backlog.json`),
  `install-launchagent.sh`, and the test suites (`test_next_session.py`,
  `test_generate_surface.py`, `test_assemble_boot_pack.py`,
  `test_m5_surface_coverage.py`).
- `docs/` — `NEXT-SESSION-V2.md` (`next-session/v2` schema),
  `SURFACE-V1.md` (`surface/v1` schema), `BOOT-PACK-V1.md` (`boot-pack/v1`).
- `launchd/` — `com.anthonyflores.fully-aware.boot-pack.plist` (daily 05:45
  boot-pack refresh; installed and armed — re-run
  `tools/install-launchagent.sh` only after changing the plist).
- `state/` — local-only, gitignored session state (holds `BOOT-PACK.md` +
  `boot-pack.json`, the surfaces cache, and logs).
