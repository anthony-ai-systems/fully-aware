# Existing IRIS sweep: time admission and latest attempt

These tools add executable time checks and immutable attempt evidence to the
existing owner, run directory and heartbeat. They do not add a scheduler, work
writer, provider invocation or approval. All source/review/notification/permission
checks remain independently required. Neither tool grants any action authority.

## Clock

Create the usual unique owner-private (0700) sweep run directory. Invoke once,
using the actual automation trigger timestamp/reference, not the time the model
finally starts. Retain the returned exact clock hash in the existing checkpoint.

```sh
python3 tools/convergence/sweep_clock.py --run-dir ABSOLUTE_EXISTING_RUN init \
  --trigger-at ACTUAL_TRIGGER_UTC --trigger-ref ACTUAL_TRIGGER_ID \
  --owner-task-id EXISTING_IRIS_TASK_ID
```

The clock records the actual host and boot session, initialization UTC and monotonic
sample and original trigger. It is published exclusively, never replaced. A late
initialization keeps all already elapsed trigger time. Reinitialization refuses;
resume `status`/`check` using the original hash. Do not mint another clock/run to
recover expired time. A diagnostic of an old failure belongs outside the old run.

Before each operation, read `status` or run a `check` immediately before invocation:

```sh
python3 tools/convergence/sweep_clock.py --run-dir ABSOLUTE_EXISTING_RUN check \
  --clock-sha256 ORIGINAL_HASH --phase preparation \
  --maximum-call-seconds 60 --closure-reserve-seconds 300
```

Exit 0 means this **time check** passed, 3 means the finite bound does not fit,
2 means validation/continuity failed. Refuse the operation on any nonzero exit or
uncertain result. Retain the JSON in the existing run evidence. A refusal permits
only the already authorized mandatory closure path; it never permits retries or
relaxed source checks. Unknown tool duration must not be represented as a guess
that claims an actual bound. Tool/provider call timeouts must enforce the supplied
maximum; this checker cannot interrupt an MCP call, model processing or final reply.

| Phase | Last admission | Minimum closing reserve |
| --- | --- | --- |
| collection, preparation, dispatch | strictly before 600 seconds | 300 seconds |
| feed_publish | at/before 420 seconds | 480 seconds, including refresh/readback |
| priority | at/before 780 seconds, exact same-run outcome required | 120 seconds |
| finalization | bounded work finished by 850 seconds | 50 seconds for transport |

Every call maximum plus its explicit closure reserve must fit the original
900-second ceiling. No normal call may extend beyond the 850-second tool-work
target. Priority checks require `--outcome-sha256` matching the private immutable
same-run `outcome.json`; this proves receipt binding only, not semantic acceptance.
The caller still enforces the one-priority-file rule and all original exceptions.
At/after 900 seconds normal checks refuse. The existing minimal late-closure
procedure preserves honest failure without normal work or budget reset.

For each existing Notion/Slack capture branch, invoke `begin-capture --capture
notion|slack --clock-sha256 HASH` once before any branch setup/read. Its exclusive
capture clock supplies a hash. Subsequent `collection` checks pass both `--capture`
and `--capture-sha256`; cumulative capture time plus the call maximum must fit 300
seconds as well as the original sweep. Never substitute a new capture clock.
Collection without capture metadata is for other already authorized collection
only; it must not be used to bypass those two branch caps.

Clock checks require the same host/boot session and non-regressing clocks.
Before initialization, the trigger has no monotonic sample; its UTC provenance
and an unobserved prior wall-clock change remain caller/platform limitations. Wall
elapsed includes suspension; monotonic elapsed prevents a small wall correction
from granting time back. Reboot, significant backward-clock drift or unknown
continuity fails closed. Metadata hashes prevent accidental replacement, not a
malicious same-user caller; caller identity and permissions are outside this tool.

## Latest attempt

The last useful source outcome and the latest attempted sweep are different.
A missed evening trigger must remain visible while afternoon source evidence is
still retained. Do not replace `latest_sweep` or refresh its source timestamps.

After a genuine immutable outcome or minimal late closure, create one envelope:

```sh
python3 tools/convergence/sweep_attempt.py --run-dir ABSOLUTE_EXISTING_RUN \
  --receipt ABSOLUTE_SAME_RUN_OUTCOME_OR_LATE_CLOSURE --receipt-sha256 EXACT_HASH \
  --local-date ACTUAL_SLOT_DATE --hour 9
```

`hour` is one of 9/13/17 in America/Los_Angeles. The actual trigger must fall within
15 minutes after the declared slot. The caller verifies the actual scheduler event
and accountable owner; a timestamp or fixed owner label alone is not that proof.
Creation does not update pointers. For an already-expired trigger, do not initialize
a replacement run clock: use only the existing minimal late closure and its
reviewed failure-reporting exception to record this exact envelope and pointer.
This does not turn the late run into timely work. If publication is uncertain,
retain the closure path without retry or further normal work.
Only the existing IRIS owner may atomically add
`NEXT_SESSION.latest_sweep_attempt` as the returned `{path, sha256}`, preserving all
other fields and refusing to replace a newer attempt with an older one.

The envelope `iris-sweep-attempt/v1` includes automation_id, owner_thread_id, run_id,
trigger_at, intended_slot {local_date,hour,timezone}, closed_at, recorded_at, status,
and receipt {path,sha256}. Closed time is the original linked terminal receipt;
recorded time is actual envelope publication. Historical review cannot backdate it.
`outcome_recorded` is not success; `missed_before_start` requires a late-closure
receipt after the ceiling explicitly stating no start and refused admission;
other supported uncertain late closure remains `unknown`.

Consumers validate the exact pointer hash, physical owner-private bounded files,
same-run source receipt/hash, fixed automation/owner, slot and timestamp ordering.
They do not follow paths outside the declared run root or promote attempt evidence
to current business-source coverage, human reading or milestone acceptance. The
briefing's optional `--latest-sweep-attempt` plus `--latest-sweep-attempt-sha256`
exposes the result separately. Its fixed 12,000-character budget remains intact.

The existing IRIS watchdog can consume the same contract, using its single current
notification/dedup state. Missing/invalid attempt coverage must be explicit; it must
not silence legacy receipt failures or claim delivery from a send attempt.
