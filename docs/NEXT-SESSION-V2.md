# `next-session/v2` — unified NEXT_SESSION schema

Status: v2 (Fully Aware lane M1). Parser: [`next_session.py`](../tools/next_session.py).

The machine survey found **five** divergent NEXT_SESSION schemas across ~1,761
files on this laptop. `next-session/v2` is the single shape consumers read. One
parser module — `next_session.py` — normalizes all legacy variants into v2
**read-only**; new interactive/continuity handoffs are **written** directly as
v2.

## Consumers

| Consumer | State | How it reads v2 |
|----------|-------|-----------------|
| boot-pack assembler | **wired** | `assemble-boot-pack.py` imports this module in-repo and projects each manifest repo's `human_only[]` into the unified decision queue. |
| Iris | **archived**, no live consumer | The Iris console read the boot pack's JSON sidecar (v2 reached it through the assembler, never a direct parser call). It was archived 2026-08-04 — anthony-wiki-vault `c54cb6c`, now under `IRIS-archive/control-center`. Contract retained for revival; nothing external reads v2 today. |
| mission-control | **intended**, not yet wired | No mission-control code reads v2 today. A design target, not a live consumer. |

## Field table

| Field | Req | Type | Meaning |
|-------|-----|------|---------|
| `schema` | yes | `"next-session/v2"` | Format tag. |
| `written_at` | yes | ISO-ish string | When the handoff was written. |
| `environment` | yes | string | Repo/environment id (matches the P21 repo-manifest id where one exists). |
| `status` | yes | enum | One of `in-progress` \| `blocked` \| `parked` \| `done`. Best-effort when normalized; defaults to `parked` when undeterminable. |
| `summary` | yes | string (prose) | Recap of where the session left things. |
| `next_action` | yes | object | `{ "who", "what", "then"? }`. `who` = who acts next; `what` = the next action; `then` = optional follow-on. |
| `branch` | no | string | Working branch, when repo-scoped. |
| `worktree` | no | string | Absolute worktree path, when repo-scoped. |
| `expected_head` | no | string | Expected HEAD sha to resume against. |
| `human_only` | no | string[] | Decisions only Anthony can make — a direct decision-queue feed. |
| `pitfalls` | no | string[] | Traps/landmines/gotchas/warnings to avoid. |
| `read_first` | no | string[] | Ordered minimum read set before acting. |
| `evidence` | no | object | Named map of proof (commands run, gates passed, verification, PR object). |
| `legacy` | no | object | Any source field not mapped above, preserved **verbatim** so nothing is lost in normalization. |

## Legacy schema → v2 semantic mapping

The parser ships one adapter per legacy schema. Mapping (per M1 spec §1.3):

| v2 target | A (SAGA run-bundle) | B (optimus `next-session/v1`) | D (console handoff) | E (Codex packet) |
|-----------|---------------------|-------------------------------|---------------------|------------------|
| `next_action.what` | `next_action` (str) | `state.next_in_order` or `next_action.what` | `next[]` (first) | `next_actions[]` + `gates[]` (else `next_session_role`) |
| `next_action.who` | `operator` | `next_action.who` | — | `by` |
| `human_only` | — | — | `anthony_solo` | — |
| `status` | `status` + `phase` | `state` (`blocked_on_anthony` ⇒ blocked) | `state` | `pull_request.state` + `mergeStateStatus` |
| `pitfalls` | `issues_discovered` | `landmines` \| `traps` | `gotchas` | `warnings` (+ `prerequisites`) |
| `summary` | `summary` | `session` | `state` | `deliverables` + `completed` |
| `read_first` | `cites_precedent` | — | `read_first` | `read_in_order` |
| `written_at` | `date` | `written_at` | `written` | `written` |
| `branch`/`worktree`/`expected_head` | direct (or `git_preconditions.*` in the D38 variant) | direct | — | `branch`/`worktree`/`local_commit` |
| `evidence` | `commands_run` | — | `evidence` | `gates_passed` + `verification` + `pull_request` |

