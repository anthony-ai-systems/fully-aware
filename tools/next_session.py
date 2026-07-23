#!/usr/bin/env python3
"""next_session.py -- unified NEXT_SESSION.json parser (next-session/v2).

Normalizes every NEXT_SESSION.json schema variant found on this machine into a
single ``next-session/v2`` shape so that Iris and mission-control consume state
through one reader instead of five. Read-only by construction: source files are
never mutated. Stdlib only, Python 3.9+, no third-party deps.

Schemas handled
---------------
  A         SAGA run-bundle (bundle-law; normalized read-only, never rewritten).
            Keys: status, reason, id, type, run_id, phase, date, operator,
            summary, next_action (str), commands_run[], procedures_abided[],
            issues_discovered[], self_heal, cites_precedent[],
            supersedes/deprecates, optional branch/worktree/expected_head/
            expected_head_mode/prompt_file. Also the schema-tagged D38 variant
            (schema == "saga-next-session/...", fields under git_preconditions).
  B         optimus "next-session/v1": schema, written_at, session, state (obj),
            landmines[] OR traps[], sometimes next_action {who,what,then}.
  D         console handoff: version (int), written, read_first[], state (prose),
            next[], anthony_solo[], evidence{}, gotchas[], live_worktrees[].
  E         marketing-os Codex packet handoff: written, by, next_session_role,
            worktree, branch, base, local_commit, pull_request{}, read_in_order[],
            deliverables/completed[], gates/gates_passed/verification[],
            warnings/prerequisites/next_actions[]. These files are frequently
            INVALID strict JSON -- Codex appended packet objects without array
            brackets, sometimes merging a new record into the prior object's
            unclosed array. The E adapter does a lenient multi-object / salvage
            decode and normalizes the leading (most recent) record.
  v2        next-session/v2 -- passthrough + validation.
  C         heartbeat test fixture -- EXCLUDED by path rule
            (*/tests/fixtures/heartbeat-bundle/NEXT_SESSION.json).
  fallback  free-form handoff schemas -- best-effort extraction; every
            unmapped source field is preserved verbatim under ``legacy``.

CLI
---
    python3 next_session.py scan <root> [--jsonl]

Walks <root> (skipping node_modules and .git), classifies + normalizes every
NEXT_SESSION.json, and emits one record per file. A per-schema summary
(including the unparseable count) is written to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from json import JSONDecoder, JSONDecodeError
from typing import Any, Dict, List, Optional, Tuple

V2_SCHEMA = "next-session/v2"
V2_STATUS = ("in-progress", "blocked", "parked", "done")

# Path rule: heartbeat test fixtures are excluded from normalization entirely.
_C_EXCLUDE_RE = re.compile(r"/tests/fixtures/heartbeat-bundle/NEXT_SESSION\.json$")

_SKIP_DIRS = {"node_modules", ".git"}


# --------------------------------------------------------------------------- #
# Small value helpers
# --------------------------------------------------------------------------- #
def _s(v: Any) -> str:
    """Coerce a scalar-ish value to a stripped string ("" for None)."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _as_list(v: Any) -> List[Any]:
    """Coerce a value to a list; wrap scalars, drop None, expand dict values."""
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if x is not None]
    if isinstance(v, dict):
        out: List[Any] = []
        for k, val in v.items():
            out.append(f"{k}: {_s(val)}" if not isinstance(val, (list, dict)) else f"{k}: {_s(val)}")
        return out
    return [v]


def _strlist(v: Any) -> List[str]:
    return [_s(x) for x in _as_list(v) if _s(x)]


def _first_nonempty(*vals: Any) -> str:
    for v in vals:
        s = _s(v)
        if s:
            return s
    return ""


def _norm_status(*candidates: Any) -> Optional[str]:
    """Best-effort map of arbitrary status prose to one of V2_STATUS.

    Returns None when undeterminable (caller defaults to "parked").
    """
    text = " ".join(_s(c).lower() for c in candidates if _s(c))
    if not text.strip():
        return None
    # Priority order: an explicit "done" marker wins, then blocked, then
    # in-progress, then parked. "not merged" must not read as done.
    done = ("done", "run_closed", "run closed", "run complete", "closed",
            "complete", "shipped", "terminal", "landed")
    if "not merged" not in text and ("merged" in text or any(w in text for w in done)):
        return "done"
    if any(w in text for w in ("block", "gated", "gate on", "awaiting", "waiting",
                               "stuck", "halt", "pending anthony", "blocked_on")):
        return "blocked"
    if any(w in text for w in ("in-progress", "in progress", "wip", "active",
                               "live", "open", "building", "running")):
        return "in-progress"
    if any(w in text for w in ("parked", "paused", "hold", "deferred", "idle")):
        return "parked"
    return None


