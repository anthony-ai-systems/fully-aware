# Read-only planning evaluation

`tools/convergence/planning_evaluation.py` evaluates one explicit request for
the frozen three-case planning experiment. It reads the existing private
receipt chains and the frozen design, then emits a bounded JSON report. It does
not create a registry, discover encounters, call a model, run a scheduler,
write a receipt, change Calendar or alter a work item.

The input schema is:

```json
{
  "schema": "planning-evaluation-input/v1",
  "design_ref": {"path": "/absolute/design.json", "sha256": "…"},
  "case_dirs": ["/absolute/case-one", null, "/absolute/case-three"]
}
```

There must be exactly three slots. A `null` slot means that no case was
encountered or recorded. An existing empty private folder also remains
`unknown`; the evaluator never creates its lock file. A `missed` receipt stays
`missed`; a missed-only chain explicitly has unverified design binding and cannot qualify. The design schema and freeze time are checked. The evaluator refuses duplicate resolved case paths, duplicate
encounter IDs, duplicate frozen source snapshot hashes, a changed receipt or
artifact, a source chain bound to another design, a wrong chain kind, malformed
JSON, non-finite values, and oversized input.

Run it with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tools/convergence/planning_evaluation.py \
  --input /absolute/planning-evaluation-input.json
```

The API is `evaluate(payload)`. Successful reports use
`planning-evaluation-report/v1` and contain three ordinal slots, their receipt
tails and evidence hashes, counts for `expected3`, `recorded`, `missed`,
`unlabelled`, `complete`, and `unknown`, plus aggregate baseline/proposal
agreement. Agreement uses set intersection with the latest label's human
top-three list; an empty human list is a genuine zero agreement label. Missing
labels and missing fields remain unknown. Correction burden and human
supervision are summarized only over known values, with a known-value count and
no zero imputation.

`eligible_for_owner_review` is true only when all three slots have complete,
useful labels, all three have paired agreement, no slot is missed or unknown,
every hard-failure field is known and empty, and proposal agreement is strictly
greater than baseline agreement. Exact ties do not qualify. The report always
sets `automatic_win`, `rollout_authorized`, `cohort_completeness_verified`, and
`real_model_superiority_claim` to `false`; the owner must still verify that the
three cases are the natural cohort and that the human evidence is genuine.

The evaluator reuses `planning_receipts.read`, `parse`, `sha`, and `load`,
including the sibling release pins. It never calls the recorder's writer-lock
helper. Its tests construct private
synthetic chains through the existing receipt API and cover empty, positive,
negative, tied, partial, missed, hard-failure, late-label, duplicate,
wrong-design, wrong-chain, tampered, no-write, and CLI cases.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s tools/convergence -p test_planning_evaluation.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s tools/convergence -v
```

This report is an owner-review aid. It cannot prove natural-cohort
completeness, human identity, truthful source coverage, or real-model
superiority by itself.
