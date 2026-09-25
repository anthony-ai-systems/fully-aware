# Initiative loop

## Purpose

This answers one question at session start and in the morning digest: is anything
currently able to originate and advance useful work, and if not, why not and what
would wake it? A stopped initiative loop has to be impossible to miss. It must never
read as a quiet day.

Fully Aware only reports and prepares. It never schedules, resumes or retries a
driver, never writes the IRIS docket, and never grants authority.

## What each module owns

| Module | Owns | Never does |
|---|---|---|
| `initiative_health.py` (Part A) | A read-only snapshot of the initiative drivers (the IRIS sweep heartbeat, the Fully Aware daily scan brief, the Radar daily heartbeat, intelligence pass receipts), the last successful sweep, missed attempts, the declared hold and whether it can resume, an event-level docket summary (answered requests with no follow-through, overdue rechecks, open requests), and local-worker throughput. From these it gives the state (`operating`, `degraded`, `stopped` or `unknown`), whether idling is legitimate, what wakes the loop next, and any decision Anthony has to make. | Writing anything. Importing IRIS code: the docket is parsed as raw JSON, so its figures are an event-level summary, not an IRIS replay. Reporting an input it could not read as healthy. |
| `intelligence_pass.py` (Part B) | Pass identity, the allocation and its accounting, missed-day and recovery rules, perspective rotation, the closed candidate shape, a novelty floor, suppression against earlier docket events, receipts, and the outbox handoff file in the docket's `opportunity_proposal` shape. | Calling a model or the network. Writing the docket, since the docket owner consumes the outbox. Turning an external claim into a fact about Anthony or a ratified preference. |
| `tools/daily-scan/intelligence-pass-prompt.md` and the optional stage 4 of `run-daily-scan.sh` (Part C) | The model's part: questions, research, judgment, and a fresh independent challenge per candidate. | Running unless `DAILY_SCAN_INTELLIGENCE=1` is set. Changing stages 1 to 3. |
| `registers/defects.json` item `INITIATIVE-1` | Keeping the stopped loop in the morning defect summary. | Nothing yet. It is provisional, so its check is a placeholder that is never run. |

State rules for `initiative_health.py`, using `policy.sweep_stale_hours` = 26:

- `operating`: the IRIS sweep driver is present and ACTIVE, a sweep outcome was recorded within 26 hours, and no attempt has been missed since.
- `stopped`: no IRIS sweep driver is present and no sweep has succeeded within 26 hours.
- `degraded`: any other case where the scheduler store is readable. That covers a driver that is present but paused, stale or missing attempts, and a missing driver whose last success is still recent.
- `unknown`: the Codex scheduler store (`codex-dev.db`) or all sweep evidence is unreadable. Absence cannot be established without the store.

Idling counts as legitimate only when the IRIS sweep driver is present and has a known future `next_run_at`, or when a declared hold has a resume condition that can actually be met. Another driver's future run, such as Radar's, does not make a stopped IRIS loop legitimate.

`--check` exits 0 when the state is operating, 1 when stopped or degraded, 3 when unknown, and 2 on a hard error.

## Proposed allocation values (proposals awaiting Anthony, not rulings)

| Budget | Proposed value |
|---|---|
| Wall time per pass | 25 minutes |
| Model launches | 3 |
| Source opens | 12 |
| Deep candidates | 2 |
| Opportunities presented per pass | at most 1 |
| Local window | 06:15–11:00 |
| Novelty threshold (token-set Jaccard) | 0.45 |
| Sweep staleness for initiative health | 26 hours |

A pass is due once per local day whatever the backlog size. An urgent incident preempts the pass, and the preemption is recorded. A missed or preempted day gets at most one recovery pass, which states the date it covers.

## Activation (all steps are Anthony's)

1. Review and merge the branch. Merge is Anthony's.
2. Once `~/code/fully-aware` carries `tools/convergence/initiative_health.py`, change `INITIATIVE-1`'s `verify` to `python3 "$HOME/code/fully-aware/tools/convergence/initiative_health.py" --check` and set `provisional` to false.
3. Decide how IRIS gets a heartbeat. Nothing here recreates or resumes one.
4. Approve or change the allocation values above.
5. Only after that, set `DAILY_SCAN_INTELLIGENCE=1` in the daily-scan LaunchAgent environment. Rehearse first with `DAILY_SCAN_STUB=1`.
6. Separately, deploy the IRIS docket change that accepts `opportunity_proposal`, which the IRIS task owns. It must be in place before any outbox file is consumed.

## Rollback

- Unset `DAILY_SCAN_INTELLIGENCE` so stage 4 logs `SKIPPED (not enabled)`.
- Delete `~/code/fully-aware/state/intelligence/`, which holds the receipts and outbox. It is gitignored state.
- Revert the commit. `initiative_health.py` holds no state, so reverting it removes it completely.

## What is NOT proven

- That any driver will actually fire. The report reads local stores only (`~/.codex/automations`, `codex-dev.db`), not the Codex app's server-side state, launchd, or whether the Mac is awake.
- That a recorded sweep outcome means accepted work, fresh sources or human delivery. The same goes for a Radar `last_run`, which is not proof of success.
- That the docket summary matches IRIS's own replay. It is event-level only, and the answered-without-follow-through list does not judge whether an answer still applies.
- That the daily scan ran today. It is judged only by the date on the newest `*-brief.md`.
- That the novelty floor detects material novelty. It is a crude, auditable Jaccard floor, and the model still has to say what is materially new.
- The allocation values. They are proposals, not measured optima.
- The intelligence pass end to end. It has only been exercised in stub mode, with no model calls.
