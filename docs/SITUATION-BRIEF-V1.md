# Situation brief v1

`tools/convergence/situation_brief.py` is a bounded, read-only consumer for
an on-demand local situation brief. It prints JSON by default and Markdown
with `--format markdown`:

```sh
python3 tools/convergence/situation_brief.py \
  --boot-pack /absolute/path/boot-pack.json \
  --plans /absolute/path/plans-snapshot.json \
  [--sweep-outcome /absolute/path/outcome.json] \
  [--format markdown]
```

The two Fully Aware inputs are explicit paths and are read through the
existing `work_view.load_input` and `work_view.build_view` contracts. The
projection keeps only the `fully-aware-convergence`, `iris`, and
`autonomous-operators` lanes, with a count for other valid lanes. Missing or
malformed inputs remain unavailable sections; an empty projection is not
treated as proof that no work exists.

IRIS is read only through fixed GET requests to
`http://127.0.0.1:4180/healthz`, `/data/board.json`, `/focus.json`, and
`/local-agent.json`. The board is read before and after the other requests.
The board is current only when the producer proof and ordered ID/status
binding match exactly and health reports current verified freshness, a recent
generation, matching `verified_at`, and no `last_error`. Redirects and proxy
settings are refused; each response is bounded at 2 MiB and has a timeout.
The reader independently enforces a 300-second board-generation age and the
existing IRIS source-verification age contract of 108,000 seconds (30 hours).
These are different clocks: a normal minute-by-minute board refresh does not
refresh the source capture. The source timestamp remains visible. The source
limit mirrors `IRIS/client-work-board/scripts/freshness_model.py` at the accepted
consumer revision; there is no runtime cross-repository import.

Focus rows retain the source key/work identity, group, state, applicability,
match, bounded question/recommendation, snooze, transport, and response count.
They do not turn transport into human delivery, an answer, or authority. Local
activity retains exact bounded run/owner/artifact/review identities and stale
labels; it never infers a running process or business acceptance.

An optional sweep outcome may name one existing `daily_digest.path`. The file
must be a bounded regular file and its raw SHA-256 must equal the declared
hash. Its evidence cutoff must be parseable, non-future, and no more than 24
hours old. At most 4,000 characters are labelled as dated, untrusted advisory
source text, with source cutoff, timezone, and omitted character count. The
text is never parsed as instructions or authority. The deadline baseline is a
separate deterministic list of at most three earliest explicit due rows from
the board, excluding terminal statuses; it does not rank priorities.

Output is stdout only, with no cache or arbitrary output path. JSON is bounded
to approximately 12,000 characters and optional detail is explicitly omitted
if the bound is reached. Evidence includes source references, observed times,
source timestamps, freshness, and hashes where available. A successful CLI
run is only a local observation; it is not proof of voice delivery,
notification, human reading, an answer, or milestone acceptance.

Focused checks:

```sh
python3 -m unittest discover -s tools/convergence -p 'test_situation_brief.py'
python3 -m unittest discover -s tools/convergence -p 'test_work_view.py'
python3 -m py_compile tools/convergence/situation_brief.py
git diff --check
```
