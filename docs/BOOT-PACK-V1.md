# `boot-pack/v1` -- macro boot-pack assembler

Macro Seat spec v1, Artifact 3 (`build-plans/macro-seat-spec-2026-07-23.md`
SS3.1-3.2). One shared, committed, D30-class assembler
(`assemble-boot-pack.py`) folds four sections into `state/BOOT-PACK.md` (human
render) plus `state/boot-pack.json` (machine sidecar) -- the single primary
artifact a fresh Fully Aware macro session loads regardless of cwd.

The assembler is committed and shared; the emitted pack is **local-only and
gitignored** (`state/`). Never hand-edit a pack -- re-run the assembler.

## What it is (and is not)

The pack is **ADVISORY STATE, NOT LAW.** SAGA doctrine, repo CLAUDE.md, and
merge-is-Anthony's bind regardless of pack content. The pack *routes* attention;
it never absorbs, performs, or clears ratification, and nothing it does merges,
pushes, or ratifies.

`assemble-boot-pack.py` is **D30-class**: standalone, read-only inputs, stateless,
non-enforcing, report-only, manually invoked. It performs **no git operations and
no network**. The only files it writes are its own `state/` outputs, and it
refuses to write a non-gitignored path.

## The four sections

Every entry is tagged `[source | as_of]`.

1. **Topology manifest** -- from the hand-maintained seed
   `tools/configs/seed-manifest.json` (tagged `provenance: manual`) until P21's
   `repo-manifest.json` ships. Canonical repos only; worktree copies excluded.
   Each entry: environment, path, role, status, kind, owning system, branch,
   pins. Staleness threshold **7d**.