def _consume(src: Dict[str, Any], *keys: str) -> None:
    for k in keys:
        src.pop(k, None)


def _legacy(src: Dict[str, Any]) -> Dict[str, Any]:
    """Whatever remains in ``src`` after an adapter pops what it mapped."""
    return {k: v for k, v in src.items() if v is not None}


# --------------------------------------------------------------------------- #
# Lenient / salvage JSON decoding for Schema E (and any corrupt file)
# --------------------------------------------------------------------------- #
_KEY_RE = re.compile(r'"((?:[^"\\]|\\.)*)"\s*:')


def _raw_decode_loop(raw: str) -> Tuple[List[Any], bool]:
    """Decode one-or-more whitespace/comma-separated top-level JSON values.

    Returns (objects, clean) where ``clean`` is True when the entire string was
    consumed with no leftover. Handles the "valid single object" case and the
    "cleanly concatenated objects" case.
    """
    dec = JSONDecoder()
    i, n = 0, len(raw)
    objs: List[Any] = []
    while i < n:
        while i < n and raw[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        try:
            obj, end = dec.raw_decode(raw, i)
        except JSONDecodeError:
            return objs, False
        objs.append(obj)
        i = end
    return objs, True


def _salvage_records(raw: str) -> List[Dict[str, Any]]:
    """Recover one-or-more flat records from corrupt concatenated JSON.

    Codex-appended E files often merge a new packet into the previous object's
    unclosed array, so a plain raw_decode loop recovers nothing. This scraper
    walks top-down decoding ``"key": <value>`` pairs; a repeated key signals a
    new record boundary. Values that cannot be decoded (e.g. an unterminated
    array) are skipped rather than aborting the record.
    """
    records: List[Dict[str, Any]] = []
    dec = JSONDecoder()
    i, n = 0, len(raw)
    cur: Dict[str, Any] = {}
    while i < n:
        m = _KEY_RE.search(raw, i)
        if not m:
            break
        try:
            key = json.loads('"' + m.group(1) + '"')
        except JSONDecodeError:
            key = m.group(1)
        vi = m.end()
        while vi < n and raw[vi] in " \t\r\n":
            vi += 1
        if vi >= n:
            break
        try:
            val, end = dec.raw_decode(raw, vi)
        except JSONDecodeError:
            # Undecodable value (unterminated array/object). Skip past the key
            # and keep scanning for the next recoverable pair.
            i = m.end()
            continue
        if key in cur:
            # Boundary: a repeated key means a new record began.
            records.append(cur)
            cur = {}
        cur[key] = val
        i = end
    if cur:
        records.append(cur)
    return records


def _lenient_parse(raw: str) -> Tuple[List[Dict[str, Any]], str]:
    """Return (records, mode) for a possibly-invalid file.

    mode is one of: "single" (one valid object), "concatenated" (clean multi),
    "salvaged" (recovered from corrupt text), or "" (nothing recovered).
    """
    objs, clean = _raw_decode_loop(raw)
    dict_objs = [o for o in objs if isinstance(o, dict)]
    if clean and dict_objs:
        return dict_objs, ("single" if len(dict_objs) == 1 else "concatenated")
    salvaged = _salvage_records(raw)
    if salvaged:
        return salvaged, "salvaged"
    if dict_objs:  # partial clean prefix before a break
        return dict_objs, "salvaged"
    return [], ""


# --------------------------------------------------------------------------- #
# v2 assembly
# --------------------------------------------------------------------------- #
def _v2(
    *,
    written_at: str,
    environment: str,
    status: Optional[str],
    summary: str,
    who: str,
    what: str,
    then: str = "",
    branch: str = "",
    worktree: str = "",
    expected_head: str = "",
    human_only: Optional[List[str]] = None,
    pitfalls: Optional[List[str]] = None,
    read_first: Optional[List[str]] = None,
    evidence: Optional[Dict[str, Any]] = None,
    legacy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a validated next-session/v2 object with required + optional fields."""
    next_action: Dict[str, Any] = {"who": who, "what": what}
    if then:
        next_action["then"] = then
    out: Dict[str, Any] = {
        "schema": V2_SCHEMA,
        "written_at": written_at,
        "environment": environment,
        "status": status if status in V2_STATUS else "parked",
        "summary": summary,
        "next_action": next_action,
    }
    if branch:
        out["branch"] = branch
    if worktree:
        out["worktree"] = worktree
    if expected_head:
        out["expected_head"] = expected_head
    if human_only:
        out["human_only"] = [x for x in human_only if x]
    if pitfalls:
        out["pitfalls"] = [x for x in pitfalls if x]
    if read_first:
        out["read_first"] = [x for x in read_first if x]
    if evidence:
        out["evidence"] = evidence
    if legacy:
        out["legacy"] = legacy
    return out


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #
def adapt_A(data: Dict[str, Any], environment: str) -> Dict[str, Any]:
    """SAGA run-bundle -> v2. Read-only normalization; bundle stays canonical."""
    src = dict(data)
    # The D38 variant nests git facts under git_preconditions (sometimes prose).
    gp = src.get("git_preconditions")
    gp = gp if isinstance(gp, dict) else {}
    branch = _first_nonempty(src.get("branch"), gp.get("branch"))
    worktree = _first_nonempty(src.get("worktree"), gp.get("worktree"))
    expected_head = _first_nonempty(src.get("expected_head"), gp.get("expected_head"))
    env = _first_nonempty(environment, gp.get("repo"), src.get("run_id"))

    evidence: Dict[str, Any] = {}
    if _strlist(src.get("commands_run")):
        evidence["commands_run"] = _strlist(src.get("commands_run"))

    v2 = _v2(
        written_at=_first_nonempty(src.get("date"), src.get("last_updated")),
        environment=env,
        status=_norm_status(src.get("status"), src.get("phase"), src.get("reason")),
        summary=_first_nonempty(src.get("summary"), src.get("reason")),
        who=_s(src.get("operator")),
        what=_s(src.get("next_action")),
        branch=branch,
        worktree=worktree,
        expected_head=expected_head,
        pitfalls=_strlist(src.get("issues_discovered")),
        read_first=_strlist(src.get("cites_precedent")),
        evidence=evidence or None,
    )
    _consume(src, "date", "last_updated", "status", "phase", "reason", "summary",
             "operator", "next_action", "branch", "worktree", "expected_head",
             "issues_discovered", "cites_precedent", "commands_run",
             "git_preconditions", "schema")
    legacy = _legacy(src)
    if legacy:
        v2["legacy"] = legacy
    return v2


def adapt_B(data: Dict[str, Any], environment: str) -> Dict[str, Any]:
    """optimus next-session/v1 -> v2."""
    src = dict(data)
    state = src.get("state")
    na = src.get("next_action") if isinstance(src.get("next_action"), dict) else {}

    what = ""
    then = ""
    who = ""
    if na:
        what = _s(na.get("what"))
        then = _s(na.get("then"))
        who = _s(na.get("who"))
    if not what and isinstance(state, dict):
        what = _s(state.get("next_in_order"))
    if not what and not isinstance(state, dict):
        what = _s(state)

    # pitfalls: landmines OR traps
    pitfalls = _strlist(src.get("landmines")) + _strlist(src.get("traps"))

    # status from the state object/prose. A non-empty ``blocked_on_anthony``
    # key is itself a blocked signal (a stronger "done" cue in the values,
    # e.g. "merged", still wins via _norm_status priority).
    if isinstance(state, dict):
        blocked_signal = "blocked" if _s(state.get("blocked_on_anthony")) else ""
        status = _norm_status(blocked_signal, *state.values())
        summary = _first_nonempty(src.get("session"), json.dumps(state, ensure_ascii=False))
    else:
        status = _norm_status(state)
        summary = _first_nonempty(src.get("session"), _s(state))

    v2 = _v2(
        written_at=_s(src.get("written_at")),
        environment=environment,
        status=status,
        summary=summary,
        who=who,
        what=what,
        then=then,
        branch=_s(src.get("branch")),
        worktree=_s(src.get("worktree")),
        expected_head=_s(src.get("expected_head")),
        pitfalls=pitfalls,
        read_first=_strlist(src.get("read_first")),
    )
    _consume(src, "schema", "written_at", "session", "state", "next_action",
             "landmines", "traps", "branch", "worktree", "expected_head",
             "read_first")
    legacy = _legacy(src)
    if legacy:
        v2["legacy"] = legacy
    return v2


def adapt_D(data: Dict[str, Any], environment: str) -> Dict[str, Any]:
    """console handoff -> v2."""
    src = dict(data)
    nxt = _strlist(src.get("next"))
    v2 = _v2(
        written_at=_s(src.get("written")),
        environment=environment,
        status=_norm_status(src.get("state")),
        summary=_s(src.get("state")),
        who="",
        what=(nxt[0] if nxt else ""),
        then=(" | ".join(nxt[1:]) if len(nxt) > 1 else ""),
        human_only=_strlist(src.get("anthony_solo")),
        pitfalls=_strlist(src.get("gotchas")),
        read_first=_strlist(src.get("read_first")),
        evidence=(src.get("evidence") if isinstance(src.get("evidence"), dict) else None),
    )
    _consume(src, "written", "state", "next", "anthony_solo", "gotchas",
             "read_first", "evidence")
    legacy = _legacy(src)
    if legacy:
        v2["legacy"] = legacy
    return v2


def adapt_E(records: List[Dict[str, Any]], environment: str,
            recovered: bool) -> Dict[str, Any]:
    """marketing-os Codex packet handoff -> v2 (leading/most-recent record)."""
    data = records[0] if records else {}
    src = dict(data)

    next_actions = _strlist(src.get("next_actions"))
    gates = _strlist(src.get("gates"))
    what_parts = next_actions or [_s(src.get("next_session_role"))]
    what = " | ".join([p for p in what_parts if p])
    then = " | ".join(gates) if gates else ""

    pr = src.get("pull_request") if isinstance(src.get("pull_request"), dict) else {}
    status = _norm_status(pr.get("state"), pr.get("mergeStateStatus"),
                          src.get("next_session_role"))

    summary = _first_nonempty(
        "; ".join(_strlist(src.get("deliverables")) + _strlist(src.get("completed"))),
        src.get("next_session_role"),
        src.get("state_of_record"),
    )

    evidence: Dict[str, Any] = {}
    gp = _strlist(src.get("gates_passed"))
    ver = _strlist(src.get("verification"))
    if gp:
        evidence["gates_passed"] = gp
    if ver:
        evidence["verification"] = ver
    if isinstance(pr, dict) and pr:
        evidence["pull_request"] = pr

    v2 = _v2(
        written_at=_s(src.get("written")),
        environment=environment,
        status=status,
        summary=summary,
        who=_s(src.get("by")),
        what=what,
        then=then,
        branch=_s(src.get("branch")),
        worktree=_s(src.get("worktree")),
        expected_head=_first_nonempty(src.get("expected_head"), src.get("local_commit")),
        pitfalls=_strlist(src.get("warnings")) + _strlist(src.get("prerequisites")),
        read_first=_strlist(src.get("read_in_order")),
        evidence=evidence or None,
    )
    _consume(src, "written", "by", "next_session_role", "worktree", "branch",
             "base", "local_commit", "expected_head", "pull_request",
             "read_in_order", "deliverables", "completed", "gates",
             "gates_passed", "verification", "warnings", "prerequisites",
             "next_actions")
    legacy = _legacy(src)
    if len(records) > 1:
        legacy["_recovered_record_count"] = len(records)
    if recovered:
        legacy["_recovered_from_invalid_json"] = True
    if legacy:
        v2["legacy"] = legacy
    return v2


def adapt_v2(data: Dict[str, Any], environment: str) -> Dict[str, Any]:
    """Passthrough + validation for an already-v2 object."""
    out = dict(data)
    out["schema"] = V2_SCHEMA
    if not _s(out.get("environment")):
        out["environment"] = environment
    if out.get("status") not in V2_STATUS:
        out["status"] = _norm_status(out.get("status")) or "parked"
    na = out.get("next_action")
    if not isinstance(na, dict):
        out["next_action"] = {"who": "", "what": _s(na)}
    else:
        na.setdefault("who", "")
        na.setdefault("what", "")
    out.setdefault("written_at", "")
    out.setdefault("summary", "")
    return out


def adapt_fallback(data: Dict[str, Any], environment: str) -> Dict[str, Any]:
    """Free-form handoff schemas -> v2, best-effort; unknowns -> legacy."""
    src = dict(data)
    # Probe a range of observed free-form field names.
    what = _first_nonempty(
        src.get("next_action_for_agent"), src.get("next_action"),
        src.get("launch_one_liner"),
        "; ".join(_strlist(src.get("next"))),
        "; ".join(_strlist(src.get("next_actions"))),
    )
    summary = _first_nonempty(
        src.get("summary"), src.get("state"), src.get("reason"),
        src.get("project"), src.get("lane"),
    )
    status = _norm_status(src.get("status"), src.get("state"), src.get("lane"),
                          src.get("reason"))
    human_only = (_strlist(src.get("anthony_solo"))
                  + _strlist(src.get("anthony_decisions_pending")))
    pitfalls = (_strlist(src.get("gotchas")) + _strlist(src.get("landmines"))
                + _strlist(src.get("traps")) + _strlist(src.get("warnings"))
                + _strlist(src.get("issues_discovered"))
                + _strlist(src.get("constraints")) + _strlist(src.get("guardrails")))
    read_first = (_strlist(src.get("read_first")) + _strlist(src.get("read_in_order"))
                  + _strlist(src.get("read_first_order")))
    evidence = src.get("evidence") if isinstance(src.get("evidence"), dict) else None

    v2 = _v2(
        written_at=_first_nonempty(src.get("written"), src.get("written_at"),
                                   src.get("date"), src.get("session_date")),
        environment=_first_nonempty(environment, src.get("project"), src.get("cwd")),
        status=status,
        summary=summary,
        who=_first_nonempty(src.get("operator"), src.get("by")),
        what=what,
        branch=_s(src.get("branch")),
        worktree=_s(src.get("worktree")),
        expected_head=_s(src.get("expected_head")),
        human_only=human_only,
        pitfalls=pitfalls,
        read_first=read_first,
        evidence=evidence,
    )
    _consume(src, "next_action_for_agent", "next_action", "launch_one_liner",
             "next", "next_actions", "summary", "state", "reason", "project",
             "lane", "status", "anthony_solo", "anthony_decisions_pending",
             "gotchas", "landmines", "traps", "warnings", "issues_discovered",
             "constraints", "guardrails", "read_first", "read_in_order",
             "read_first_order", "evidence", "written", "written_at", "date",
             "session_date", "operator", "by", "branch", "worktree",
             "expected_head", "cwd")
    legacy = _legacy(src)
    if legacy:
        v2["legacy"] = legacy
    return v2


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _classify_dict(d: Dict[str, Any]) -> str:
    """Return a schema tag for an already-parsed dict record."""
    schema = _s(d.get("schema"))
    if schema == V2_SCHEMA:
        return "v2"
    if schema == "next-session/v1":
        return "B"
    if schema.startswith("saga-next-session"):
        return "A"
    keys = set(d.keys())
    # A: run-bundle key signature.
    if ({"run_id", "type", "phase"} & keys and "next_action" in keys) \
            or {"expected_head_mode", "self_heal"} & keys \
            or ("operator" in keys and "commands_run" in keys):
        return "A"
    # D: console handoff signature (numeric version + anthony_solo/live_worktrees).
    if ("anthony_solo" in keys or "live_worktrees" in keys) and "state" in keys:
        return "D"
    if isinstance(d.get("version"), int) and "read_first" in keys and "next" in keys:
        return "D"
    # E: Codex packet signature.
    if "next_session_role" in keys or "read_in_order" in keys \
            or ("written" in keys and "by" in keys):
        return "E"
    # B without schema tag (unlikely) -- session + state + traps/landmines.
    if "session" in keys and ("traps" in keys or "landmines" in keys):
        return "B"
    return "fallback"


def normalize(data: Any, environment: str, detected_schema: str,
              records: Optional[List[Dict[str, Any]]] = None,
              recovered: bool = False) -> Dict[str, Any]:
    """Dispatch a parsed record (or E record list) to the right adapter."""
    if detected_schema == "A":
        return adapt_A(data, environment)
    if detected_schema == "B":
        return adapt_B(data, environment)
    if detected_schema == "D":
        return adapt_D(data, environment)
    if detected_schema == "E":
        return adapt_E(records if records is not None else [data], environment, recovered)
    if detected_schema == "v2":
        return adapt_v2(data, environment)
    return adapt_fallback(data, environment)


# --------------------------------------------------------------------------- #
# Environment inference
# --------------------------------------------------------------------------- #
def infer_environment(path: str) -> str:
    """Best-effort environment id: nearest ancestor git repo root's basename,
    else the file's parent directory name."""
    d = os.path.dirname(os.path.abspath(path))
    cur = d
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return os.path.basename(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.basename(d)


# --------------------------------------------------------------------------- #
# Per-file entry point
# --------------------------------------------------------------------------- #
def normalize_file(path: str) -> Dict[str, Any]:
    """Classify + normalize a single NEXT_SESSION.json.

    Never raises for content reasons -- returns either
      {source_path, detected_schema, normalized{...}} or
      {source_path, detected_schema, unparseable: True, error} or
      {source_path, detected_schema: "C", excluded: True}.
    """
    apath = os.path.abspath(path)
    if _C_EXCLUDE_RE.search(apath.replace(os.sep, "/")):
        return {"source_path": apath, "detected_schema": "C", "excluded": True}

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError) as exc:  # unreadable file
        return {"source_path": apath, "detected_schema": "unknown",
                "unparseable": True, "error": f"{type(exc).__name__}: {exc}"}

    environment = infer_environment(path)

    # Fast path: strict single JSON object.
    try:
        parsed = json.loads(raw)
    except JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        schema = _classify_dict(parsed)
        norm = normalize(parsed, environment, schema)
        return {"source_path": apath, "detected_schema": schema, "normalized": norm}

    if isinstance(parsed, list):
        dicts = [x for x in parsed if isinstance(x, dict)]
        if dicts:
            schema = _classify_dict(dicts[0])
            if schema == "E":
                norm = normalize(dicts[0], environment, "E", records=dicts)
            else:
                norm = normalize(dicts[0], environment, schema)
            return {"source_path": apath, "detected_schema": schema, "normalized": norm}
        return {"source_path": apath, "detected_schema": "unknown",
                "unparseable": True, "error": "empty or non-object JSON array"}

    # Invalid strict JSON: lenient / salvage recovery (Schema E and friends).
    records, mode = _lenient_parse(raw)
    if not records:
        return {"source_path": apath, "detected_schema": "unknown",
                "unparseable": True,
                "error": f"unrecoverable JSON (lenient mode={mode or 'none'})"}
    schema = _classify_dict(records[0])
    recovered = mode == "salvaged"
    if schema == "E":
        norm = normalize(records[0], environment, "E", records=records,
                         recovered=recovered)
    else:
        norm = normalize(records[0], environment, schema)
        if recovered and isinstance(norm, dict):
            norm.setdefault("legacy", {})["_recovered_from_invalid_json"] = True
    tag = schema if schema != "fallback" else "fallback"
    return {"source_path": apath, "detected_schema": tag, "normalized": norm}


# --------------------------------------------------------------------------- #
# scan CLI
# --------------------------------------------------------------------------- #
def iter_next_session_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if "NEXT_SESSION.json" in filenames:
            yield os.path.join(dirpath, "NEXT_SESSION.json")


def scan(root: str, jsonl: bool = False, out=sys.stdout, err=sys.stderr) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    unparseable = 0
    records: List[Dict[str, Any]] = []
    for path in sorted(iter_next_session_files(root)):
        try:
            rec = normalize_file(path)
        except Exception as exc:  # last-resort guard: never abort the scan
            rec = {"source_path": os.path.abspath(path),
                   "detected_schema": "unknown", "unparseable": True,
                   "error": f"UNCAUGHT {type(exc).__name__}: {exc}"}
        schema = rec.get("detected_schema", "unknown")
        counts[schema] = counts.get(schema, 0) + 1
        if rec.get("unparseable"):
            unparseable += 1
        if jsonl:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        else:
            records.append(rec)
    if not jsonl:
        json.dump(records, out, ensure_ascii=False, indent=2)
        out.write("\n")

    total = sum(counts.values())
    err.write("\n=== next_session scan summary ===\n")
    err.write(f"root: {os.path.abspath(root)}\n")
    err.write(f"total files: {total}\n")
    for schema in sorted(counts):
        err.write(f"  {schema:>10}: {counts[schema]}\n")
    err.write(f"unparseable: {unparseable}\n")
    return {"total": total, "unparseable": unparseable, **counts}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="next_session.py",
        description="Normalize NEXT_SESSION.json files into next-session/v2.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("scan", help="walk a root and normalize every NEXT_SESSION.json")
    sp.add_argument("root", help="directory to walk")
    sp.add_argument("--jsonl", action="store_true",
                    help="emit newline-delimited JSON instead of a JSON array")
    args = parser.parse_args(argv)
    if args.cmd == "scan":
        scan(args.root, jsonl=args.jsonl)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
