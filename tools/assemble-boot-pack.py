#!/usr/bin/env python3
"""assemble-boot-pack.py -- D30-class boot-pack assembler (Fully Aware Artifact 3).

Folds five sections into ``state/BOOT-PACK.md`` (human render) plus
``state/boot-pack.json`` (machine sidecar), the single primary artifact a fresh
Fully Aware macro session loads regardless of cwd:

  0. Defects              -- the defect register's status (defect-status/v1,
                             produced by verify-defects.py from
                             registers/defects.json). Rendered FIRST, capped at
                             a dozen lines: one summary line, then Anthony's
                             items for today, then the oldest open P1s to fill.
  1. Topology manifest   -- hand-maintained seed (provenance: manual) until P21.
  2. State surfaces       -- the fold of every discoverable surface (surface/v1,
                             produced by generate-surface.py; normally state/surfaces/).
  3. Unified decision queue -- a PROJECTION (routes, never absorbs ratification)
                             over five feeds: surface decisions[], next-session
                             human_only[] (via the M1 next_session.py parser),
                             a hand-maintained ratification backlog, the
                             atlas-v2 adjudication queues (checkbox files the
                             nightly ripple sweep writes into the vault), and
                             the weekly Atlas DECAY review queue.
  4. Scan / priorities feed -- consumes scan-consumption-interface-v1 artifacts
                             (weights / scan-targets / suppression / optional
                             intentions) from a configurable --scan-consumption-dir.

Class D30 (read-only, stateless, non-enforcing, report-only, MANUALLY invoked --
never wired into CI/hooks/gates). Stdlib only, Python 3.9+. NO git operations, NO
network. The ONLY files it writes are its own state/ outputs (both gitignored);
it refuses to write a non-gitignored path.

The pack is ADVISORY STATE, NOT LAW: SAGA doctrine, repo CLAUDE.md, and
merge-is-Anthony's bind regardless of pack content.

Provenance: every entry is tagged ``[source | as_of]``. Degraded sources
(missing/invalid surface, invalid/absent scan artifact) surface a WARNING block
at the top of the pack -- never a silent omission, never a crash.

Staleness thresholds (spec SS3.1): topology 7d, surfaces 24h, daily decision
items 1h, and the Monday DECAY review 7d.
Stale entries render a ``STALE(<age>)`` prefix -- never dropped, never silently
trusted.

Hard cap: 50000 tokens (chars/4 estimate). Overflow truncates lowest-priority
content first (per-repo next_lanes in section 2) with explicit
``TRUNCATED: n entries`` markers -- never a silent cap.

Determinism (spec SS3.1): deterministic ordering everywhere (manifest order for
topology + surfaces, a stable sort for the decision queue, fixed artifact order
for scan). Wall-clock appears ONLY inside ``as_of`` fields, which are carved
before any determinism/diff comparison; ``build_pack`` takes an injectable
``now`` so two runs against fixed inputs are byte-identical after carving.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

# Import the M1 next-session parser from the same directory (data-contract
# consumption; no cross-repo import).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import next_session as ns  # noqa: E402

PACK_SCHEMA = "boot-pack/v1"
MANIFEST_SCHEMA = "seed-manifest/v1"
BACKLOG_SCHEMA = "ratification-backlog/v1"
SURFACE_SCHEMA = "surface/v1"
DEFECT_STATUS_SCHEMA = "defect-status/v1"

HARD_CAP_TOKENS = 50000

# Section 0 (defects) is the first thing a session reads, so it is deliberately
# tiny: one summary line plus a handful of bullets, hard-capped by line count so
# a growing register can never eat the pack's budget.
DEFECTS_MAX_BULLETS = 8
DEFECTS_SECTION_MAX_LINES = 12
DEFECT_BULLET_CHARS = 120

# Per-repo next-session line: one line, hard-truncated (token-budget discipline).
NEXT_SESSION_SUMMARY_CHARS = 200

# Staleness thresholds (seconds).
STALE_TOPOLOGY = 7 * 24 * 3600
STALE_SURFACES = 24 * 3600
STALE_DECISIONS = 1 * 3600
# Atlas DECAY is a weekly Monday feed. Its run stays visible after this
# threshold (with a stale marker); age never removes unresolved review work.
STALE_DECAY = 7 * 24 * 3600
DECAY_CADENCE = "weekly (Monday)"
# ledger.py rewrites the plans snapshot on every register/append/regen; a
# snapshot older than a day-and-a-half means no session touched any lane.
STALE_PLANS = 36 * 3600
# The defect loop runs inside the same 05:45 wrapper as this assembler, so a
# status file older than a day-and-a-half means that step did not run.
STALE_DEFECTS = 36 * 3600
# ...but 36 hours is far too patient on its own. The register step runs seconds
# before this assembler in the same wrapper, so a status file more than an hour
# behind the pack it is folded into is already a step that failed or never ran.
# Below STALE_DEFECTS it is not "stale" yet, so it is labelled with its own age
# instead: old counts must never read as this morning's counts.
DEFECT_LAG = 3600

# Scan-consumption-interface-v1 required + optional artifacts (fixed order).
SCAN_ARTIFACTS = [
    ("weights.json", "saga-scan/doctrine-weights", True),
    ("scan-targets.json", "saga-scan/staleness-targets", True),
    ("suppression.json", "saga-scan/reproposal-suppression", True),
    ("intentions.json", "saga-scan/intentions", False),
]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_ATLAS_PENDING_DIR = os.path.expanduser(
    "~/code/anthony-wiki-vault/adjudication/pending")


class AssembleError(Exception):
    """A hard assembly failure (nonzero exit; not a degrade)."""


# --------------------------------------------------------------------------- #
# time helpers -- wall-clock lives ONLY in as_of fields
# --------------------------------------------------------------------------- #
# A leading ISO date, optionally followed by a time and offset. Anchored so we
# only ever lift a *prefix* out of free-text as_of strings (e.g. the SMC form
# "2026-07-23T07:30 local (approx, overnight ...)" yields "2026-07-23T07:30").
_ISO_PREFIX_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?")

# strptime fallbacks for tokens fromisoformat rejects on 3.9 (e.g. HH:MM only).
_ISO_STRPTIME_FMTS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _iso_token_to_utc(token):
    """Parse one clean ISO date/datetime token to aware UTC, or None."""
    token = token.strip()
    if not token:
        return None
    dt = None
    try:
        dt = datetime.datetime.fromisoformat(token.replace("Z", "+00:00"))
    except ValueError:
        for fmt in _ISO_STRPTIME_FMTS:
            try:
                dt = datetime.datetime.strptime(token, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def parse_ts(value):
    """Parse an ISO-ish timestamp to an aware UTC datetime, or None.

    Accepts date-only (YYYY-MM-DD), full ISO with/without offset, and a trailing
    'Z'. Naive values are assumed UTC. When the whole value is not itself a clean
    timestamp, a leading ISO date/datetime PREFIX is extracted from free text
    (source NEXT_SESSION as_of fields carry prose like
    "2026-07-23T07:30 local (approx, overnight 2026-07-23 session)"). Never raises.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    dt = _iso_token_to_utc(s)
    if dt is not None:
        return dt
    m = _ISO_PREFIX_RE.match(s)
    if m:
        return _iso_token_to_utc(m.group(0))
    return None


