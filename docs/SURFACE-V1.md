# surface/v1 -- per-environment state surface

Macro Seat spec v1, Artifact 1 (`build-plans/macro-seat-spec-2026-07-23.md`
SS1.1-1.2). One shared, committed, D30-class generator
(`generate-surface.py`) emits a machine-generated, provenance-tagged JSON surface
per canonical environment. The surface is the atomic unit the Artifact-3 boot-pack
assembler folds -- one per repo -- into the Macro Seat's starting context.

The generator is committed and shared; the emitted `surface.json` is **local-only
and gitignored** (ruling SS6.2). Never hand-edit a surface -- re-run the generator.

## What it is (and is not)

`surface.json` is generator output: every leaf claim is machine-derived
(`git`/`gh`/`file-exists`/`script-exit-code`) or a config-declared fact, and every
leaf carries `source` + `as_of` provenance -- or a `{"degraded": true, "reason":
...}` marker in place. It is not a MASTER-PLAN, not a design doc, and not a
hand-maintained surface.

`generate-surface.py` is **D30-class**: standalone, read-only inputs, stateless,
non-enforcing, report-only, manually invoked. It never fetches, mutates, commits,
pushes, or merges anything in any repo. The only file it writes is the surface.

## Fields (fixed top-level order)

| field | purpose |
| --- | --- |
| `schema` | always `"surface/v1"`. |
| `environment` | environment id (matches the P21 repo-manifest id). |
| `source_commit_time` | HEAD committer date (`%cI`) -- the deterministic timestamp. Degrades if HEAD is unreadable. |
| `generated_at` | ISO wall-clock. **Excluded from any diff comparison** (carved before comparing two surfaces). |
| `identity` | `{repo_path, canonical, role, default_branch, head_sha, behind_origin_main, source, as_of}`. `behind_origin_main` is an int, or a degraded marker when `origin/main` is absent. |
| `cold_load[]` | 3-6 repo-specific facts, each `{claim, source, as_of}` (or degraded in place). |
| `state` | `{open_prs, active_branches, verification}`. Each is a provenance-tagged list, or a degraded marker in place. |
| `decisions[]` | `{id, summary, kind: ratification\|merge\|ruling, waiting_since, source, as_of}` -- feeds the Artifact-3 unified decision queue. |
| `next_lanes[]` | `{entry, source, as_of}`. |
| `do_not_read[]` | `{entry, source, as_of}` -- forbidden / stale surfaces. |
| `pitfalls[]` | `{entry, source, as_of}`. |
| `next_session` | normalized view of the repo's root-level `NEXT_SESSION.json` via the M1 `next_session.py` parser (`{source, as_of, normalized{...}}`), or degraded if none. |

`state.open_prs[]` entries: `{number, title, head, base, source, as_of}`.
`state.active_branches[]` entries: `{name, source, as_of}`.
`state.verification[]` entries: `{name, checker_exit, notes, source, as_of}` (or
`{name, degraded, reason}`).

## Probe vocabulary (whitelist -- no arbitrary shell)

| kind | reads | notes |
| --- | --- | --- |
| `git-head` | `git rev-parse HEAD` | head sha; `as_of` = commit time. |
| `git-head-time` | `git log -1 --format=%cI HEAD` | the `%cI` committer date. |
| `git-behind` | `git rev-list --count HEAD..refs/remotes/origin/main` | reads EXISTING refs; **never fetches**. Degrades if the ref is absent. |
| `gh-pr-count` | count of open PRs | derived from the single `gh pr list` probe. |
| `file-exists` | existence of a repo-relative `path` | value is `yes`/`no`. |
| `script-exit-code` | exit code of a config-listed `script` | runs ONLY scripts declared in `verification[]` (or a cold-load probe), with a per-probe `timeout`. `.py` scripts run under the current interpreter. |

`gh` is queried once per surface (`gh pr list --state open`); it degrades
gracefully (`gh_missing`, `gh_auth_failed`, ...) when gh is missing,
unauthenticated, or erroring, and the whole run still exits 0 with the marker
inline.

