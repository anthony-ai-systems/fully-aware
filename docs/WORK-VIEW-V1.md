# Local work view — work-view/v1

This read-only adapter puts the existing Clayton status projection, Fully Aware boot context and the plans snapshot consumed by IRIS into one local operator view. It answers what the available sources report, how old they are and which execution evidence is missing. It does not dispatch work or change a source record.

The implementation lives in `tools/convergence/work_view.py`. It is Python 3.9+ standard library only, with no service, network, model, subprocess or dependency installation. It extends Fully Aware's data-contract approach without changing `boot-pack/v1`, its assembler, the original aggregate health contract or any installed scheduler. The adapter is manually invoked.

## Existing inputs and ownership

| Optional input | Owner and meaning | Important limit |
| --- | --- | --- |
| Clayton `STATUS.json` | Existing factory status producer on the selected executor host | Partial projection: reports queue/current item and counters, not independent review receipts or verified progress |
| Fully Aware `boot-pack/v1` | Existing boot-pack assembler | Advisory context; fresh generation does not revalidate every underlying fact |
| Estate `plans-snapshot.json` | Generated view of the per-project plan ledgers, also consumed by IRIS | This is the shared plans feed, not all IRIS/Notion state; no accepted commitment is modified |

Inputs are supplied explicitly. The adapter never reads raw conversation archives, credentials, private Imprint stores or factory attempt ledgers. It does not infer an identity join from matching project names. Atlas/Imprint observations remain whatever the existing authorized boot pack carries; this adapter creates no new raw-content connection to either system.

Clayton's current source provides no JSON output flag. Read its already generated `~/status/STATUS.json`; do not invoke the producer with `--json`, because it ignores unknown arguments and can write its ordinary status outputs. No source script execution is required to consume the existing file.

## Usage

From the Fully Aware checkout, supply captured inputs and print the private operator view:

```bash
python3 tools/convergence/work_view.py \
  --clayton-status state/convergence-inputs/STATUS.json \
  --boot-pack state/boot-pack.json \
  --plans-snapshot /Users/anthonyflores/code/state/plans-snapshot.json
```

Add `--format json` for a machine-readable projection. Add `--out-dir state/convergence` to write `WORK-VIEW.md` and `work-view.json` from the same in-memory snapshot. `--now` supplies the timezone-aware evaluation time for replay (the model's `generated_at`/displayed as-of). Actual CLI file-read observations still use wall-clock time, so replaying the same inputs at different read times produces different observation receipts. Pure `build_view` replay is deterministic when both evaluation time and observation metadata are fixed. Missing/unconfigured inputs remain visible; do not substitute invented empty files to make a view green.

The committed `.gitignore` covers `state/`. The adapter enforces that resolved output destinations are inside that directory; it does not run Git or implement the Git ignore engine. Keep that ignore rule in place. Source files cannot be overwritten, and symlink output traversal is rejected. Outputs are private local artifacts, not deliverables to commit or publish. This task installs no collector, timer or client plugin; callers must deliberately refresh inputs. Reading an old copy again does not refresh its producer timestamp.

## Meaning and limits

The output declares `audience: operator-local`, advisory state and no commands. Its snapshot identity identifies this combined observation. It is not a historical transaction across the source systems. Every source keeps its own timestamp, freshness, coverage and input digest. Read time is separate from producer time; neither filesystem mtime nor a new view generation can turn an old source into current evidence.

Freshness thresholds are ten minutes for Clayton's five-minute status producer and thirty-six hours for the daily boot pack and shared plans projection. Missing/invalid/timezone-free timestamps remain unknown; timestamps more than one minute ahead are future, never fresh. These are source-liveness policies, not truth or critical-decision guarantees.

Clayton queue/spend values are explicitly source-reported. The source implementation collapses missing files into some zero counts, so this bridge cannot prove an empty queue or remaining budget. IDLE does not imply healthy, finished or authorized. Started-at and elapsed time do not prove progress. The interface explicitly marks verified progress, review/completion records and artifact/outcome verification unavailable when absent from the source contract.

Repository checkout, recorded installation and profile hashes remain separate. Matching valid evidence can establish internal consistency only; readiness stays unverified until the owner-run installation and engine/phone acceptance have their own evidence. Missing old-format fields stay unknown. Receipt evidence is reported, invalid or unavailable; an absent export field does not establish that a remote installation receipt is absent. Known valid revision mismatches remain visible even when other receipt evidence is missing. A merged PR never becomes an installation receipt.

Plans and boot-pack notices are source-reported projections. A plan labelled complete cannot close a factory item or a delivery commitment. Rendered text is bounded and escaped; arbitrary extra fields, artifact paths and embedded links are not passed through from the factory payload. Truncation is visible. This is an operator-private formatter, not an authenticated service, client access-control boundary, secret classifier or complete prompt-injection defense. Do not publish the output or expose it to another client without a separate content/access contract.

## Verification

```bash
python3 -m unittest discover -s tools/convergence -p 'test_*.py' -v
```

The suite covers deterministic projections, stale/missing/future evidence, legacy status, malformed inputs and duplicate keys, numeric type traps, profile consistency, unknown review/progress, conflicting plan identity, truncation, text escaping and output/source protection. Synthetic cases test the adapter; they do not deploy Clayton or exercise an execution workflow.

The next execution-level contract must be owned with Clayton's trusted runner/reconciler. This adapter supplies no work request, admission grant, checkpoint advancement, cancellation command or review approval. The existing IRIS manager and Clayton cycle/runner continue to own their separate duties.

Each output file is replaced atomically; the pair is not a filesystem transaction. Consumers reading both formats must compare snapshot IDs and retry after a mismatch. The JSON file is the machine interface. A failed write returns a nonzero code and cannot certify a refreshed pair.

Input paths must be physical paths without symlink components, including before `..`; macOS `/tmp` and `/var` aliases should be resolved explicitly by the caller. A captured status file does not establish active-host ownership. Unregistered plan-file counts are projected without copying their paths.
