# Private planning and focus evidence

`tools/convergence/planning_receipts.py` records evidence for the existing IRIS
sweep. It cannot start a sweep, call a model, grant authority, write Calendar or
change work. Use its reviewed release from the existing sweep, with private
case folders under the current planning-evaluation directory.

Every receipt is atomically published once, mode 0600 inside an owner-only 0700
case folder. A lock serializes writers; the caller supplies the exact last
receipt SHA-256 (`none` for an empty case). Receipt sequence, previous-byte hash,
phase, timestamps, frozen artifact hashes and derived results are revalidated
on every operation. Changed evidence refuses. A regressed clock refuses before publication. Invalid or oversized input is validated before any snapshot is frozen. Run `inspect` after uncertain
publication; do not delete, reset or substitute a new case. A crash before the
receipt may leave the exact frozen snapshot available for retry. A crash after
publication produces a tail conflict on blind retry, not a duplicate receipt.

```
python3 tools/convergence/planning_receipts.py source --case-dir /private/cases/decision-one --input /private/source-input.json --expected-tail none
python3 tools/convergence/planning_receipts.py inspect --case-dir /private/cases/decision-one
```

Each following write uses the returned `tail_sha256`. `baseline` takes an empty
input JSON object. This tool is for a trusted owner, not an untrusted public
endpoint: hashes prove which bytes were recorded, not that a claimed human
answer or source coverage is true. The existing sweep must establish those
claims from actual source receipts. The existing frozen experiment determines
which natural encounters belong in the cohort, including missed cases; the
recorder does not discover encounters or prove cohort completeness.

## Prospective comparison chain

Order: `source` → `baseline` → `proposal` → `label`. A case can instead end in
`missed` before a proposal. It cannot be replaced to improve the sample. The
next three distinct naturally arising encounters specified by the frozen design
remain the intended cohort; existing development cases and the already-approved
Monday block do not become held-out cases merely by being copied here.

`source` input fields:

- `sweep_run_id`, `encounter_id`, aware `encountered_at` and `source_cutoff`.
- `frozen_design` and `source_ref`: each `{ "path": "/absolute/file.json", "sha256": "…" }`.
- `board_current:true`, `development_case:false`, `already_ranked_or_labelled:false`.
- `coverage_limits`: explicit strings, including missing calendars/context.

The exact bounded source and design are copied privately before ranking. Baseline and proposal must be recorded within 15 minutes of the source receipt; an expired window can still be recorded as missed. The
source must provide `items` or `base.items`. Every row keeps its original index.
Unknown/terminal statuses and invalid/absent explicit due dates are excluded;
missing/duplicate identities refuse the snapshot. The status allow-list is in
`ACTIVE`. This is the restricted deadline comparison universe, not every life
priority. Excluded context still needs separate factual/coverage evaluation.

`baseline` directly calls the released sibling
`situation_brief._deadline_baseline`; there is no second ranking algorithm.
It pins all three reader modules, preserves source order for tied UTC instants,
and retains the baseline's date-only UTC-midnight sorting convention. Keep the original reviewed release available for late labels: changing any pinned module makes that comparison case refuse until its exact release is restored. Outcome chains do not depend on these baseline pins.

`proposal` takes `ordered_ids` (one to three unique eligible IDs), one `reasons`
string per ID, `coverage_limits`, and `human_labels_known:false`.

`label` takes `answer_reference`, nullable `human_top_three_ids`, nullable
`usefulness` (`useful` or `not_useful`), nullable nonnegative `correction_burden`
and `human_supervision_minutes`, nullable `hard_failures` (strings), and
`raw_answer_minimal`. Missing values remain null. A genuine answer reference is:

```
{"task_id":"actual UUID","turn_id":"actual turn or null","message_id":null,
 "reference_limit":"State exactly which reference is unavailable.",
 "observed_at":"actual aware timestamp"}
```

At least one turn/message reference is required for any labels. A transport
submission or elapsed timer is never a human answer. A fully unknown label may
have a null reference and null raw answer. A later genuine answer can append a
new label without rewriting the earlier unknown receipt. Never downgrade a
known answer or individual known field to missing data, and never supersede a newer actual answer with an older one. A human top-three empty list may explicitly mean none of those candidates; it is not a missing label. Retractions to unknown require owner reconciliation, not an automatic overwrite. Comparisons expose intersection counts and missing
labels; `automatic_win` is always false. Three complete distinct cases, actual
owner utility labels, no hard failures and better outcomes than the baseline
still require explicit evaluation against the frozen design. No gain is inferred
from successful receipt writes or equal scores.

`missed` takes a concrete `reason`, nullable known `encountered_at`, and
`evidence_reference`. It records ineligibility and no replacement permission.
Do not invent an old encounter timestamp if only coverage uncertainty is known.

## Calendar outcome chain

In a separate case directory, `outcome-source` takes `proposal_ref` and
`calendar_ref` with exact paths/hashes of the existing approved proposal and
verified scheduled receipt. It copies both originals and verifies their matching
proposal/event IDs, approval, start/focus/end and original estimate. It does not
change their historical work binding, create another event or imply completion.

`outcome` is refused until the recorded block end. It takes `outcome`
(`done`, `partial`, `moved`, `skipped`, `unknown`), nullable `focus_minutes`,
`source` (`self_report` or `unknown`), `answer_reference` and `raw_answer_minimal`.
A self-report must have an actual post-block answer reference. Unknown source
requires unknown outcome and null minutes/reference/answer. Accepted timer
integration is not implemented; do not relabel inferred elapsed time as a timer.
Later genuine outcomes append, preserving earlier unknowns. The original
positive estimate never changes. A later outcome cannot drop known minutes or replace a known outcome with unknown; preserve the old evidence and use owner reconciliation for a retraction. Only a done result with positive known focus minutes
is eligible for duration calibration; partial work does not teach full-task time.
The existing schedule helper remains responsible for aggregate calibration.

## Verification and limits

```
python3 -m unittest discover -s tools/convergence -p test_planning_receipts.py -v
```

Tests use synthetic private cases, including publication crashes, CAS conflict,
tamper/module drift, source-order ties, unsafe inputs, unknown human labels and
premature focus outcomes. They are engineering checks, not real held-out cases
or proof of improved scheduling. This local owner-controlled chain cannot defend
against a malicious owner rewriting the entire filesystem, prove human identity,
or independently guarantee truthful source observations. Keep the reviewed source
pins, real answer references and existing canonical work/docket authorities.