def humanize_age(delta):
    """A compact, deterministic age string: 3d / 5h / 12m / 8s."""
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs >= 86400:
        return "%dd" % (secs // 86400)
    if secs >= 3600:
        return "%dh" % (secs // 3600)
    if secs >= 60:
        return "%dm" % (secs // 60)
    return "%ds" % secs


def stale_prefix(now, as_of_str, threshold_secs):
    """Return 'STALE(<age>) ' when as_of is older than threshold, else ''.

    An as_of with no extractable date renders 'AS_OF-UNPARSEABLE ' (fail-visible)
    -- distinct from STALE, which means known-but-old.
    """
    ts = parse_ts(as_of_str)
    if ts is None:
        return "AS_OF-UNPARSEABLE "
    age = now - ts
    if age.total_seconds() > threshold_secs:
        return "STALE(%s) " % humanize_age(age)
    return ""


def tag(source, as_of):
    """The universal [source | as_of] provenance tag."""
    return "[%s | %s]" % (source or "?", as_of or "?")


# --------------------------------------------------------------------------- #
# config / input loading
# --------------------------------------------------------------------------- #
def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_manifest(path):
    try:
        data = _load_json(path)
    except (OSError, ValueError) as exc:
        raise AssembleError("cannot load manifest %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise AssembleError("manifest %s is not a JSON object" % path)
    if data.get("schema") != MANIFEST_SCHEMA:
        raise AssembleError(
            "manifest %s has unexpected schema %r (want %r)"
            % (path, data.get("schema"), MANIFEST_SCHEMA))
    if not isinstance(data.get("repos"), list):
        raise AssembleError("manifest %s missing repos[]" % path)
    return data


def load_backlog(path):
    """Ratification backlog is OPTIONAL and degrades to empty on any problem."""
    if not path or not os.path.isfile(path):
        return {"present": False, "items": [], "as_of": "", "reason":
                "no ratification-backlog.json at %s" % path}
    try:
        data = _load_json(path)
    except (OSError, ValueError) as exc:
        return {"present": False, "items": [], "as_of": "",
                "reason": "invalid ratification-backlog.json: %s" % exc}
    if not isinstance(data, dict) or data.get("schema") != BACKLOG_SCHEMA:
        return {"present": False, "items": [], "as_of": "",
                "reason": "ratification-backlog schema mismatch"}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return {"present": True, "items": items, "as_of": data.get("as_of", ""),
            "source": os.path.relpath(path, _REPO_ROOT)}


# Atlas-v2 adjudication queue prefixes -> plain queue names. The nightly ripple
# sweep re-derives its FULL queue each run, so only the lexically last file per
# prefix is current (ISO-dated filenames sort correctly); older ones are
# superseded, never additive.
ADJUDICATION_PREFIXES = (
    ("AUTO-APPLY-", "mechanical auto-apply queue"),
    ("BACKLOG-", "review backlog"),
    ("RIPPLE-", "ripple review"),
)

# An actioned checkbox: "- [x]" / "- [X]" (findings carry APPLY/REJECT/DEFER
# boxes; any checked box counts as actioned).
_ADJUDICATION_ACTIONED_RE = re.compile(r"^\s*-\s*\[[xX]\]")


def load_adjudication(pending_dir):
    """Atlas-v2 adjudication queues are OPTIONAL and degrade to empty.

    ``pending_dir=None`` means the feed is not configured: silently absent (no
    warning -- mirrors an unconfigured scan dir). A configured-but-missing dir
    returns ``present: False`` with a reason so collect() can warn. For each
    queue prefix only the newest ``<prefix>*.md`` is read; findings are the
    ``### `` heading lines, actioned boxes are checked checkboxes, and as_of is
    the ISO date carved from the filename. READ-ONLY: the vault is never
    written.
    """
    if not pending_dir:
        return {"present": False, "queues": [], "reason": None}
    if not os.path.isdir(pending_dir):
        return {"present": False, "queues": [],
                "reason": "adjudication pending dir does not exist: %s"
                % pending_dir}
    try:
        names = os.listdir(pending_dir)
    except OSError as exc:
        return {"present": False, "queues": [],
                "reason": "unreadable adjudication pending dir: %s" % exc}
    queues = []
    for prefix, queue_name in ADJUDICATION_PREFIXES:
        matches = sorted(n for n in names
                         if n.startswith(prefix) and n.endswith(".md"))
        if not matches:
            continue
        fname = matches[-1]
        findings = 0
        actioned = 0
        try:
            with open(os.path.join(pending_dir, fname), "r",
                      encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("### "):
                        findings += 1
                    elif _ADJUDICATION_ACTIONED_RE.match(line):
                        actioned += 1
        except OSError as exc:
            queues.append({"file": fname, "queue": queue_name,
                           "degraded": True, "reason": str(exc)})
            continue
        queues.append({"file": fname, "queue": queue_name,
                       "findings": findings, "actioned": actioned,
                       "as_of": fname[len(prefix):-len(".md")]})
    return {"present": True, "queues": queues, "reason": None}


# Atlas-v2 M4 weekly decay queue. This is intentionally a separate loader from
# the daily prefixes above: applying the daily one-hour freshness rule to a
# Monday DECAY run would report it stale almost immediately. The producer's
# write_decay_queue() emits DECAY-YYYY-MM-DD.md, then uses numeric suffixes
# (-2, -3, ...) when a date is written more than once.
_DECAY_FILENAME_RE = re.compile(
    r"^DECAY-(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:-(?P<suffix>\d+))?\.md$")
_DECAY_CHECKS = (
    ("STILL TRUE", re.compile(
        r"^\s*-\s*\[[xX]\]\s*\*\*STILL TRUE\*\*")),
    ("NEEDS UPDATE", re.compile(
        r"^\s*-\s*\[[xX]\]\s*\*\*NEEDS UPDATE\*\*")),
    ("DEFER", re.compile(
        r"^\s*-\s*\[[xX]\]\s*\*\*DEFER\*\*")),
)


def _empty_decay(decay_dir=None, reason=None):
    """Return the stable shape for an absent or degraded DECAY feed."""
    return {
        "configured": bool(decay_dir),
        "present": False,
        "path": decay_dir,
        "file": None,
        "source_file": None,
        "as_of": "",
        "suffix": None,
        "cadence": DECAY_CADENCE,
        "freshness": "unknown",
        "age_days": None,
        "freshness_threshold_seconds": STALE_DECAY,
        # No counts are emitted for an absent run. In particular, zero does
        # not mean the producer found no work: write_decay_queue() intentionally
        # writes nothing for an empty result.
        "state_counts": {},
        "counts": {},
        "items": [],
        "retention": "unresolved records retained regardless of age",
        "reason": reason,
    }


def _decay_candidate(path, name):
    """Return a sortable candidate for a producer-named queue file, or None."""
    match = _DECAY_FILENAME_RE.fullmatch(name)
    if not match or not os.path.isfile(path):
        return None
    try:
        run_date = datetime.date.fromisoformat(match.group("date"))
    except ValueError:
        return None
    # The unsuffixed producer output is the first run for a date. Suffixes are
    # numeric, so -10 must sort after -2 (lexical sorting gets this wrong).
    suffix = int(match.group("suffix")) if match.group("suffix") else None
    suffix_rank = suffix if suffix is not None else 1
    return (run_date, suffix_rank, name, suffix)


def _parse_decay_queue(text):
    """Parse producer-compatible DECAY blocks into state-only projections.

    The producer's apply loop recognizes the same three bold checkbox labels.
    We retain only a block id/path, its checked labels, and the derived state;
    queue bodies are deliberately not copied into the boot pack.
    """
    items = []
    current = None
    for raw in text.splitlines():
        if raw.startswith("### "):
            if current is not None:
                items.append(current)
            heading = raw[4:].strip()
            path_match = re.match(r"`([^`]+)`", heading)
            item_id = path_match.group(1) if path_match else heading
            current = {"id": item_id, "path": item_id, "checked": []}
            continue
        if current is None:
            continue
        for label, pattern in _DECAY_CHECKS:
            if pattern.match(raw):
                if label not in current["checked"]:
                    current["checked"].append(label)
                break
    if current is not None:
        items.append(current)
    if not items:
        return None, "DECAY queue contains no ### review sections"

    counts = {
        "total": len(items),
        "reviewed": 0,
        "needs_update": 0,
        "deferred": 0,
        "pending": 0,
        "unchecked": 0,
    }
    for item in items:
        checked = item["checked"]
        if len(checked) == 1 and checked[0] == "STILL TRUE":
            state = "reviewed"
        elif len(checked) == 1 and checked[0] == "NEEDS UPDATE":
            state = "needs_update"
        elif len(checked) == 1 and checked[0] == "DEFER":
            state = "deferred"
        else:
            # No checked box is pending. Multiple checked boxes are an
            # inconsistent human edit and remain pending rather than being
            # mistaken for a resolution.
            state = "pending"
        item["state"] = state
        counts[state] += 1
        if not checked:
            counts["unchecked"] += 1
    return items, counts


def load_decay(decay_dir, now=None):
    """Load the newest Atlas weekly DECAY queue from ``decay_dir``.

    ``None`` means the feed is unconfigured and remains silent. A configured
    directory that is missing, unreadable, empty, or contains an unreadable or
    malformed selected queue returns a reason for the WARNING block. The
    newest run is selected by date and numeric suffix; old unresolved runs are
    retained and marked stale rather than discarded.
    """
    if not decay_dir:
        return _empty_decay()
    if not os.path.isdir(decay_dir):
        return _empty_decay(
            decay_dir,
            "DECAY feed directory does not exist: %s" % decay_dir)
    try:
        names = os.listdir(decay_dir)
    except OSError as exc:
        return _empty_decay(
            decay_dir, "unreadable DECAY feed directory: %s" % exc)

    candidates = []
    for name in names:
        path = os.path.join(decay_dir, name)
        candidate = _decay_candidate(path, name)
        if candidate is not None:
            candidates.append((candidate, path))
    if not candidates:
        return _empty_decay(
            decay_dir,
            "no DECAY queue run at %s (producer emits no file when the result "
            "is empty; no work cannot be inferred)" % decay_dir)

    candidate, selected_path = max(candidates, key=lambda pair: pair[0][:3])
    run_date, _suffix_rank, file_name, suffix = candidate
    try:
        with open(selected_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return _empty_decay(
            decay_dir, "DECAY queue %s unreadable: %s" % (file_name, exc))

    items, parsed = _parse_decay_queue(text)
    if items is None:
        return _empty_decay(
            decay_dir, "DECAY queue %s invalid: %s" % (file_name, parsed))

    as_of = run_date.isoformat()
    run_ts = parse_ts(as_of)
    reference_now = now or datetime.datetime.now(datetime.timezone.utc)
    age_seconds = max(0, (reference_now - run_ts).total_seconds()) \
        if run_ts else 0
    freshness = "fresh" if age_seconds <= STALE_DECAY else "stale"
    return {
        "configured": True,
        "present": True,
        "path": decay_dir,
        "file": file_name,
        "source_file": selected_path,
        "as_of": as_of,
        "suffix": suffix,
        "cadence": DECAY_CADENCE,
        "freshness": freshness,
        "age_days": int(age_seconds // (24 * 3600)),
        "freshness_threshold_seconds": STALE_DECAY,
        "state_counts": parsed,
        "counts": parsed,
        "items": items,
        "retention": "unresolved records retained regardless of age",
        "reason": None,
    }


def _refresh_decay_freshness(decay, now):
    """Apply injected pack time to a loaded DECAY result."""
    decay = dict(decay)
    if not decay.get("present"):
        return decay
    ts = parse_ts(decay.get("as_of", ""))
    if ts is None:
        decay["freshness"] = "unknown"
        decay["age_days"] = None
        return decay
    age_seconds = max(0, (now - ts).total_seconds())
    decay["freshness"] = "fresh" if age_seconds <= STALE_DECAY else "stale"
    decay["age_days"] = int(age_seconds // (24 * 3600))
    return decay


# --------------------------------------------------------------------------- #
# defects (section 0) -- consumes defect-status/v1 written by verify-defects.py
# --------------------------------------------------------------------------- #
def _valid_defect_counts(counts):
    """The summary line's small, strict defect-status/v1 count shape."""
    if not isinstance(counts, dict) or not counts:
        return False
    for severity in ("P0", "P1", "P2"):
        block = counts.get(severity)
        if not isinstance(block, dict):
            return False
        for key in ("open", "oldest_days"):
            value = block.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
    for key in ("fixed_since_last", "provisional", "deferred", "error"):
        value = counts.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
    owners = counts.get("open_by_owner")
    if not isinstance(owners, dict):
        return False
    return all(isinstance(owner, str) and owner
               and isinstance(value, int) and not isinstance(value, bool)
               and value >= 0 for owner, value in owners.items())


def _valid_defect_item(rec):
    """The bullet's small, strict item shape -- only what defect_bullets reads.

    ``id`` keys a dict and breaks the tie in the bullet sort, so it must be a
    non-empty string; a list or a dict id raises TypeError, and an integer id
    beside a string one makes the sort itself unorderable. ``days_open`` is
    negated in that same sort key and is None on a fixed item, so None stays
    legal and everything non-integer does not.
    """
    if not isinstance(rec, dict):
        return False
    item_id = rec.get("id")
    if not isinstance(item_id, str) or not item_id:
        return False
    days = rec.get("days_open")
    if days is None:
        return True
    return isinstance(days, int) and not isinstance(days, bool) and days >= 0


def load_defects(path):
    """Load the defect-status/v1 file. Never raises.

    ``path=None`` means the feed is not configured: the section is silently
    absent (the adjudication-feed precedent). A CONFIGURED path that is missing
    or unusable comes back ``present: False`` with a reason, so section 0 still
    renders one honest line and collect() raises a WARNING -- a defect register
    nobody ran is exactly the silence this system exists to break.
    """
    if not path:
        return {"configured": False, "present": False, "path": None,
                "reason": None}
    base = {"configured": True, "present": False, "path": path}
    if not os.path.isfile(path):
        base["reason"] = "no defect status at %s (run tools/verify-defects.py)" % path
        return base
    try:
        data = _load_json(path)
    except (OSError, ValueError) as exc:
        base["reason"] = "unreadable defect status at %s: %s" % (path, exc)
        return base
    if not isinstance(data, dict) or data.get("schema") != DEFECT_STATUS_SCHEMA:
        base["reason"] = ("defect status at %s is not schema %s"
                          % (path, DEFECT_STATUS_SCHEMA))
        return base
    if not _valid_defect_counts(data.get("counts")):
        base["reason"] = "defect status at %s carries an invalid counts block" % path
        return base
    items = data.get("items") or []
    if not isinstance(items, list):
        base["reason"] = "defect status at %s carries a non-list items block" % path
        return base
    for position, record in enumerate(items):
        if not _valid_defect_item(record):
            base["reason"] = ("defect status at %s carries a malformed item "
                              "record (item %d)" % (path, position))
            return base
    base.update({
        "present": True,
        "reason": None,
        "generated_at": data.get("generated_at", ""),
        "register_updated": data.get("register_updated", ""),
        "counts": data["counts"],
        "yours_today": [i for i in (data.get("yours_today") or [])
                        if isinstance(i, str)],
        "items": items,
    })
    return base


def defects_summary_line(counts):
    """The ONE defect line, built from the status file's counts block.

    Deliberately duplicated in verify-defects.py and boot-digest.py rather than
    imported: the three tools couple by the defect-status/v1 data contract only.
    """
    counts = counts or {}
    p0 = counts.get("P0") or {}
    p1 = counts.get("P1") or {}
    p2 = counts.get("P2") or {}
    p0_open = int(p0.get("open") or 0)
    oldest = " (oldest %dd)" % int(p0.get("oldest_days") or 0) if p0_open else ""
    owners = counts.get("open_by_owner") or {}
    return ("DEFECTS -- P0: %d%s · P1: %d · P2: %d · "
            "fixed since yesterday: %d · yours today: %d · "
            "no real check yet: %d · accepted: %d"
            % (p0_open, oldest, int(p1.get("open") or 0), int(p2.get("open") or 0),
               int(counts.get("fixed_since_last") or 0),
               int(owners.get("anthony") or 0),
               int(counts.get("provisional") or 0),
               int(counts.get("accepted") or 0)))


def _defect_bullet(rec):
    line = "- %s — %dd — %s" % (rec.get("id", "?"),
                                int(rec.get("days_open") or 0),
                                _oneline(rec.get("symptom")) or "(no symptom)")
    if len(line) > DEFECT_BULLET_CHARS:
        line = line[:DEFECT_BULLET_CHARS - 3].rstrip() + "..."
    return line


def defect_bullets(defects, limit=DEFECTS_MAX_BULLETS):
    """``(rendered_ids, lines)`` -- Anthony's items first, oldest open P1 to fill."""
    if not defects.get("present"):
        return [], []
    by_id = {r.get("id"): r for r in defects.get("items", []) if r.get("id")}
    chosen = []
    for item_id in defects.get("yours_today", []):
        rec = by_id.get(item_id)
        if rec is not None and rec not in chosen:
            chosen.append(rec)
    fillers = [r for r in defects.get("items", [])
               if r.get("status") == "open" and r.get("severity") == "P1"
               and r not in chosen]
    fillers.sort(key=lambda r: (-(r.get("days_open") or 0), r.get("id", "")))
    chosen.extend(fillers)
    chosen = chosen[:limit]
    return ([r.get("id", "?") for r in chosen],
            [_defect_bullet(r) for r in chosen])


# --------------------------------------------------------------------------- #
# surfaces (artifact 1 fold)
# --------------------------------------------------------------------------- #
def _has_degrade(obj):
    if isinstance(obj, dict):
        if obj.get("degraded") is True:
            return True
        return any(_has_degrade(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_degrade(v) for v in obj)
    return False


def collect_degraded_probes(obj, path=""):
    """Return [(probe_name, reason), ...] for every degraded marker in a surface.

    probe_name is the dotted key path to the marker (e.g. 'next_session',
    'identity.behind_origin_main'); reason is the marker's own 'reason' field.
    Deterministic (insertion / index order). Does not descend into a marker once
    found, so the reason string is reported once, never re-walked.
    """
    out = []
    if isinstance(obj, dict):
        if obj.get("degraded") is True:
            out.append((path or "surface",
                        obj.get("reason", "no reason given")))
            return out
        for k, v in obj.items():
            child = "%s.%s" % (path, k) if path else k
            out.extend(collect_degraded_probes(v, child))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(collect_degraded_probes(v, "%s[%d]" % (path, i)))
    return out


def load_surface(repo_entry, surfaces_cache_dir):
    """Resolve a repo's surface.json. Missing/invalid -> degraded (never raise).

    Lookup order: <repo_path>/.macro/surface.json, then
    <surfaces_cache_dir>/<environment>.json (a fully-aware-local cache so the
    assembler can fold surfaces without writing into other repos).
    """
    env = repo_entry.get("environment", "")
    repo_path = repo_entry.get("repo_path", "")
    candidates = []
    if repo_path:
        candidates.append(os.path.join(repo_path, ".macro", "surface.json"))
    if surfaces_cache_dir:
        candidates.append(os.path.join(surfaces_cache_dir, env + ".json"))

    for p in candidates:
        if not os.path.isfile(p):
            continue
        try:
            data = _load_json(p)
        except (OSError, ValueError) as exc:
            return {"degraded": True, "reason":
                    "invalid surface.json at %s: %s" % (p, exc), "path": p}
        if not isinstance(data, dict) or data.get("schema") != SURFACE_SCHEMA:
            return {"degraded": True, "reason":
                    "surface at %s is not schema %s" % (p, SURFACE_SCHEMA),
                    "path": p}
        return {"degraded": False, "path": p, "data": data,
                "self_degraded": _has_degrade(data)}
    return {"degraded": True, "reason":
            "no surface.json found (looked in .macro/ and the surfaces cache)",
            "path": candidates[0] if candidates else ""}


# --------------------------------------------------------------------------- #
# next-session human_only feed (via the M1 parser)
# --------------------------------------------------------------------------- #
def load_human_only(repo_entry, now):
    """Extract next-session/v2 human_only[] from a repo's root NEXT_SESSION.json.

    Absence is normal (most repos have none) -- returns []. A present-but-broken
    file surfaces as a degraded note but never raises.
    """
    repo_path = repo_entry.get("repo_path", "")
    env = repo_entry.get("environment", "")
    root_file = os.path.join(repo_path, "NEXT_SESSION.json")
    if not os.path.isfile(root_file):
        return {"items": [], "degraded": None}
    try:
        rec = ns.normalize_file(root_file)
    except Exception as exc:  # last-resort guard; parser is designed not to raise
        return {"items": [], "degraded": "next_session parser error in %s: %s"
                % (env, exc)}
    if rec.get("unparseable"):
        return {"items": [], "degraded":
                "unparseable NEXT_SESSION.json in %s" % env}
    norm = rec.get("normalized", {}) or {}
    human = [x for x in (norm.get("human_only") or []) if x]
    written = norm.get("written_at", "") or ""
    items = [{"summary": h, "source": "next-session:%s" % env,
              "as_of": written} for h in human]
    return {"items": items, "degraded": None}


# --------------------------------------------------------------------------- #
# scan feed (scan-consumption-interface-v1)
# --------------------------------------------------------------------------- #
def _schema_major(schema_str, prefix):
    """Return (ok_prefix, major_int_or_None) for a 'prefix@vN' schema string."""
    if not isinstance(schema_str, str) or not schema_str.startswith(prefix + "@"):
        return False, None
    tail = schema_str[len(prefix) + 1:]
    if not tail.startswith("v"):
        return True, None
    try:
        return True, int(tail[1:].split(".")[0])
    except ValueError:
        return True, None


def validate_scan_artifact(path, schema_prefix):
    """Validate one scan artifact independently (per spec SS6 consumer duties).

    Returns a dict describing status; never raises. Statuses:
      ok               -- valid v1 envelope, entries counted.
      invalid          -- unreadable / not JSON / wrong schema family.
      rejected-major   -- known family but unknown schema major (reject + banner).
    Unknown keys are ignored by construction (only known keys are read).
    """
    try:
        data = _load_json(path)
    except (OSError, ValueError) as exc:
        return {"status": "invalid", "reason": "unreadable/not JSON: %s" % exc}
    if not isinstance(data, dict):
        return {"status": "invalid", "reason": "artifact is not a JSON object"}
    ok_prefix, major = _schema_major(data.get("schema"), schema_prefix)
    if not ok_prefix:
        return {"status": "invalid",
                "reason": "schema %r is not %s@vN" % (data.get("schema"),
                                                      schema_prefix)}
    if major != 1:
        return {"status": "rejected-major",
                "reason": "unknown schema major %r (want @v1); reject + banner"
                % data.get("schema")}
    entries = data.get("entries")
    entries_n = len(entries) if isinstance(entries, list) else 0
    producer = data.get("producer") if isinstance(data.get("producer"), dict) else {}
    prov = "%s@%s (%s)" % (producer.get("name", "?"),
                           producer.get("version", "?"),
                           (producer.get("commit", "?") or "?")[:12])
    out = {"status": "ok", "entries": entries_n, "provenance": prov,
           "as_of": data.get("generated_at", "")}
    if isinstance(data.get("budget_split"), dict):
        out["budget_split"] = data["budget_split"]
    return out


def load_scan(scan_dir, now):
    """Consume the scan-consumption-interface-v1 artifacts from scan_dir.

    Absence of the dir/artifacts is a STATE, not an error. Each artifact is
    validated independently: one bad artifact never kills the cycle.
    """
    if not scan_dir:
        return {"present": False, "path": None, "as_of":
                now.isoformat(timespec="seconds"),
                "reason": "no --scan-consumption-dir configured"}
    if not os.path.isdir(scan_dir):
        return {"present": False, "path": scan_dir, "as_of":
                now.isoformat(timespec="seconds"),
                "reason": "scan-consumption-dir does not exist"}
    artifacts = []
    any_found = False
    for fname, prefix, required in SCAN_ARTIFACTS:
        p = os.path.join(scan_dir, fname)
        if not os.path.isfile(p):
            artifacts.append({"file": fname, "required": required,
                              "status": "absent", "prefix": prefix})
            continue
        any_found = True
        res = validate_scan_artifact(p, prefix)
        res.update({"file": fname, "required": required, "prefix": prefix})
        artifacts.append(res)
    return {"present": any_found, "path": scan_dir,
            "as_of": now.isoformat(timespec="seconds"), "artifacts": artifacts}


def _first_sentence(text):
    """First sentence of a plain-English note (period-space boundary)."""
    text = (text or "").strip()
    cut = text.find(". ")
    return text[:cut + 1] if cut != -1 else text


def load_plans(path, now):
    """plans-snapshot.json (written by ledger.py export) is OPTIONAL and
    degrades on any problem -- a missing or stale snapshot is a WARNING,
    never fatal."""
    empty = {"present": False, "path": path, "warn": None, "lanes": [],
             "unregistered": [], "generated": ""}
    if not path:
        return empty
    if not os.path.isfile(path):
        empty["warn"] = ("plans snapshot unavailable: no file at %s "
                         "(ledger.py export writes it)" % path)
        return empty
    try:
        data = _load_json(path)
    except (OSError, ValueError) as exc:
        empty["warn"] = "plans snapshot unreadable: %s" % exc
        return empty
    generated = data.get("generated", "")
    warn = None
    ts = parse_ts(generated)
    if ts is None:
        warn = "plans snapshot generated time unparseable (%r)" % generated
    elif (now - ts).total_seconds() > STALE_PLANS:
        warn = "plans snapshot STALE (generated %s, >36h old)" % generated
    return {"present": True, "path": path, "warn": warn,
            "lanes": data.get("lanes") or [],
            "unregistered": data.get("unregistered_plan_files") or [],
            "generated": generated}


# --------------------------------------------------------------------------- #
# model assembly
# --------------------------------------------------------------------------- #
def collect(now, manifest, backlog, surfaces_cache_dir, scan_dir,
            adjudication=None, defects=None, plans=None, decay=None):
    """Build the section data model + warnings + open-items. Pure data (no md)."""
    adjudication = adjudication or {"present": False, "queues": [],
                                    "reason": None}
    decay = _refresh_decay_freshness(
        decay or load_decay(None), now)
    plans = plans or {"present": False, "path": None, "warn": None,
                      "lanes": [], "unregistered": [], "generated": ""}
    defects = defects or {"configured": False, "present": False, "path": None,
                          "reason": None}
    warnings = []
    manifest_as_of = manifest.get("as_of", "")

    # -- section 0: defects ---------------------------------------------------
    if defects.get("configured") and not defects.get("present"):
        warnings.append("defect status unavailable: %s"
                        % defects.get("reason", "?"))
    defects = dict(defects)
    rendered_ids, defect_lines = defect_bullets(defects)
    defects["rendered_ids"] = rendered_ids
    defects["bullets"] = defect_lines

    # -- section 1: topology --------------------------------------------------
    topo_entries = []
    for r in manifest.get("repos", []):
        topo_entries.append({
            "environment": r.get("environment", ""),
            "repo_path": r.get("repo_path", ""),
            "role": r.get("role", ""),
            "status": r.get("status", ""),
            "kind": r.get("kind", ""),
            "owning_system": r.get("owning_system", ""),
            "default_branch": r.get("default_branch", ""),
            "pins": list(r.get("pins", []) or []),
            "source": "seed-manifest.json (%s)" % manifest.get("provenance", "manual"),
            "as_of": manifest_as_of,
        })

    # -- section 2: surfaces --------------------------------------------------
    surface_repos = []
    for r in manifest.get("repos", []):
        env = r.get("environment", "")
        s = load_surface(r, surfaces_cache_dir)
        if s.get("degraded"):
            warnings.append("surface DEGRADED for %s: %s" % (env, s.get("reason")))
            surface_repos.append({"environment": env, "degraded": True,
                                  "reason": s.get("reason"),
                                  "source": "surface.json", "as_of": ""})
            continue
        data = s["data"]
        if s.get("self_degraded"):
            probes = collect_degraded_probes(data)
            if probes:
                detail = "; ".join("%s (%s)" % (name, reason)
                                   for name, reason in probes)
                warnings.append("surface for %s: degraded probe %s"
                                % (env, detail))
            else:
                warnings.append(
                    "surface for %s carries inline degraded probe(s)" % env)
        surface_repos.append(_project_surface(env, data, s["path"]))

    # -- section 3: unified decision queue (projection) -----------------------
    queue = []
    # feed a: surface decisions[]
    for sr in surface_repos:
        if sr.get("degraded"):
            continue
        for d in sr.get("decisions", []):
            queue.append({
                "summary": d.get("summary", "") or d.get("id", ""),
                "kind": d.get("kind", "ruling"),
                "source": "surface:%s" % sr["environment"],
                "as_of": d.get("waiting_since") or d.get("as_of") or "",
            })
    # feed b: next-session human_only[]
    for r in manifest.get("repos", []):
        ho = load_human_only(r, now)
        if ho.get("degraded"):
            warnings.append("decision-feed DEGRADED: %s" % ho["degraded"])
        for it in ho["items"]:
            queue.append({"summary": it["summary"], "kind": "human_only",
                          "source": it["source"], "as_of": it["as_of"]})
    # feed c: standing ratification backlog. Placeholder entries (seed shapes
    # Anthony hasn't replaced with real items) are SKIPPED -- they never leak
    # into the live inbox; a single provenance footer records they were skipped.
    if not backlog.get("present"):
        warnings.append("ratification backlog unavailable: %s"
                        % backlog.get("reason", "?"))
    backlog_live = 0
    backlog_placeholders = 0
    for it in backlog.get("items", []):
        if it.get("placeholder") is True:
            backlog_placeholders += 1
            continue
        backlog_live += 1
        queue.append({
            "summary": it.get("summary", "") or it.get("id", ""),
            "kind": it.get("kind", "ratification"),
            "source": it.get("source", "ratification-backlog.json"),
            "as_of": it.get("waiting_since", "") or backlog.get("as_of", ""),
        })
    backlog_summary = {
        "present": bool(backlog.get("present")),
        "live": backlog_live,
        "placeholders_skipped": backlog_placeholders,
        "source": backlog.get("source", "ratification-backlog.json"),
        "as_of": backlog.get("as_of", ""),
    }
    # feed d: atlas-v2 adjudication queues (one item per current queue file;
    # the loader already reduced each queue to its newest file + counts).
    if adjudication.get("reason"):
        warnings.append("adjudication feed unavailable: %s"
                        % adjudication["reason"])
    for aq in adjudication.get("queues", []):
        if aq.get("degraded"):
            warnings.append("adjudication queue %s unreadable: %s"
                            % (aq.get("file"), aq.get("reason")))
            continue
        queue.append({
            "summary": "atlas-v2 %s: %d finding(s) awaiting checkbox pass "
                       "(%d actioned) -- %s"
                       % (aq["queue"], aq["findings"], aq["actioned"],
                          aq["file"]),
            "kind": "adjudication",
            "source": "adjudication:atlas-v2",
            "as_of": aq["as_of"],
        })
    # feed e: Atlas-v2 M4 weekly DECAY review. This remains a single queue
    # item so the existing digest/attention projection keeps working, while
    # the sidecar's typed section carries the per-state counts for the digest.
    if decay.get("configured") and not decay.get("present"):
        warnings.append("DECAY feed unavailable: %s"
                        % decay.get("reason", "?"))
    if decay.get("present"):
        counts = decay.get("state_counts") or {}
        queue.append({
            "summary": (
                "atlas-v2 weekly DECAY review (%s): %d item(s) -- "
                "%d reviewed, "
                "%d needs update, %d deferred, %d pending -- %s"
                % (decay.get("freshness", "unknown"),
                   counts.get("total", 0), counts.get("reviewed", 0),
                   counts.get("needs_update", 0), counts.get("deferred", 0),
                   counts.get("pending", 0), decay.get("file", "?"))),
            "kind": "decay",
            "source": "adjudication:atlas-v2",
            "as_of": decay.get("as_of", ""),
            "cadence": decay.get("cadence", DECAY_CADENCE),
            "freshness": decay.get("freshness", "unknown"),
            "stale_after_seconds": STALE_DECAY,
            "state_counts": counts,
            "counts": counts,
            "file": decay.get("file"),
        })
    # deterministic order: oldest waiting first (missing as_of sorts last),
    # tie-break by source then summary.
    def _qkey(q):
        ts = parse_ts(q["as_of"])
        # missing timestamp -> sort last (large sentinel)
        epoch = ts.timestamp() if ts else float("inf")
        return (epoch, q["source"], q["summary"])
    queue.sort(key=_qkey)

    # -- section 4: plans (ledger-backed) -------------------------------------
    if plans.get("warn"):
        warnings.append(plans["warn"])

    # -- section 5: scan feed -------------------------------------------------
    scan = load_scan(scan_dir, now)
    if scan.get("present"):
        for a in scan.get("artifacts", []):
            if a["status"] in ("invalid", "rejected-major"):
                warnings.append("scan artifact %s %s: %s"
                                % (a["file"], a["status"], a.get("reason", "")))
            elif a["status"] == "absent" and a["required"]:
                warnings.append("required scan artifact %s absent" % a["file"])

    # -- open items -----------------------------------------------------------
    open_items = [
        "COS v2 event contract wiring: section 3 (unified decision queue) is "
        "specified by SS3.1 as a projection over the COS v2 event contract, with "
        "mission-control as a second head over the same projection. No concrete "
        "COS v2 event SOURCE exists on disk yet, so this assembler projects over "
        "the four available feeds (surface decisions[], next-session "
        "human_only[], ratification backlog, atlas-v2 adjudication) instead. "
        "Wire the COS v2 event source into this projection when it lands -- do "
        "NOT invent a reader.",
    ]

    return {
        "manifest_provenance": manifest.get("provenance", "manual"),
        "manifest_as_of": manifest_as_of,
        "defects": defects,
        "topology": topo_entries,
        "surfaces": surface_repos,
        "queue": queue,
        "plans": plans,
        "scan": scan,
        "backlog": backlog,
        "decay": decay,
        "backlog_summary": backlog_summary,
        "warnings": warnings,
        "open_items": open_items,
    }


def _oneline(v):
    """Coerce a value to a single whitespace-collapsed line ("" for None)."""
    if v is None:
        return ""
    return " ".join(str(v).split())


def _project_next_session(data):
    """Reduce surface.next_session to {status, summary, as_of, source} or None.

    A degraded probe is already reported in the WARNING block, so only a parsed
    record projects. The summary is collapsed to a single line and hard-cut at
    NEXT_SESSION_SUMMARY_CHARS: this tier is one line per repo, never a block.
    """
    block = data.get("next_session")
    if not isinstance(block, dict) or block.get("degraded"):
        return None
    norm = block.get("normalized")
    if not isinstance(norm, dict):
        return None
    status = _oneline(norm.get("status"))
    summary = _oneline(norm.get("summary"))
    if not status and not summary:
        return None
    if len(summary) > NEXT_SESSION_SUMMARY_CHARS:
        summary = summary[:NEXT_SESSION_SUMMARY_CHARS - 3].rstrip() + "..."
    return {
        "status": status,
        "summary": summary,
        "as_of": _oneline(block.get("as_of")) or _oneline(norm.get("written_at")),
        "source": _oneline(block.get("source")) or "next_session.py",
    }


def _project_surface(env, data, path):
    """Reduce a surface/v1 doc to the boot-pack's per-repo projection."""
    ident = data.get("identity", {}) if isinstance(data.get("identity"), dict) else {}
    cold = [c for c in data.get("cold_load", []) if isinstance(c, dict)]
    state = data.get("state", {}) if isinstance(data.get("state"), dict) else {}
    open_prs = state.get("open_prs")
    prs = open_prs if isinstance(open_prs, list) else []
    verification = state.get("verification")
    ver = verification if isinstance(verification, list) else []
    decisions = [d for d in data.get("decisions", []) if isinstance(d, dict)]
    next_lanes = [n for n in data.get("next_lanes", []) if isinstance(n, dict)]
    return {
        "environment": env,
        "degraded": False,
        "surface_path": path,
        "generated_at": data.get("generated_at", ""),
        "source_commit_time": data.get("source_commit_time", ""),
        "role": ident.get("role", ""),
        "head_sha": ident.get("head_sha", ""),
        "behind_origin_main": ident.get("behind_origin_main", ""),
        "cold_load": cold,
        "open_prs": prs,
        "verification": ver,
        "decisions": decisions,
        "next_session": _project_next_session(data),
        "next_lanes": next_lanes,
        "next_lanes_truncated": 0,
    }


# --------------------------------------------------------------------------- #
# markdown rendering
# --------------------------------------------------------------------------- #
def _fmt_val(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, dict) and v.get("degraded"):
        return "DEGRADED(%s)" % v.get("reason", "?")
    return str(v)


def defect_age_prefix(now, as_of_str):
    """The mark section 0's summary line carries: STALE, AS_OF, or nothing.

    STALE(<age>) past 36h, exactly as the rest of the pack marks a dead feed.
    Below that, AS_OF(<age>) whenever the status is more than DEFECT_LAG behind
    this pack -- the register step runs seconds before the assembler, so any
    real gap means these counts are not from this run.
    """
    prefix = stale_prefix(now, as_of_str, STALE_DEFECTS)
    if prefix:
        return prefix
    ts = parse_ts(as_of_str)
    if ts is None:
        return ""
    age = now - ts
    if age.total_seconds() > DEFECT_LAG:
        return "AS_OF(%s) " % humanize_age(age)
    return ""


def render_defects(model, now):
    """Section 0: the defect register's status, in at most a dozen lines.

    First content line is the ONE summary line every session sees, carrying the
    same STALE(<age>) prefix the rest of the pack uses when the morning loop did
    not run -- or AS_OF(<age>) when the status is merely older than this pack.
    Then Anthony's items for today, oldest open P1s filling the rest.
    """
    defects = model.get("defects") or {}
    if not defects.get("configured"):
        return ""
    lines = ["## 0. Defects (single register; verify exit 0 = fixed)"]
    if not defects.get("present"):
        lines.append("no defect status yet (run tools/verify-defects.py)")
        lines.append("")
        return "\n".join(lines)
    lines.append(defect_age_prefix(now, defects.get("generated_at", ""))
                 + defects_summary_line(defects.get("counts")))
    lines.extend(defects.get("bullets") or [])
    # Hard line cap: the section may never grow into the pack's budget.
    while len(lines) > DEFECTS_SECTION_MAX_LINES - 1:
        lines.pop()
    lines.append("")
    return "\n".join(lines)


def render_topology(model, now):
    lines = ["## 1. Topology manifest",
             "_provenance: %s (hand-maintained seed until P21 repo-manifest.json ships)_"
             % model["manifest_provenance"], ""]
    for e in model["topology"]:
        sp = stale_prefix(now, e["as_of"], STALE_TOPOLOGY)
        pins = ("; pins: " + " / ".join(e["pins"])) if e["pins"] else ""
        lines.append("- %s**%s** (%s, %s) -- %s"
                     % (sp, e["environment"], e["status"], e["kind"], e["role"]))
        lines.append("  path: `%s` | branch: %s | owner: %s%s %s"
                     % (e["repo_path"], e["default_branch"], e["owning_system"],
                        pins, tag(e["source"], e["as_of"])))
    lines.append("")
    return "\n".join(lines)


def render_surfaces(model, now):
    lines = ["## 2. State surfaces",
             "_fold of every discoverable surface (surface/v1); normally state/surfaces/<env>.json_",
             ""]
    for s in model["surfaces"]:
        env = s["environment"]
        if s.get("degraded"):
            lines.append("- **%s** -- WARNING: surface unavailable: %s %s"
                         % (env, s.get("reason"), tag("surface.json", "")))
            continue
        sp = stale_prefix(now, s["generated_at"], STALE_SURFACES)
        lines.append("- %s**%s** -- %s"
                     % (sp, env, s.get("role", "") or "(no role)"))
        lines.append("  head: %s | behind origin/main: %s | surface %s"
                     % (_fmt_val(s["head_sha"]), _fmt_val(s["behind_origin_main"]),
                        tag(os.path.relpath(s["surface_path"], _REPO_ROOT)
                            if _inside(s["surface_path"], _REPO_ROOT)
                            else s["surface_path"], s["generated_at"])))
        for c in s["cold_load"]:
            if c.get("degraded"):
                lines.append("    - cold-load DEGRADED: %s" % c.get("reason"))
            else:
                lines.append("    - %s %s"
                             % (c.get("claim", ""),
                                tag(c.get("source", ""), c.get("as_of", ""))))
        nxt = s.get("next_session")
        if nxt:
            lines.append("    - next-session[%s]: %s %s"
                         % (nxt["status"] or "?", nxt["summary"] or "(no summary)",
                            tag(nxt["source"], nxt["as_of"])))
        for p in s["open_prs"] if isinstance(s["open_prs"], list) else []:
            if isinstance(p, dict) and not p.get("degraded"):
                lines.append("    - PR #%s %s -> %s %s"
                             % (p.get("number"), p.get("head", ""),
                                p.get("base", ""),
                                tag(p.get("source", "gh"), p.get("as_of", ""))))
        for v in s["verification"]:
            if isinstance(v, dict) and not v.get("degraded"):
                lines.append("    - verify %s: exit %s %s"
                             % (v.get("name", ""), v.get("checker_exit"),
                                tag(v.get("source", ""), v.get("as_of", ""))))
        # next_lanes -- the truncation tier
        if s["next_lanes_truncated"]:
            lines.append("    - next_lanes: TRUNCATED: %d entries (over %d-token cap)"
                         % (s["next_lanes_truncated"], HARD_CAP_TOKENS))
        for n in s["next_lanes"]:
            if isinstance(n, dict) and not n.get("degraded"):
                lines.append("    - next-lane: %s %s"
                             % (n.get("entry", ""),
                                tag(n.get("source", ""), n.get("as_of", ""))))
    lines.append("")
    return "\n".join(lines)


def render_queue(model, now):
    decay = model.get("decay") or {}
    feed_note = (", and the weekly DECAY review"
                 if decay.get("configured") else "")
    lines = ["## 3. Unified decision queue (projection -- routes, never absorbs)",
             "_one ordered inbox, oldest waiting first; projection over surface "
             "decisions[], next-session human_only[], the ratification "
             "backlog, the atlas-v2 adjudication queues%s. See OPEN-ITEMS "
             % feed_note,
             "for COS v2 wiring._", ""]
    if not model["queue"]:
        lines.append("- (empty) no decisions, human-only items, or backlog "
                     "entries across the manifest%s."
                     % (" or DECAY run" if decay.get("configured") else ""))
    for q in model["queue"]:
        threshold = q.get("stale_after_seconds", STALE_DECISIONS)
        if (isinstance(threshold, bool) or not isinstance(threshold, int)
                or threshold < 0):
            threshold = STALE_DECISIONS
        sp = stale_prefix(now, q["as_of"], threshold)
        lines.append("- %s[%s] %s %s"
                     % (sp, q["kind"], q["summary"], tag(q["source"], q["as_of"])))
    if decay.get("configured") and not decay.get("present"):
        # decay_review.py intentionally writes no queue file when there are no
        # past-horizon beliefs. Keep that distinction visible: no run evidence
        # is not evidence that there was no work.
        reason = decay.get("reason", "") or ""
        if reason.startswith("no DECAY queue run"):
            lines.append("- [decay] weekly DECAY: no run recorded -- "
                         "producer may emit no file when the result is empty; "
                         "no work cannot be inferred %s"
                         % tag("adjudication:atlas-v2",
                               decay.get("as_of", "")))
        else:
            lines.append("- [decay] weekly DECAY unavailable -- see WARNINGS %s"
                         % tag("adjudication:atlas-v2",
                               decay.get("as_of", "")))
    bs = model.get("backlog_summary")
    if bs and bs.get("present"):
        note = (" (seed placeholder skipped)"
                if bs.get("placeholders_skipped") else "")
        lines.append("- ratification backlog: %d live items%s %s"
                     % (bs.get("live", 0), note,
                        tag(bs.get("source", "ratification-backlog.json"),
                            bs.get("as_of", ""))))
    lines.append("")
    return "\n".join(lines)


def render_plans(model, now):
    plans = model["plans"]
    lines = ["## 4. Plans (ledger-backed, generated)",
             "_one line per registered lane, generated from the per-project "
             "ledgers via plans-snapshot.json (ledger.py export). The ledger "
             "is truth; this section is a view -- append events, never "
             "hand-edit._", ""]
    if not plans.get("present"):
        if not plans.get("path"):
            lines.append("- plans snapshot not configured (pass "
                         "--plans-snapshot; ledger.py export writes it) %s"
                         % tag("config", ""))
        else:
            lines.append("- no plans snapshot at %s %s"
                         % (plans["path"], tag("plans-snapshot.json", "")))
        lines.append("")
        return "\n".join(lines)
    src = tag("plans-snapshot.json", plans.get("generated", ""))
    for lane in plans["lanes"]:
        bits = ["- %s: %s -- %s" % (lane.get("name", "?"),
                                    lane.get("health_phrase", "?"),
                                    _first_sentence(lane.get("step", "")))]
        if lane.get("finish_progress"):
            bits.append("finish line %s" % lane["finish_progress"])
        if lane.get("waiting_on_anthony"):
            bits.append("waiting on Anthony: " + "; ".join(
                _first_sentence(w) for w in lane["waiting_on_anthony"]))
        lines.append(" | ".join(bits) + " " + src)
    unreg = plans.get("unregistered") or []
    if unreg:
        names = ", ".join(os.path.basename(u.get("path", "?")) for u in unreg)
        lines.append("- %d plan file(s) not registered (invisible to every "
                     "lane view): %s %s" % (len(unreg), names, src))
    if not plans["lanes"] and not unreg:
        lines.append("- (empty) no registered lanes %s" % src)
    lines.append("")
    return "\n".join(lines)


def render_scan(model, now):
    lines = ["## 5. Scan / priorities feed",
             "_scan-consumption-interface-v1 (weights / scan-targets / "
             "suppression / optional intentions); PR #150 ratified -- real "
             "consumption, each artifact validated independently_", ""]
    scan = model["scan"]
    if not scan.get("present"):
        if not scan.get("path"):
            # No dir configured at all -- one clean, actionable line.
            lines.append(
                "- scan consumption dir not configured (pass "
                "--scan-consumption-dir; wire into the LaunchAgent once the "
                "saga-scan run emits artifacts) %s"
                % tag("config", scan.get("as_of", "")))
        else:
            # Dir configured but no artifacts present (empty / missing dir).
            lines.append("- no scan artifacts found at %s %s"
                         % (scan.get("path"),
                            tag(scan.get("reason", "absent"),
                                scan.get("as_of", ""))))
        lines.append("")
        return "\n".join(lines)
    for a in scan.get("artifacts", []):
        req = "required" if a["required"] else "optional"
        if a["status"] == "ok":
            extra = ""
            if a.get("budget_split"):
                extra = " | budget_split=%s" % json.dumps(
                    a["budget_split"], sort_keys=True)
            lines.append("- %s (%s): OK, %d entries | producer %s%s %s"
                         % (a["file"], req, a.get("entries", 0),
                            a.get("provenance", "?"), extra,
                            tag("scan:" + a["prefix"], a.get("as_of", ""))))
        elif a["status"] == "absent":
            lines.append("- %s (%s): absent %s"
                         % (a["file"], req, tag("scan:" + a["prefix"], scan.get("as_of", ""))))
        else:
            lines.append("- %s (%s): WARNING %s -- %s %s"
                         % (a["file"], req, a["status"], a.get("reason", ""),
                            tag("scan:" + a["prefix"], scan.get("as_of", ""))))
    lines.append("")
    return "\n".join(lines)


TOKEN_PLACEHOLDER = "~PENDING"


def render_md(model, now):
    """Render the full pack markdown with a token-estimate placeholder."""
    now_iso = now.isoformat(timespec="seconds")
    head = ["# FULLY AWARE -- BOOT PACK",
            "Generated: %s" % tag("assemble-boot-pack.py", now_iso),
            "Token estimate: %s (hard cap %d, chars/4)" % (TOKEN_PLACEHOLDER, HARD_CAP_TOKENS),
            "",
            "> **ADVISORY STATE, NOT LAW.** SAGA doctrine, repo CLAUDE.md, and "
            "merge-is-Anthony's bind regardless of anything in this pack. The "
            "pack routes attention; it never absorbs ratification, and nothing "
            "here merges, pushes, or ratifies.",
            ""]
    if model["warnings"]:
        head.append("## WARNINGS (degraded sources)")
        for w in model["warnings"]:
            head.append("- %s" % w)
        head.append("")
    if model["open_items"]:
        head.append("## OPEN-ITEMS")
        for o in model["open_items"]:
            head.append("- OPEN-ITEM: %s" % o)
        head.append("")
    body = "\n".join(head) + "\n"
    defects = render_defects(model, now)
    if defects:
        body += defects + "\n"
    body += render_topology(model, now) + "\n"
    body += render_surfaces(model, now) + "\n"
    body += render_queue(model, now) + "\n"
    body += render_plans(model, now) + "\n"
    body += render_scan(model, now)
    if not body.endswith("\n"):
        body += "\n"
    return body


def est_tokens(text):
    return (len(text) + 3) // 4


def _drop_one_next_lane(model):
    """Drop one next_lane from the lowest-priority repo that still has some.

    Lowest priority = latest in manifest order. Returns True if something was
    dropped, False when nothing remains to trim.
    """
    for s in reversed(model["surfaces"]):
        if s.get("degraded"):
            continue
        if s.get("next_lanes"):
            s["next_lanes"].pop()  # drop the last (lowest) lane
            s["next_lanes_truncated"] += 1
            return True
    return False


def build_pack(now, manifest, backlog, surfaces_cache_dir, scan_dir,
               adjudication_dir=None, cap_tokens=HARD_CAP_TOKENS,
               defects_status_path=None, plans_snapshot_path=None,
               decay_dir=None):
    """Assemble (markdown, sidecar_dict). ``now`` is injectable for determinism."""
    model = collect(now, manifest, backlog, surfaces_cache_dir, scan_dir,
                    adjudication=load_adjudication(adjudication_dir),
                    defects=load_defects(defects_status_path),
                    plans=load_plans(plans_snapshot_path, now),
                    decay=load_decay(decay_dir, now=now))

    # truncation loop: shed lowest-priority next_lanes until under the cap.
    md = render_md(model, now)
    while est_tokens(md) > cap_tokens:
        if not _drop_one_next_lane(model):
            break  # nothing left to trim; render honestly over-cap (still marked)
        md = render_md(model, now)

    est = est_tokens(md)
    md = md.replace(TOKEN_PLACEHOLDER, "~%d" % est, 1)

    truncation = [{"environment": s["environment"],
                   "next_lanes_truncated": s["next_lanes_truncated"]}
                  for s in model["surfaces"]
                  if not s.get("degraded") and s.get("next_lanes_truncated")]

    sections = {}
    if model["defects"].get("configured"):
        sections["defects"] = {
            "status_path": model["defects"].get("path"),
            "generated_at": model["defects"].get("generated_at", ""),
            "counts": model["defects"].get("counts", {}),
            "yours_today": model["defects"].get("yours_today", []),
            "rendered_ids": model["defects"].get("rendered_ids", []),
        }
    sections.update({
        "topology": {"provenance": model["manifest_provenance"],
                     "as_of": model["manifest_as_of"],
                     "entries": model["topology"]},
        "surfaces": model["surfaces"],
        "decision_queue": {"projection": True, "absorbs_ratification": False,
                           "items": model["queue"]},
    })
    if model["decay"].get("configured"):
        sections["decay"] = {
            "present": model["decay"].get("present", False),
            "path": model["decay"].get("path"),
            "file": model["decay"].get("file"),
            "source_file": model["decay"].get("source_file"),
            "as_of": model["decay"].get("as_of", ""),
            "suffix": model["decay"].get("suffix"),
            "cadence": model["decay"].get("cadence", DECAY_CADENCE),
            "freshness": model["decay"].get("freshness", "unknown"),
            "age_days": model["decay"].get("age_days"),
            "freshness_threshold_seconds":
                model["decay"].get("freshness_threshold_seconds", STALE_DECAY),
            "state_counts": model["decay"].get("state_counts", {}),
            "counts": model["decay"].get("counts",
                                           model["decay"].get("state_counts", {})),
            "items": model["decay"].get("items", []),
            "retention": model["decay"].get(
                "retention", "unresolved records retained regardless of age"),
            "reason": model["decay"].get("reason"),
        }
    sections.update({
        "plans": {"present": model["plans"].get("present", False),
                  "generated": model["plans"].get("generated", ""),
                  "lanes": model["plans"].get("lanes", []),
                  "unregistered_plan_files":
                      model["plans"].get("unregistered", [])},
        "scan": model["scan"],
    })

    sidecar = {
        "schema": PACK_SCHEMA,
        "generated_at": now.isoformat(timespec="seconds"),
        "token_estimate": est,
        "hard_cap_tokens": cap_tokens,
        "advisory": "ADVISORY STATE, NOT LAW -- SAGA doctrine, repo CLAUDE.md, "
                    "and merge-is-Anthony's bind regardless of pack content.",
        "warnings": list(model["warnings"]),
        "open_items": list(model["open_items"]),
        "truncation": truncation,
        "sections": sections,
    }
    return md, sidecar


def render_sidecar(sidecar):
    return json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


# --------------------------------------------------------------------------- #
# gitignore guard (state/ outputs must be gitignored) -- generate-surface pattern
# --------------------------------------------------------------------------- #
def _inside(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) \
            == os.path.abspath(root)
    except ValueError:
        return False


def assert_gitignored(out_path):
    """Refuse an in-repo output path unless git ignores it (D30 state discipline)."""
    if not _inside(out_path, _REPO_ROOT):
        return
    import subprocess
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    proc = subprocess.run(
        ["git", "-C", _REPO_ROOT, "check-ignore", "--quiet", out_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        raise AssembleError(
            "refusing to write %s: boot-pack outputs must be gitignored "
            "(state/ is local-only). Add it to .gitignore or pass an --out "
            "path outside the repo." % out_path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _default(*parts):
    return os.path.join(_REPO_ROOT, *parts)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="D30 read-only boot-pack assembler (Fully Aware Artifact 3).")
    ap.add_argument("--manifest", default=_default("tools", "configs",
                                                   "seed-manifest.json"),
                    help="topology seed manifest (provenance: manual)")
    ap.add_argument("--ratification-backlog",
                    default=_default("tools", "configs",
                                     "ratification-backlog.json"),
                    help="hand-maintained ratification backlog (Anthony's)")
    ap.add_argument("--surfaces-cache-dir",
                    default=_default("state", "surfaces"),
                    help="fallback dir for surfaces not at <repo>/.macro/")
    ap.add_argument("--scan-consumption-dir", default=None,
                    help="scan-consumption-interface-v1 artifact dir "
                         "(absent -> section renders 'no scan artifacts found')")
    ap.add_argument("--adjudication-dir",
                    default=_DEFAULT_ATLAS_PENDING_DIR,
                    help="atlas-v2 adjudication pending dir (read-only; the "
                         "newest AUTO-APPLY-/BACKLOG-/RIPPLE-*.md per prefix "
                         "feeds the decision queue)")
    ap.add_argument("--decay-dir", default=_DEFAULT_ATLAS_PENDING_DIR,
                    help="atlas-v2 weekly DECAY pending dir (read-only; "
                         "newest DECAY-YYYY-MM-DD[-N].md feeds the decision "
                         "queue; missing run is warning-visible)")
    ap.add_argument("--defects-status",
                    default=_default("state", "defects-status.json"),
                    help="defect-status/v1 file written by verify-defects.py "
                         "(missing -> section 0 says so and a WARNING is raised)")
    ap.add_argument("--plans-snapshot",
                    default=os.path.expanduser(
                        "~/code/state/plans-snapshot.json"),
                    help="plans-snapshot.json written by ledger.py export "
                         "(missing or >36h old -> WARNING, never fatal)")
    ap.add_argument("--out-md", default=_default("state", "BOOT-PACK.md"))
    ap.add_argument("--out-json", default=_default("state", "boot-pack.json"))
    ap.add_argument("--stdout", action="store_true",
                    help="write the markdown to stdout (skips file writes)")
    args = ap.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
        backlog = load_backlog(args.ratification_backlog)
        now = datetime.datetime.now(datetime.timezone.utc)
        md, sidecar = build_pack(now, manifest, backlog,
                                 args.surfaces_cache_dir,
                                 args.scan_consumption_dir,
                                 adjudication_dir=args.adjudication_dir,
                                 decay_dir=args.decay_dir,
                                 defects_status_path=args.defects_status,
                                 plans_snapshot_path=args.plans_snapshot)
        if args.stdout:
            sys.stdout.write(md)
            _report(sys.stderr, sidecar, dest="stdout")
            return 0
        out_md = os.path.abspath(args.out_md)
        out_json = os.path.abspath(args.out_json)
        assert_gitignored(out_md)
        assert_gitignored(out_json)
        for p in (out_md, out_json):
            d = os.path.dirname(p)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
    except AssembleError as exc:
        sys.stderr.write("assemble-boot-pack: ASSEMBLY FAILURE: %s\n" % exc)
        return 2

    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(out_json, "w", encoding="utf-8") as fh:
        fh.write(render_sidecar(sidecar))
    _report(sys.stderr, sidecar, dest=out_md)
    return 0


def _report(err, sidecar, dest):
    err.write("assemble-boot-pack: pack assembled (~%d tokens, cap %d)\n"
              % (sidecar["token_estimate"], sidecar["hard_cap_tokens"]))
    err.write("  warnings: %d | truncation: %d repo(s) | open-items: %d\n"
              % (len(sidecar["warnings"]), len(sidecar["truncation"]),
                 len(sidecar["open_items"])))
    err.write("  output: %s\n" % dest)


if __name__ == "__main__":
    raise SystemExit(main())
