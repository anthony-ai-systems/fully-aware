#!/usr/bin/env python3
"""Macro-seat taste distiller worker (spec: macro-seat-spec-2026-07-23.md §2, M3).

Drains the session-close spool queue, streams each session's full transcript
JSONL (sidechains dropped, per-session char cap enforced), runs one headless
Haiku call to extract taste specimens (corrections, rulings, preferences,
approved patterns), and emits each specimen as an IngestCandidate to
``imprint ingest scan`` — quarantine only, NEVER ``keep``. Anthony's keep/kill
is the only promotion path; that gate is structural, not procedural.

Runs detached (LaunchAgent com.macroseat.taste-distiller); killing it must be
invisible to sessions, so the only session-facing artifact is the append-only
queue the Stop hook writes. Idempotency is two-layer: ingest's server-side
content sha256 dedupe, plus this worker's ledger of processed session_ids
(survives re-runs and backfill overlap). The queue is append-only and never
compacted during normal runs — the ledger is the source of truth, which keeps
the worker race-free against concurrent hook appends.

Entity scoping (§2.5): every specimen carries a required scope — ``global`` or
``entity:<slug>`` — assigned from the entity registry's project-dir globs plus
content cues; ambiguous assignments are flagged in the envelope and re-scopable
at keep time. The registry lives in imprint's operator data root because it
holds client names (P25: canonical repos and the vault stay client-clean).
"""

import datetime
import fcntl
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

VERSION = "com.macroseat.taste-distiller/1.0.0"

DEFAULT_CONFIG = os.path.expanduser("~/.config/imprint/config.json")

# Per-run session cap bounds token spend per 15-min tick; raise via env for
# manually paced backfill runs (see backfill_seed.py).
MAX_SESSIONS = int(os.environ.get("MACROSEAT_MAX_SESSIONS", "6"))

# Per-session excerpt budget (chars; ~4 chars/token). User turns are the taste
# signal and are kept whole; assistant turns are trimmed first.
MAX_EXCERPT_CHARS = int(os.environ.get("MACROSEAT_MAX_EXCERPT_CHARS", "240000"))
MAX_ASSISTANT_TURN_CHARS = 1500
MAX_USER_TURN_CHARS = 8000

# Transcripts below these floors carry no distillable taste; skipping them
# without a model call is what makes the 6.4K-transcript backfill tractable.
MIN_USER_TURNS = 2
MIN_TOTAL_CHARS = 2000

MODEL_TIMEOUT_SECS = 300
MARKER_HEADING = "## TASTE MARKER"

# Stop fires at the END OF EVERY ASSISTANT TURN, not once per session — the
# queue accumulates per-turn breadcrumbs. A session is distilled only after
# its transcript has been quiet this long (quiet ≈ session over), and errors
# are retried at most this many times before the ledger goes terminal.
# (Quiet-gate + retry design credit: the ceded parallel M3 build, 2026-08-11.)
DEFAULT_QUIET_SECONDS = 1800
RETRY_LIMIT = 3

_ALIAS_WORD = r"(?<![A-Za-z0-9]){alias}(?![A-Za-z0-9])"


# --------------------------------------------------------------------------- #
# paths / state
# --------------------------------------------------------------------------- #
def load_imprint_config(path=None):
    """Resolve imprint's data_root + operator_slug (IMPRINT_CONFIG honored)."""
    path = path or os.environ.get("IMPRINT_CONFIG") or DEFAULT_CONFIG
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    return cfg["data_root"], cfg["operator_slug"]


def macroseat_root(data_root, operator_slug):
    """Macro-seat's own namespace inside the operator data root. Deliberately
    NOT imprint's ``spool/`` dir — that is imprint's producer-scoped subsystem
    and foreign files there would collide with its semantics."""
    return os.path.join(data_root, operator_slug, "macroseat")


def queue_path(root):
    return os.path.join(root, "distill-queue.ndjson")


def ledger_path(root):
    return os.path.join(root, "distill-ledger.json")


def registry_path(root):
    return os.path.join(root, "entity-registry.json")


def read_queue(path):
    """Lenient NDJSON read: malformed lines are reported, never fatal."""
    entries, bad = [], 0
    if not os.path.exists(path):
        return entries, bad
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(obj, dict) and obj.get("session_id"):
                entries.append(obj)
            else:
                bad += 1
    return entries, bad