2. **State surfaces** -- the fold of every discoverable `<repo>/.macro/surface.json`
   (schema `surface/v1`, produced by `generate-surface.py`). Lookup order:
   `<repo_path>/.macro/surface.json`, then `state/surfaces/<environment>.json`
   (a Fully Aware-local cache so the assembler folds surfaces without writing
   into other repos). A missing/invalid surface for a manifest repo is a
   **degraded-source WARNING**, never a crash. Staleness threshold **24h**
   (on the surface's `generated_at`).
3. **Unified decision queue** -- a **PROJECTION** (routes, never absorbs
   ratification) over three feeds, rendered as one ordered inbox (oldest waiting
   first) with per-item age + provenance:
   - `decisions[]` from each surface;
   - `human_only[]` via the M1 `next_session.py` parser (each manifest repo's
     root `NEXT_SESSION.json`, normalized to `next-session/v2`);
   - a standing ratification backlog from the hand-maintained
     `tools/configs/ratification-backlog.json` (`provenance: manual`, Anthony's
     to maintain).
   Staleness threshold **1h**.
   **OPEN-ITEM (COS v2):** SS3.1 specifies this queue as a projection over the
   COS v2 event contract, with mission-control as a second head. No concrete
   COS v2 event *source* exists on disk yet, so the assembler projects over the
   three feeds above and records the COS v2 wiring as an explicit `OPEN-ITEM` in
   the pack. It does **not** invent a COS v2 reader.
4. **Scan / priorities feed** -- consumes the `scan-consumption-interface-v1`
   artifacts (`weights.json`, `scan-targets.json`, `suppression.json`, optional
   `intentions.json`) from a configurable `--scan-consumption-dir`. Each artifact
   is validated **independently** (one bad artifact never kills the cycle):
   unknown keys ignored, unknown schema majors rejected + bannered, full producer
   provenance surfaced. If the dir/artifacts are absent, the section renders an
   explicit `no scan artifacts found at <path>` line -- absence is a state, not
   an error. (saga-protocol PR #150 ratified the interface; this is real
   consumption, not the spec's pre-ratification `UNRATIFIED -- omitted` stub.)

## Staleness

Stale entries render a `STALE(<age>)` prefix (e.g. `STALE(2d)`, `STALE(5h)`) --
**never dropped, never silently trusted.** An unparseable `as_of` renders
`STALE(?)` (fail-visible). Thresholds: topology 7d, surfaces 24h, decisions 1h.

## Degraded sources

Every degraded source (missing/invalid surface, invalid/rejected/absent-required
scan artifact, broken decision feed) produces a line in the **WARNING block at
the top of the pack** -- never a silent omission.

## Hard cap + truncation

Hard cap **50000 tokens** (chars/4 estimate). On overflow the assembler truncates
the **lowest-priority tier first -- per-repo `next_lanes` in section 2**, dropping
from the lowest-priority repos (latest in manifest order) -- and marks each
affected repo `next_lanes: TRUNCATED: n entries`. Never a silent cap. If shedding
the entire truncation tier still cannot reach the cap (the base pack exceeds it),
the pack renders over-cap but fully marked.

## Determinism

Deterministic ordering everywhere: manifest order for topology + surfaces, a
stable sort for the decision queue (oldest `as_of` first, tie-break source then
summary), fixed artifact order for scan. **Wall-clock appears ONLY inside `as_of`
fields** (the `Generated:` line, surface/scan `generated_at`), which are carved
before any determinism/diff comparison. `build_pack(now, ...)` takes an
injectable `now`; against fixed inputs two runs are byte-identical, and across
different reference times they are identical after carving `as_of`/`generated_at`.
`STALE(<age>)` markers are a deterministic function of `(now, as_of)`.

## CLI

```
python3 assemble-boot-pack.py                        # -> state/BOOT-PACK.md + state/boot-pack.json
python3 assemble-boot-pack.py --stdout               # markdown to stdout, no file writes
python3 assemble-boot-pack.py \
    --scan-consumption-dir <dir> \                   # consume scan artifacts
    --surfaces-cache-dir state/surfaces \            # surface fallback cache
    --manifest tools/configs/seed-manifest.json \
    --ratification-backlog tools/configs/ratification-backlog.json
```

On-demand run is the default entry point (macro-session boot).

## Morning freshness wrapper (`tools/morning-pack.sh`)

The assembler is strictly read-only and never regenerates surfaces -- but
surfaces go **STALE at 24h**. A scheduled assembler alone would therefore render
permanently-stale surfaces. `tools/morning-pack.sh` is the orchestration layer:
it FIRST regenerates every `surface-config/v1` config under `tools/configs/`
(via `generate-surface.py`, writing into the Fully Aware-local
`state/surfaces/<environment>.json` cache -- **never into any other repo**), then
runs `assemble-boot-pack.py` over the fresh surfaces. Non-repo configs
(`seed-manifest.json`, `ratification-backlog.json`) are skipped by schema.
Per-repo generation **failure degrades** (logged, skipped) -- it never aborts the
pack; a merely *degraded* surface still writes with inline markers the assembler
renders as WARNINGs. Any args to the wrapper are forwarded to the assembler.

```
tools/morning-pack.sh                        # regenerate all surfaces, then assemble
tools/morning-pack.sh --scan-consumption-dir <dir>   # args pass through to the assembler
```

A LaunchAgent (`launchd/com.anthonyflores.fully-aware.boot-pack.plist`, daily
05:45 before the 6am brief) runs the **wrapper** (M5; previously the assembler
alone) and is authored but **unarmed** -- arm it post-merge with
`tools/install-launchagent.sh`. The currently-armed M4 agent stays live until the
install script is rerun post-merge.

## Hand-maintained seeds

- `tools/configs/seed-manifest.json` (`provenance: manual`) -- topology until P21.
- `tools/configs/ratification-backlog.json` (`provenance: manual`) -- Anthony's
  standing ratification backlog; seeded with a placeholder + a README note. The
  assembler only reads it and projects its items into the queue.

## Tests

```
python3 -m unittest test_assemble_boot_pack -v
```

Stdlib `unittest`, no third-party deps. All fixtures synthetic. Covers: section
assembly + order, `[source | as_of]` tagging, staleness at each threshold,
degraded-source WARNING, hard-cap truncation with explicit markers, scan-artifact
independent validation (one bad artifact does not kill the cycle), the
`human_only` projection, and determinism (byte-identical + carve-stable).
