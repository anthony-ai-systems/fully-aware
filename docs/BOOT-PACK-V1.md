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

## Consumer contract -- `boot-pack/v1` has a LIVE external consumer

`boot-pack/v1` is **not** an internal-only shape. The Iris console server
(`_awareness_digest`, in the vault's Iris runtime checkout) reads the JSON
sidecar directly, from the **hardcoded absolute default path**
`/Users/anthonyflores/code/fully-aware/state/boot-pack.json`, and **hard-validates**
it: `schema == "boot-pack/v1"`, `generated_at`, `token_estimate`, `warnings[]`
and `open_items[]` as arrays, and `sections.decision_queue.items[]` as an array.
Anything else raises.

Two consequences:

- **Any change to the sidecar schema MUST be reviewed against Iris first** --
  renaming or retyping one of those fields silently breaks its awareness
  context.
- **Breakage is silent.** Iris wraps the whole read in an `except Exception`
  guard (awareness must never abort packet or chat construction), so a missing,
  moved, or schema-drifted pack degrades to `{"available": false, "reason": ...}`
  with only a log line -- never an error the operator sees. Do not rely on Iris
  to fail loudly.

## The four sections

Every entry is tagged `[source | as_of]`.

1. **Topology manifest** -- from the hand-maintained seed
   `tools/configs/seed-manifest.json` (tagged `provenance: manual`) until P21's
   `repo-manifest.json` ships. Canonical repos only; worktree copies excluded.
   Each entry: environment, path, role, status, kind, owning system, branch,
   pins. Staleness threshold **7d**.
2. **State surfaces** -- the fold of every discoverable surface (schema
   `surface/v1`, produced by `generate-surface.py`). In practice every surface
   on this machine lives in the Fully Aware-local cache
   **`state/surfaces/<environment>.json`** -- that is what `morning-pack.sh`
   writes and what the assembler actually reads, and it is how the layer keeps a
   zero footprint in every other repo. The lookup order is
   `<repo_path>/.macro/surface.json` first, then the cache; the `.macro/` path is
   a supported per-repo fallback that no repo here exercises (no `.macro/`
   directory exists on this machine). A missing/invalid surface for a manifest
   repo is a **degraded-source WARNING**, never a crash. Staleness threshold
   **24h** (on the surface's `generated_at`). Each repo's block also renders its
   `next_session` payload as **one** line — `next-session[<status>]: <summary>
   [<parser source> | <as_of>]` — hard-truncated at 200 summary chars, so an
   authored handoff (e.g. a `PARKED pending ruling` directive) reaches the
   reader instead of dead-ending in the surface. A degraded `next_session` probe
   projects nothing; it is already reported in the WARNING block.
3. **Unified decision queue** -- a **PROJECTION** (routes, never absorbs
   ratification) over three feeds, rendered as one ordered inbox (oldest waiting
   first) with per-item age + provenance:
   - `decisions[]` from each surface;
   - `human_only[]` via the M1 `next_session.py` parser (each manifest repo's
     root `NEXT_SESSION.json`, normalized to `next-session/v2`);
   - a standing ratification backlog from the hand-maintained
     `tools/configs/ratification-backlog.json` (`provenance: manual`, Anthony's
     to maintain). A backlog item may carry an optional `"placeholder": true`
     flag (used only on the seed shape); placeholder items are **skipped** --
     they never enter the live inbox. Instead the queue section renders one
     provenance footer line, e.g. `ratification backlog: 0 live items (seed
     placeholder skipped) [tools/configs/ratification-backlog.json | <as_of>]`.
     Real items must **not** carry the flag.
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
   provenance surfaced. Absence is a state, not an error: when **no** dir is
   configured the section renders one clean actionable line (`scan consumption
   dir not configured (pass --scan-consumption-dir; ...) [config | <as_of>]`);
   when a dir **is** configured but holds no artifacts it renders `no scan
   artifacts found at <path>`. (saga-protocol PR #150 ratified the interface; this is real
   consumption, not the spec's pre-ratification `UNRATIFIED -- omitted` stub.)

## Staleness

Stale entries render a `STALE(<age>)` prefix (e.g. `STALE(2d)`, `STALE(5h)`) --
**never dropped, never silently trusted.** Free-text `as_of` values (source
NEXT_SESSION files carry prose like `2026-07-23T07:30 local (approx, overnight
2026-07-23 session)`) have their leading ISO date/datetime **prefix** extracted
for age computation (that example -> `2026-07-23T07:30`). Only when **no** date
is extractable does the entry render `AS_OF-UNPARSEABLE` (fail-visible, distinct
from STALE which means known-but-old); such entries sort **last** in the
oldest-first queue with a stable tie-break. Thresholds: topology 7d, surfaces
24h, decisions 1h.

## Degraded sources

Every degraded source (missing/invalid surface, invalid/rejected/absent-required
scan artifact, broken decision feed) produces a line in the **WARNING block at
the top of the pack** -- never a silent omission. A surface that carries inline
degraded probes names each one and why, e.g. `surface for saga-protocol:
degraded probe next_session (no root-level NEXT_SESSION.json)`, pulling the
probe name (dotted key path) and reason from the surface's own degraded markers.

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
alone) and is **installed and armed** -- the scheduled 05:45 run is the pack's
normal source. `tools/install-launchagent.sh` re-copies + reloads the plist; run
it only after changing the plist, since edits in this repo do not reach the
installed copy on their own.

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
