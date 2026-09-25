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
| `intelligence_pass.py` (Part B) | Pass identity, the allocation and its accounting, missed-day rules, perspective rotation, the closed candidate shape, the novelty corpus and a novelty floor, suppression against earlier docket events, receipts, saved proofs, and the outbox handoff file in the docket's `opportunity_proposal` shape. | Calling a model. Any network read other than the corpus's loopback GET of the local board. Writing the docket, since the docket owner consumes the outbox. Turning an external claim into a fact about Anthony or a ratified preference. |
| `tools/daily-scan/intelligence-pass-prompt.md`, `intelligence-challenge-prompt.md` and the optional stage 4 of `run-daily-scan.sh` (Part C) | The model's part: questions, research, judgment, and a fresh independent challenge per candidate. | Running unless `DAILY_SCAN_INTELLIGENCE=1` is set. Changing stages 1 to 3. |
| `registers/defects.json` item `INITIATIVE-1` | Keeping the stopped loop in the morning defect summary. | Nothing yet. It is provisional, so its check is a placeholder that is never run. |

State rules for `initiative_health.py`, using `policy.sweep_stale_hours` = 26:

- A driver is present only when the readable scheduler store (`codex-dev.db`) has a row for it. An `automation.toml` with no row is `present_in_config_only`: nothing will fire it, so it is not a driver.
- `operating`: the IRIS sweep driver is present and ACTIVE, a sweep outcome was recorded within 26 hours, and no attempt has been missed since.
- `degraded` with reason `scheduler_row_missing`: the IRIS automation is `present_in_config_only`. This also sets a required decision.
- `stopped`: no IRIS sweep driver is present and no sweep has succeeded within 26 hours.
- `degraded`: any other case where the scheduler store is readable. That covers a driver that is present but paused, stale or missing attempts, and a missing driver whose last success is still recent.
- `unknown`: the scheduler store or all sweep evidence is unreadable. Absence cannot be established without the store, so a config file alone then counts as present.

Idling counts as legitimate only when the IRIS sweep driver is present, ACTIVE and has a known future `next_run_at`, or when a declared hold has a resume condition that can actually be met. A PAUSED or otherwise non-ACTIVE driver never makes idling legitimate, whatever its `next_run_at`. Another driver's future run, such as Radar's, does not make a stopped IRIS loop legitimate.

The first markdown line gives the time of the last successful sweep. When no successful sweep has ever been observed it says `since unknown (no successful sweep observed)`, never the observation time.

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
| Missed-day lookback | 7 days |
| Sweep staleness for initiative health | 26 hours |

A pass is due once per local day whatever the backlog size. An urgent incident preempts the pass, and the preemption is recorded. Missed days are listed, never re-run and never backdated.

How these rules are implemented in `intelligence_pass.py`:

- There is one receipt per local date (`<date>.json`), and it is never overwritten. At most one pass runs per local day. A preempted day's receipt blocks a second pass that day (`preempted_today`).
- Every pass covers its own day. The first pass after a run of missed, preempted or failed days runs as `scheduled_after_gap`, still covers today, and lists those days in `gap_dates` as missed. They are not re-run. The run of days stops at the latest completed pass and reaches back no further than the first receipt or the 7-day lookback. `finalize_receipt` refuses a receipt whose `covers_date` is not its `date`, and one whose `gap_dates` do not match its mode.
- `before_window` and `window_closed` are logged skips with no receipt. A day whose window closed with no pass becomes a missed day.
- Allocation accounting: `finalize_receipt` compares `used` with the allocation (`model_launches`, `wall_seconds` against `wall_minutes` × 60, `source_opens`), wherever the allocation value is numeric. Any excess needs a non-empty `allocation_exceeded_reason`, or the receipt is refused. The receipt always records `allocation_exceeded` as true or false, computed and never taken from the input. The runner supplies the reason when the watchdog fires. Otherwise the model's own `allocation_exceeded_reason` is used, prefixed `model:`.
- Novelty corpus: stage 4 runs `intelligence_pass.py corpus` by default and writes `raw/corpus-<date>.json` under the receipt directory. It reads, read-only and tolerating absence: board titles and next actions (`GET http://127.0.0.1:4180/data/board.json`, 5 s timeout, loopback only), items waiting on Anthony in `~/code/state/plans-snapshot.json`, Radar `TOPICS.md` and `DECISIONS.md`, questions and recommendations from earlier docket packets, and earlier outbox files. Each source that is unconfigured or unreadable becomes a gap. Any gap, or no corpus at all, lowers internal coverage to `partial`. Stub runs read neither the board nor the docket. `DAILY_SCAN_INTEL_CORPUS` replaces the built corpus with a given file.
- Candidates go through checks in this order: closed-shape and challenge validation, then suppression against the docket, then the novelty floor, then the saved-proof check, then the `present_max` cap. The host fills in `generated_by` and `challenge.by`, and any challenge the generator wrote about its own candidate is thrown away.
- Proofs: a candidate carries its proof as `proof_body` (markdown, at most 20 KB). The host writes it to `proofs/<opportunity_id>.md` (0600) before the outbox file and sets `proof.ref` to the opportunity id, so the packet's reference names a file that exists. A candidate with no `proof_body` is `held` with reason `proof_missing` and is never presented. The docket's private-text rules apply to the packet, not to this local file.
- The packet asks a specific decision: `packet.question` is the candidate's required `decision_question` (the exact decision for this proposal, at most 1000 characters) and `packet.why_now` is `Independent discovery: ` plus its required `why_now`. The options stay proceed, modify, defer and decline. `origin.discovered_at` is the candidate's `discovered_at` when it is a valid time within the last 30 days, and otherwise the time of the pass.
- Suppression survives rewording. `evidence_revision` hashes only the set of evidence identities. For internal evidence that is the first whitespace-delimited token of each `ref`, lowercased (`board:8c7450d0`). For external evidence it is the optional `url` when present, and otherwise the source lowercased with whitespace collapsed, cut to 80 characters. Counterevidence, claims, ref descriptions, observation times and wording are excluded. Code enforces only "the set of evidence identities changed". Judging whether that change is material stays with the pass, the challenger and Anthony.
- If the docket is named but can't be read, every otherwise eligible candidate is `held`, because a declined idea must not be presented again without that check. Internal coverage is also marked `partial`.
- Earlier docket packets and outbox files enter the novelty corpus under their opportunity id, and the candidate's own id is left out. That way yesterday's proposal can't block today's with changed evidence, which suppression handles instead.