## Config format (`surface-config/v1`)

Resolution order (first hit wins, spec SS1.2):

1. `<repo>/.macro/surface-config.json` (repo-local, if present)
2. `tools/configs/<environment>.json` (central, shipped here)

The `<environment>` is taken from `--environment`, else the repo basename.
`--config <path>` overrides resolution entirely.

```json
{
  "schema": "surface-config/v1",
  "environment": "<repo id>",
  "repo_path": "<abs path>",
  "canonical": true,
  "role": "<one line>",
  "default_branch": "main",
  "gh_repo": "owner/repo",
  "cold_load": [
    {"claim_template": "HEAD is {value}", "probe": {"kind": "git-head"}},
    {"claim": "<static fact>", "source": "<path/id>", "as_of": "<date>"}
  ],
  "verification": [
    {"name": "<label>", "notes": "<why>", "probe": {
      "kind": "script-exit-code", "script": "<repo-rel>", "args": ["--strict"],
      "timeout": 120}}
  ],
  "decisions":   [{"id", "summary", "kind", "waiting_since", "source", "as_of"}],
  "next_lanes":  [{"entry", "source", "as_of"}],
  "do_not_read": [{"entry", "source", "as_of"}],
  "pitfalls":    [{"entry", "source", "as_of"}]
}
```

A `cold_load` entry is either a **static fact** (`claim` + `source` + `as_of`) or
a **probe-driven fact** (`claim_template` with a `{value}` placeholder + `probe`;
optional `source`/`as_of` overrides). Probe failure lands as a degraded entry in
place -- never dropped. `next_lanes`/`do_not_read`/`pitfalls` entries may also be
bare strings (`source` defaults to `config`, `as_of` to the commit time).

## Degraded mode

Any probe that cannot answer emits `{"degraded": true, "reason": <enum/string>}`
**in place of** its value -- never a silent omission. The assembler renders these
as WARNINGs. A surface containing any degraded marker is reported DEGRADED on
stderr, but the generator still **exits 0** (a degraded surface is legitimate
local state; only `--stdout` and file writes are affected by the gitignore guard,
never by degradation). Hard failures (unresolvable config, missing repo, missing
`.macro` output dir, a non-gitignored in-repo output path) exit non-zero and write
nothing.

## Determinism

Byte-identical output on an unchanged repo, after carving the volatiles:

- fixed top-level key order + fixed nested shapes;
- `LC_ALL=C` child collation; PRs sorted by number, branches sorted by name, all
  config-driven lists kept in config order;
- single trailing newline;
- **no wall-clock in diffed content.** `generated_at` and live-probe `as_of`
  fields (gh, script-exit-code) hold wall-clock and are carved before comparison;
  every repo-fact `as_of` is the deterministic `%cI` committer date.

## Placement ruling (SS6.2): local-only, gitignored

`surface.json` lives at `<repo>/.macro/surface.json`, is **gitignored**, and is
never committed -- the consumer is the local assembler, so committing 14 repos'
surfaces would add PR churn with no reader. The generator **refuses** to write a
path inside the target repo unless that path is gitignored there; pass `--stdout`
(or `--out <path outside the repo>`) to bypass.

```
python3 generate-surface.py --environment marketing-os-main --stdout
python3 generate-surface.py --repo <path>            # -> <repo>/.macro/surface.json (must be gitignored)
python3 generate-surface.py --repo <path> --out <p>  # explicit output path
```

## Deviation from spec SS1.2 (per-repo config placement)

Spec SS1.2 places each repo's `surface-config.json` in that repo
(`<repo>/.macro/surface-config.json`). For M2 the two pilot configs are shipped
**centrally** under `tools/configs/` instead, so M2 stays a single-repo
PR with no cross-repo commits into marketing-os-main or saga-protocol. The
repo-local path still takes precedence when present (see resolution order), so
repos can adopt per-repo configs later with no generator change.