def load_ledger(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except ValueError:
        # A corrupt ledger must not wedge the worker forever; preserve the
        # evidence and start fresh (ingest sha-dedupe absorbs the re-scans).
        shutil.copy2(path, path + ".corrupt")
        return {}


def save_ledger(path, ledger):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_registry(path):
    """Entity registry ({slug, kind, aliases[], project_dir_globs[]} entries).
    Absent or unreadable → empty registry (everything scopes global)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        entities = data.get("entities", [])
        return [e for e in entities if isinstance(e, dict) and e.get("slug")]
    except (OSError, ValueError):
        return []


# --------------------------------------------------------------------------- #
# transcript reading
# --------------------------------------------------------------------------- #
def _block_text(block):
    if isinstance(block, str):
        return block
    if isinstance(block, dict) and block.get("type") == "text":
        return block.get("text", "")
    return ""


_NOISE_PREFIXES = ("<command-", "<local-command", "<system-reminder>",
                   "Caveat: ", "[Request interrupted")


def _is_noise(text):
    return text.lstrip().startswith(_NOISE_PREFIXES)


def _result_text(block):
    """Flatten a tool_result's content (string or block list) to text."""
    content = block.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(t for t in (_block_text(b) for b in content) if t)
    return ""


# Tool results are noise — except these: their results ARE operator judgment
# (AskUserQuestion answers are decisions Anthony typed/selected).
_DECISION_TOOLS = ("AskUserQuestion",)


def extract_turns(transcript_path):
    """Stream the FULL transcript JSONL (the hook's 2MB tail reader is the
    wrong tool here). Yields (line_no, role, text) for user/assistant text
    turns; sidechains, tool traffic, and command noise dropped — but decision
    tools' results (AskUserQuestion) are kept as user turns: the ratification
    YES/NO answers flow through them, and they are high-grade taste signal."""
    turns = []
    decision_tool_ids = set()
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for line_no, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if obj.get("isSidechain"):
                continue
            typ = obj.get("type")
            if typ not in ("user", "assistant"):
                continue
            content = (obj.get("message") or {}).get("content", "")
            if isinstance(content, list):
                if typ == "assistant":
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "tool_use"
                                and block.get("name") in _DECISION_TOOLS):
                            decision_tool_ids.add(block.get("id"))
                else:
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "tool_result"
                                and block.get("tool_use_id") in decision_tool_ids):
                            answer = _result_text(block).strip()
                            if answer:
                                turns.append((line_no, "user",
                                              "[decision] " + answer))
                text = "\n".join(t for t in (_block_text(b) for b in content) if t)
            else:
                text = content if isinstance(content, str) else ""
            text = text.strip()
            if not text or _is_noise(text):
                continue
            turns.append((line_no, typ, text))
    return turns


def build_excerpt(turns, max_chars=MAX_EXCERPT_CHARS):
    """One line-tagged excerpt under the char cap. User turns are kept (they
    carry the taste); assistant turns are trimmed per-turn, then dropped
    oldest-first if still over budget. Never silent: gaps are marked."""
    trimmed = []
    for line_no, role, text in turns:
        cap = MAX_USER_TURN_CHARS if role == "user" else MAX_ASSISTANT_TURN_CHARS
        if len(text) > cap:
            text = text[:cap] + " …[trimmed]"
        trimmed.append((line_no, role, text))

    def render(items, dropped):
        parts = []
        for line_no, role, text in items:
            parts.append("[L%d %s] %s" % (line_no, role.upper(), text))
        if dropped:
            parts.insert(0, "[%d earlier assistant turns omitted for budget]" % dropped)
        return "\n\n".join(parts)

    dropped = 0
    items = list(trimmed)
    excerpt = render(items, dropped)
    while len(excerpt) > max_chars:
        idx = next((i for i, (_, role, _) in enumerate(items) if role == "assistant"), None)
        if idx is None:
            # Only user turns left; hard-truncate from the front.
            items = items[1:]
        else:
            items.pop(idx)
            dropped += 1
        excerpt = render(items, dropped)
        if not items:
            break
    return excerpt, dropped > 0 or len(items) < len(trimmed)