## Activation (all steps are Anthony's)

1. Review and merge the branch. Merge is Anthony's.
2. Once `~/code/fully-aware` carries `tools/convergence/initiative_health.py`, change `INITIATIVE-1`'s `verify` to `python3 "$HOME/code/fully-aware/tools/convergence/initiative_health.py" --check` and set `provisional` to false.
3. Decide how IRIS gets a heartbeat. Nothing here recreates or resumes one.
4. Approve or change the allocation values above.
5. Only after that, set `DAILY_SCAN_INTELLIGENCE=1` in the daily-scan LaunchAgent environment. Rehearse first with `DAILY_SCAN_STUB=1 DAILY_SCAN_INTELLIGENCE=1` in a scratch checkout. Stub stage 4 writes only to `state/intelligence-stub/` (receipt, `raw/` corpus, `proofs/`, `outbox/`), but stages 1 to 3 in stub mode still overwrite that checkout's brief. Its corpus build reads the plans snapshot and Radar files under `$HOME` read-only, and never the board or the docket. `test_intelligence_pass.StageFourStubTest` runs this rehearsal in a temp copy with fake `codex`, `claude`, `gh` and `launchctl` binaries.
6. Separately, deploy the IRIS docket change that accepts `opportunity_proposal`, which the IRIS task owns. It must be in place before any outbox file is consumed.

## Rollback

- Unset `DAILY_SCAN_INTELLIGENCE` so stage 4 logs `SKIPPED (not enabled)`.
- Delete `~/code/fully-aware/state/intelligence/` (and `state/intelligence-stub/` if a rehearsal ran), which hold the receipts and outbox. They are gitignored state.
- Revert the commit. `initiative_health.py` holds no state, so reverting it removes it completely.

## What is NOT proven

- That any driver will actually fire. The report reads local stores only (`~/.codex/automations`, `codex-dev.db`), not the Codex app's server-side state, launchd, or whether the Mac is awake.
- That a recorded sweep outcome means accepted work, fresh sources or human delivery. The same goes for a Radar `last_run`, which is not proof of success.
- That the docket summary matches IRIS's own replay. It is event-level only, and the answered-without-follow-through list does not judge whether an answer still applies.
- That the daily scan ran today. It is judged only by the date on the newest `*-brief.md`.
- That the novelty floor detects material novelty. It is a crude, auditable Jaccard floor over word overlap, and the model still has to say what is materially new. Its corpus covers the board, the plans snapshot's waiting-on-Anthony items, Radar topics and decisions, earlier docket packets and outbox files. It does not cover bookmarks or prompts. The board and plans parsers follow the shapes observed on 2026-09-25 (`items[].project`/`next_action`, `lanes[].waiting_on_anthony`), and a changed shape shows up as an empty source or a gap. The Radar parser reads markdown table rows and bold-led paragraph IDs only.
- The allocation values. They are proposals, not measured optima.
- The intelligence pass end to end. It has only been exercised in stub mode, with no model calls.
- The `-c web_search="live"` override that stage 4 passes to `codex exec` has not been verified against the installed codex. `DAILY_SCAN_INTEL_CODEX_SEARCH=` (empty) removes it. Without web access, the pass has to report external coverage as a gap.
- That the outbox is ready to consume as written. `source.verified_at` is the time of the pass, so the consumer has to stamp it again within the docket's 900 s recheck window. The docket also has to have the `opportunity_proposal` change installed.
- That suppression catches every resubmission. Rewording no longer changes the revision, but it still depends on the model reusing the same ref identifiers and URLs and the same `dedupe_key`. A new `dedupe_key` for the same idea is a new opportunity. Citing one new or differently named ref or source makes it `eligible_changed_evidence`, even when nothing material changed. Code enforces only "the set of evidence identities changed". Materiality is judged by the pass, the challenger and Anthony.
- `used.source_opens`. The model reports this number itself, and the code does not measure it, so its allocation check is only as honest as that report.
- That a saved proof is correct or sufficient. Code checks only that `proof_body` exists and is at most 20 KB. The challenger is asked whether it supports the recommendation.
- That `decision_question` is actually specific. Code checks presence, length and private text only. The prompt and the challenger push for a specific decision, but nothing measures it.
- That a missed day's work is recovered. By design it is not: missed days are listed in `gap_dates` and never re-run.