Anything not in the table above lands under `legacy` verbatim.

### v2-tagged files that carry the free-form shape — gap fill
Several on-disk files declare `"schema": "next-session/v2"` but hold the
free-form fallback field set (`session_date` / `state` / `launch_one_liner` /
`next_action_for_agent`) instead of the v2 fields. Plain passthrough normalized
those to an **empty summary**, so their authored content — including parked
directives — never reached a reader. `adapt_v2` therefore gap-fills: when the
summary comes out empty **and** a fallback-shape key is present, the missing
fields are filled with the same mappings `adapt_fallback` uses (`state` →
`summary`/`status`, `session_date` → `written_at`, `next_action_for_agent` (else
`launch_one_liner`) → `next_action.what`). Files that genuinely conform to v2
are untouched: their own non-empty values always win.

### Schema C — excluded by path rule
Any file matching `*/tests/fixtures/heartbeat-bundle/NEXT_SESSION.json` is a
committed test fixture and is **excluded** from normalization (emitted as
`detected_schema: "C", excluded: true`).

### Schema E — invalid-JSON tolerance
Some marketing-os Codex packet handoffs are **invalid strict JSON**: successive
packets were appended without array brackets, and in the worst cases a new
packet's keys were merged into the prior object's unterminated array. The E
adapter therefore runs a lenient decode: a `raw_decode` loop for the
valid-single / cleanly-concatenated cases, then a salvage scraper that walks
`"key": value` pairs top-down (a repeated key marks a record boundary,
undecodable values are skipped) so the leading (most recent) packet is still
recovered. Recovered records are tagged
`legacy._recovered_from_invalid_json: true`. Normalization never mutates the
source file; the separate writer fix that stops the bad append lives with the
marketing-os writer, not here.

## Writer guidance (new sessions)

New interactive/continuity handoffs should be written **directly as v2**:

```json
{
  "schema": "next-session/v2",
  "written_at": "2026-01-01T00:00:00Z",
  "environment": "my-repo",
  "branch": "feat/thing",
  "worktree": "/Users/you/code/my-repo-thing",
  "expected_head": "abc1234",
  "status": "in-progress",
  "summary": "What happened and where it stands.",
  "next_action": { "who": "opus", "what": "do the next thing", "then": "await merge" },
  "human_only": ["a decision only Anthony can make"],
  "pitfalls": ["a trap to avoid"],
  "read_first": ["docs/context.md"],
  "evidence": { "ci": "green" }
}
```

Rules:
- One JSON object per file. **Never append** a second object — that is exactly
  the corruption the E adapter has to tolerate. Rewrite the whole file each time.
- `status` must be one of the four enum values.
- `next_action` is always the object form (`who`/`what`, optional `then`).
- Put decisions that only Anthony can make under `human_only` — that array is a
  direct feed into the unified decision queue.

## SAGA run-bundle NEXT_SESSION stays under bundle law

Schema A is the SAGA run-bundle NEXT_SESSION (the large majority of files, under
`runs/`). It is **bundle-law territory**, versioned and owned by saga-protocol.
This parser only **normalizes** Schema A into the v2 view for consumers — it
never rewrites bundles and imposes no v2 requirements on them. Run bundles keep
their own field set; new run-bundle writes stay under saga-protocol, not v2.

## CLI

```
python3 next_session.py scan <root> [--jsonl]
```

Walks `<root>` (skipping `node_modules` and `.git`), classifies + normalizes
every `NEXT_SESSION.json`, and emits one record per file — either
`{source_path, detected_schema, normalized{…}}`, or
`{source_path, detected_schema, unparseable: true, error}`, or
`{source_path, detected_schema: "C", excluded: true}`. Default output is a JSON
array; `--jsonl` emits newline-delimited JSON. A per-schema summary (including
the unparseable count) is written to stderr.

## Tests

```
python3 -m unittest test_next_session -v
```

Stdlib `unittest`, no third-party deps. All fixtures are synthetic — no real
handoff prose or client names — including a concatenated-invalid-JSON E fixture.
