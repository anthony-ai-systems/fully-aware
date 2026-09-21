# Dependency and capacity planning

These local readers turn explicitly reviewed work, prerequisites and capacity
assumptions into comparable plans. They do not maintain a second task database,
write Calendar, change canonical priorities or authorize execution. The existing
IRIS pulse owns collection, interpretation and presentation; Fully Aware supplies
these bounded calculations and Clayton supplies separately authorized execution.

## Operating flow

1. From one retained source snapshot, IRIS prepares a private
   `planning-scenarios-input/v1` JSON input. Preserve exact canonical work IDs and
   revisions where available. Use `planning_item` for explicitly unbound planning
   items; never infer a canonical join from a title. Record partial coverage.
2. State the horizon, original estimate ranges and reasons, reviewed dependencies,
   and alternative explicit priority orders/capacity assumptions. Unknown duration
   or available time is `null`, not zero. An assumed decision resolution is only a
   hypothetical scenario. Preserve every open task in each order.
3. Run `planning_scenarios.py analyze --input INPUT`. Present required work that
   cannot fit, prerequisites, estimate-sensitive choices and source limits before
   proposing a block. A total minute budget does not establish a free calendar
   window or prove a clock deadline; use the existing calendar pilot to check both.
4. If a commitment, dependency or source changes, run
   `planning_scenarios.py changes --previous OLD --input NEW`. Recheck affected
   downstream preparation through existing owners. This report does not cancel,
   restart or rewrite a job. Any unequal input invalidates reuse of the prior plan.
5. Once genuine focus outcomes exist, run `planning_calibration.py --input INPUT`
   with the same original context and explicitly classified receipt directories.
   Compare its proposed `candidate_context` using the scenario reader. Keep the
   original scenario as a baseline; no estimate is adopted automatically.
6. Freeze a proposal using the existing planning receipt recorder before requesting
   its human label. Use the separate aggregate evaluator to compare the fixed
   experiment. Development examples, source tests and descriptive timing ratios
   cannot establish a held-out win.

Run commands from a reviewed Fully Aware release:

```sh
python3 tools/convergence/planning_scenarios.py analyze --input /absolute/private/context.json
python3 tools/convergence/planning_scenarios.py changes --previous /absolute/private/old.json --input /absolute/private/context.json
python3 tools/convergence/planning_calibration.py --input /absolute/private/calibration.json
```

All three write JSON to stdout only. The CLI refuses malformed, oversized,
symlinked or unreadable input through the existing bounded receipt reader. Treat
input/output as private owner context: it can contain titles and decision reasons.
Do not publish it in a repository. These are trusted local tools, not public APIs;
source hashes and revisions bind supplied evidence but do not prove its truth.

## Scenario contract

`planning_scenarios.validate` is the strict contract; unknown fields are rejected.
Required top-level fields are `schema`, `source`, `horizon`, `items`, `dependencies`
and `scenarios`. Limits are 100 items, 400 dependencies and eight alternatives.
The maximum horizon is 42 days and the source freshness window is at most one day.
A stale or future observation produces no allocation. Re-read source before use.

- `source`: `snapshot_sha256`, timezone-aware `observed_at` / `valid_until`,
  `coverage` (`complete` / `partial`) and `limitations`. Partial coverage needs a
  nonempty limitation. The hash identifies the retained underlying snapshot.
- `horizon`: timezone-aware `start` and `end`. It defines the period under discussion,
  not calendar availability. Deadlines before and inside it remain visible.
- Each item: `id`, `identity_kind` (`canonical_work` / `planning_item`), `revision`,
  `title`, `kind` (`task` / `decision` / `external`), `state`
  (`open` / `done` / `cancelled` / `unknown`), `task_type`, `estimate`, `required`,
  `reason`, `due_at`. Canonical IDs must use the existing `work-` plus 24 hex format.
  Revisions are SHA-256 of the corresponding retained source record, not fabricated
  version numbers. Required is an explicit owner constraint, not model certainty.
- `estimate`: null or `{low, high, basis, kind, calibration_version}` in minutes.
  Initial ranges have `kind: initial` and a null calibration version. A calibrated
  proposal carries its evidence version. Only task estimates consume capacity.
- Each dependency: `prerequisite`, `dependent`, both corresponding `_revision`
  fields, `certainty` (`confirmed` / `uncertain`), and `evidence`. Missing, changed,
  uncertain or cyclic prerequisites block affected work. Cancelled is not done.
  Confirmed dependencies must reflect actual source evidence, not an invented order.
- Each scenario: `id`, `label`, `capacity_minutes`, `capacity_basis`, `order`,
  `assume_done`, `unavailable`. Order contains every open task exactly once.
  Required tasks and their ancestors precede discretionary tasks; prerequisites
  precede dependents. If required work cannot fit, that failure stays explicit.
  Other tasks can still be shown as feasible; this is not permission to skip the
  requirement. An unavailable item is excluded from that scenario, not cancelled.

Low/high runs expose the consequences of estimate ranges. They are deterministic
illustrations, not a globally optimal plan, probabilistic forecast or daily clock
schedule. A task selected by one range and not the other is marked sensitive.
Decision impact lists dependent open/unknown items without claiming a universal
importance score or inferring personal preferences.

## Outcome-based estimate proposals

Calibration input is exactly `{schema, context, episodes}` with schema
`planning-calibration-input/v1`. Context is the original scenario input. Each
explicit episode is `{case_dir, task_type, unit_definition}`. Classify only
comparable focus units under the same type and definition; never train a whole
project estimate from the duration of one editing segment. The owner also ensures
that target tasks of that type use that same unit. This semantic check cannot be
established by receipt hashes. Episode directories must be absolute physical
paths to validated `outcome-source` chains from the existing recorder.

The reader verifies every original proposal/event and later response. Duplicate
case paths, proposal IDs or calendar event IDs are refused. Receipt records later
than the planning source observation are rejected to prevent future-data leakage.
Only completed units with known positive focus minutes contribute a ratio of
actual duration to the original estimate. Partial, unknown, moved and skipped
units do not become zero-duration training samples.

At least five comparable completed units are required to propose a change. The
original low/high bounds are scaled by empirical minimum/maximum ratios; the
median supplies a descriptive typical value. These are not confidence intervals.
All episode tails and the declared unit definition bind the calibration version.
Already calibrated input is refused, preventing repeated compounding. Canonical
identity and source revision remain unchanged because only a derived planning
assumption changed. Excessive ranges are flagged for review rather than clamped.

Five observed units with at least three moved/skipped outcomes also produce a
proposal to review the window, unit size and prerequisites. No schedule, workflow
or live estimate is changed. Both kinds of learning still need prospective human
review and held-out evidence before any claim of better planning.