def _parse_marker_block(text):
    """Marker bullets from one string containing the TASTE MARKER heading."""
    found, in_block = [], False
    for ln in text.splitlines():
        if MARKER_HEADING in ln:
            in_block = True
            continue
        if in_block:
            ln = ln.strip()
            if ln.startswith("- "):
                found.append(ln[2:])
            elif ln.startswith("#") or (ln and not ln.startswith("-")):
                in_block = False
    return found


def _collect_marker_strings(obj, out):
    if isinstance(obj, str):
        if MARKER_HEADING in obj:
            out.append(obj)
    elif isinstance(obj, dict):
        for value in obj.values():
            _collect_marker_strings(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _collect_marker_strings(value, out)


def find_taste_markers(transcript_path):
    """TASTE MARKER lines from the /handoff block, wherever they appear.
    Handoff = enricher, hook = guarantee. The handoff is written via the Write
    tool, so the block usually appears JSON-ESCAPED inside a tool_use input —
    invisible to the rendered-turn reader. Walking every string of each line's
    parsed JSON gets correct unescaping for free."""
    markers, seen = [], set()
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for line_no, raw in enumerate(fh, 1):
            if "TASTE MARKER" not in raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            strings = []
            _collect_marker_strings(obj, strings)
            for text in strings:
                for marker in _parse_marker_block(text):
                    if marker not in seen:
                        seen.add(marker)
                        markers.append((line_no, marker))
    return markers


# --------------------------------------------------------------------------- #
# distillation (headless Haiku — ruling §6.3)
# --------------------------------------------------------------------------- #
PROMPT_TEMPLATE = """\
You are a taste distiller. The transcript excerpt below is from a Claude Code
session belonging to Anthony (the operator). Extract 0-N taste SPECIMENS —
moments where the operator's judgment was expressed:

- correction: the operator corrected the assistant's approach, output, or belief
- ruling: the operator made an explicit decision or ruling
- preference: the operator stated a durable preference or standard
- approved_pattern: the operator explicitly confirmed an approach as right

Rules:
- Specimens must come from the OPERATOR's words (USER turns), with the
  assistant context only as the why. Do not invent; quote faithfully.
- Skip one-off task instructions with no durable judgment content.
- An empty array is a fine answer. Quality over quantity.
- Each specimen: the quote, the surrounding why, the [L<n>] line numbers it
  spans, and a scope_hint — one of the entity slugs below if the judgment is
  about that entity's work, else "global".
- If a TASTE MARKER list is provided, each marker line is a guaranteed
  specimen: locate its origin in the excerpt, set marker_derived true.

Known entity slugs: {slugs}

{markers_section}

Return ONLY a JSON array (no prose, no code fences) of objects with keys:
quote, why, kind (correction|ruling|preference|approved_pattern),
line_start (int), line_end (int), scope_hint, marker_derived (bool).

TRANSCRIPT EXCERPT:
{excerpt}
"""


def build_prompt(excerpt, markers, registry):
    slugs = ", ".join(e["slug"] for e in registry) or "(none registered)"
    if markers:
        lines = "\n".join("- %s" % m for _, m in markers)
        markers_section = "TASTE MARKER lines (guaranteed specimens):\n" + lines
    else:
        markers_section = "No TASTE MARKER block present."
    return PROMPT_TEMPLATE.format(slugs=slugs, markers_section=markers_section,
                                  excerpt=excerpt)


def _claude_bin():
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def call_model(prompt):
    proc = subprocess.run(
        [_claude_bin(), "-p", "--model", "haiku"],
        input=prompt, capture_output=True, text=True, timeout=MODEL_TIMEOUT_SECS)
    if proc.returncode != 0:
        raise RuntimeError("claude -p failed rc=%d: %s"
                           % (proc.returncode, proc.stderr.strip()[:400]))
    return proc.stdout


def parse_specimens(output):
    """Lenient parse: strip fences, find the outermost JSON array."""
    text = output.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array in model output")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("model output is not a list")
    out = []
    for item in data:
        if not isinstance(item, dict) or not item.get("quote"):
            continue
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# entity scoping (§2.5)
# --------------------------------------------------------------------------- #
def _dir_entities(project_dir, registry):
    hits = []
    base = os.path.basename(project_dir.rstrip("/")) if project_dir else ""
    for ent in registry:
        for glob in ent.get("project_dir_globs", []):
            if fnmatch.fnmatch(project_dir or "", glob) or fnmatch.fnmatch(base, glob):
                hits.append(ent["slug"])
                break
    return hits


def _cue_entities(text, registry):
    hits = []
    for ent in registry:
        for alias in ent.get("aliases", []) + [ent["slug"]]:
            pattern = _ALIAS_WORD.format(alias=re.escape(alias))
            if re.search(pattern, text, re.IGNORECASE):
                hits.append(ent["slug"])
                break
    return hits


def assign_scope(specimen, project_dir, registry):
    """→ (scope, ambiguous). Directory mapping is the strong signal; content
    cues (aliases + the model's scope_hint) corroborate or flag. Ambiguous
    specimens stay ``global`` + flagged — re-scopable at keep time."""
    dir_hits = _dir_entities(project_dir, registry)
    cue_text = "%s\n%s" % (specimen.get("quote", ""), specimen.get("why", ""))
    cue_hits = set(_cue_entities(cue_text, registry))
    hint = specimen.get("scope_hint") or "global"
    known = {e["slug"] for e in registry}
    if hint.startswith("entity:"):
        hint = hint.split(":", 1)[1]
    if hint in known:
        cue_hits.add(hint)

    if len(dir_hits) == 1:
        slug = dir_hits[0]
        if cue_hits and cue_hits != {slug}:
            return "entity:%s" % slug, True
        return "entity:%s" % slug, False
    if len(dir_hits) > 1:
        return "global", True
    if len(cue_hits) == 1:
        return "entity:%s" % cue_hits.pop(), True
    if len(cue_hits) > 1:
        return "global", True
    return "global", False


# --------------------------------------------------------------------------- #
# candidate emission
# --------------------------------------------------------------------------- #
def build_candidates(specimens, entry, registry, now_iso):
    candidates = []
    for spec in specimens:
        scope, ambiguous = assign_scope(spec, entry.get("project_dir", ""), registry)
        line_start = int(spec.get("line_start") or 0)
        line_end = int(spec.get("line_end") or line_start)
        kind = spec.get("kind") or "preference"
        # Content is the sha-dedupe key — keep it stable (no timestamps).
        content = "[%s] %s\nWHY: %s" % (kind, spec["quote"].strip(),
                                        (spec.get("why") or "").strip())
        candidates.append({
            "source_kind": "session_transcript",
            "source_locator": "%s#L%d-L%d" % (entry["transcript_path"],
                                              line_start, line_end),
            "content": content,
            "metadata": {
                "session_id": entry["session_id"],
                "project_dir": entry.get("project_dir", ""),
                "tenant_scope": scope,
                "marker_derived": bool(spec.get("marker_derived")),
                "distiller": VERSION,
                "distilled_at": now_iso,
            },
            "extensions": {
                "taste.v1": {
                    "kind": kind,
                    "scope": scope,
                    "scope_ambiguous": ambiguous,
                    "quote": spec["quote"].strip(),
                    "why": (spec.get("why") or "").strip(),
                    "line_start": line_start,
                    "line_end": line_end,
                    "marker_derived": bool(spec.get("marker_derived")),
                },
            },
        })
    return candidates


def _imprint_bin():
    return shutil.which("imprint") or os.path.expanduser("~/.local/bin/imprint")


def ingest_scan(candidates):
    """Quarantine the candidates. This is the ONLY imprint write this worker
    ever performs — ``ingest keep``/``kill`` are Anthony's, never called here."""
    if not candidates:
        return "no candidates"
    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="macroseat-ingest-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(candidates, fh)
        proc = subprocess.run([_imprint_bin(), "ingest", "scan", "--input", tmp],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError("imprint ingest scan rc=%d: %s"
                               % (proc.returncode, proc.stderr.strip()[:400]))
        return proc.stdout.strip()
    finally:
        os.unlink(tmp)


# --------------------------------------------------------------------------- #
# per-session pipeline + main
# --------------------------------------------------------------------------- #
def process_session(entry, registry, now_iso):
    """→ ledger record for this session (status + counts, never raises)."""
    path = entry.get("transcript_path", "")
    if not path or not os.path.exists(path):
        return {"status": "transcript_missing", "processed_at": now_iso}
    try:
        turns = extract_turns(path)
        user_turns = [t for t in turns if t[1] == "user"]
        total_chars = sum(len(t[2]) for t in turns)
        if len(user_turns) < MIN_USER_TURNS or total_chars < MIN_TOTAL_CHARS:
            return {"status": "skipped_trivial", "processed_at": now_iso,
                    "user_turns": len(user_turns), "chars": total_chars}
        excerpt, truncated = build_excerpt(turns)
        markers = find_taste_markers(path)
        prompt = build_prompt(excerpt, markers, registry)
        try:
            specimens = parse_specimens(call_model(prompt))
        except ValueError:
            retry = prompt + "\n\nPrevious output was not valid JSON. Return ONLY the JSON array."
            specimens = parse_specimens(call_model(retry))
        candidates = build_candidates(specimens, entry, registry, now_iso)
        result = ingest_scan(candidates)
        return {"status": "distilled", "processed_at": now_iso,
                "specimens": len(candidates), "markers": len(markers),
                "truncated": truncated, "ingest": result[:200]}
    except Exception as exc:  # noqa: BLE001 — worker must never crash the drain loop
        return {"status": "error", "processed_at": now_iso,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:300])}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="macro-seat taste distiller worker")
    ap.add_argument("--session", metavar="ID",
                    help="force ONE session: bypass ledger + quiet gate for it")
    args = ap.parse_args(argv)

    data_root, operator = load_imprint_config()
    root = macroseat_root(data_root, operator)
    os.makedirs(root, exist_ok=True)

    # Single-instance lock: overlapping ticks exit quietly (not an error).
    lock = open(os.path.join(root, "worker.lock"), "w", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("taste-distiller: another instance holds the lock; exiting")
            return 0
        return _drain(root, force_session=args.session)
    finally:
        lock.close()


def _is_quiet(entry, quiet_secs):
    """Distill only once the transcript has stopped growing (session over)."""
    path = entry.get("transcript_path", "")
    try:
        age = datetime.datetime.now().timestamp() - os.path.getmtime(path)
    except OSError:
        return True  # missing transcript: let process_session ledger it
    return age >= quiet_secs


def _retryable(record):
    return (record.get("status") == "error"
            and record.get("attempts", 1) < RETRY_LIMIT)


def _drain(root, force_session=None):
    quiet_secs = int(os.environ.get("MACROSEAT_QUIET_SECONDS",
                                    str(DEFAULT_QUIET_SECONDS)))
    entries, bad = read_queue(queue_path(root))
    ledger = load_ledger(ledger_path(root))
    registry = load_registry(registry_path(root))

    pending, waiting, seen = [], 0, set()
    for entry in entries:
        sid = entry["session_id"]
        if sid in seen:
            continue
        seen.add(sid)
        if force_session is not None:
            if sid == force_session:
                pending.append(entry)
            continue
        if sid in ledger and not _retryable(ledger[sid]):
            continue
        if not _is_quiet(entry, quiet_secs):
            waiting += 1  # stays queued, unledgered; a later tick retries
            continue
        pending.append(entry)

    batch = pending[:MAX_SESSIONS]
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print("taste-distiller: queue=%d bad=%d quiet-wait=%d pending=%d batch=%d registry=%d"
          % (len(entries), bad, waiting, len(pending), len(batch), len(registry)))

    for entry in batch:
        record = process_session(entry, registry, now_iso)
        prior = ledger.get(entry["session_id"], {})
        if record["status"] == "error":
            record["attempts"] = prior.get("attempts", 0) + 1
        ledger[entry["session_id"]] = record
        save_ledger(ledger_path(root), ledger)  # per-session: a kill loses nothing
        print("  %s -> %s (%s specimens)"
              % (entry["session_id"][:8], record["status"],
                 record.get("specimens", "-")), file=sys.stderr)

    remaining = len(pending) - len(batch)
    if remaining:
        print("taste-distiller: %d session(s) remain; next tick continues" % remaining)
    return 0


if __name__ == "__main__":
    sys.exit(main())
