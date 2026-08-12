# M3 taste distiller (macro-seat spec §2)

Session-close taste capture into imprint's ingest quarantine. Three stages:

1. **Stop hook** (`distill_spool_hook.py`, installed by the arm script with the
   `macroseat-managed-hook` marker): appends `{session_id, transcript_path,
   project_dir, ts}` to the queue. ≤200 ms budget, fail-open — a spool failure
   never blocks a session from stopping.
2. **Worker** (`taste_distiller.py`, LaunchAgent `com.macroseat.taste-distiller`,
   RunAtLoad + every 15 min): drains the queue (default 6 sessions/tick,
   `MACROSEAT_MAX_SESSIONS` to repace), streams the full transcript (sidechains
   dropped, char-capped, user turns kept whole), anchors on the `/handoff`
   TASTE MARKER block when present, distills specimens with one headless
   `claude -p --model haiku` call, assigns entity scope (registry globs +
   content cues; ambiguous → flagged, re-scopable at keep time), and emits
   IngestCandidates via `imprint ingest scan`. **Never `keep`/`kill` — the
   judgment gate is Anthony's, structurally.**
3. **Judgment**: `imprint ingest show/keep/kill` — unchanged, human-only.

State lives in imprint's operator data root under `macroseat/` (queue, ledger,
entity registry, lock) — deliberately NOT imprint's own `spool/` subsystem.
The entity registry holds client names, so only `entity-registry.example.json`
(placeholders) is committed; the live copy stays local (P25).

**Arm (Anthony-run):** `bash arm-taste-distiller.sh` — creates the namespace,
seeds the registry if absent, installs the Stop hook idempotently, bootstraps
the LaunchAgent, measures hook overhead.

**Backfill (§2.4, one-shot):** `python3 backfill_seed.py --yes` seeds the
queue with all surviving transcripts (dry-run without `--yes`); pace the drain
with `MACROSEAT_MAX_SESSIONS=200 python3 taste_distiller.py` in a manual loop.
Trivial transcripts are skipped before any model call.

Idempotency: ingest's content-sha dedupe + the worker ledger (queue is
append-only; the ledger is the source of truth, keeping the worker race-free
against concurrent hook appends).

Tests: `python3 -m unittest test_taste_distiller -v` (synthetic fixtures;
model + ingest mocked; never touches real settings or data roots).

Note: this worker deviates from the fully-aware read-only tool rule the same
way `daily-scan` does — it spends model tokens and writes outside `state/`
(imprint quarantine + `macroseat/` state). It never touches git, never
commits, never merges, and never promotes quarantine items.
