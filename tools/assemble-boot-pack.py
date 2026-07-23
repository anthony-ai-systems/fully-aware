#!/usr/bin/env python3
"""assemble-boot-pack.py -- D30-class boot-pack assembler (Macro Seat Artifact 3).

Folds four sections into ``state/BOOT-PACK.md`` (human render) plus
``state/boot-pack.json`` (machine sidecar), the single primary artifact a fresh
Fully Aware macro session loads regardless of cwd:

  1. Topology manifest   -- hand-maintained seed (provenance: manual) until P21.
  2. State surfaces       -- the fold of every discoverable <repo>/.macro/
                             surface.json (surface/v1, produced by generate-surface.py).
  3. Unified decision queue -- a PROJECTION (routes, never absorbs ratification)
                             over three feeds: surface decisions[], next-session
                             human_only[] (via the M1 next_session.py parser), and
                             a hand-maintained ratification backlog.
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

Staleness thresholds (spec SS3.1): topology 7d, surfaces 24h, decision items 1h.
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
import sys

# Import the M1 next-session parser from the same directory (data-contract
# consumption; no cross-repo import).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import next_session as ns  # noqa: E402

PACK_SCHEMA = "boot-pack/v1"
MANIFEST_SCHEMA = "seed-manifest/v1"
BACKLOG_SCHEMA = "ratification-backlog/v1"
SURFACE_SCHEMA = "surface/v1"

HARD_CAP_TOKENS = 50000

# Staleness thresholds (seconds).
STALE_TOPOLOGY = 7 * 24 * 3600
STALE_SURFACES = 24 * 3600
STALE_DECISIONS = 1 * 3600

# Scan-consumption-interface-v1 required + optional artifacts (fixed order).
SCAN_ARTIFACTS = [
    ("weights.json", "saga-scan/doctrine-weights", True),
    ("scan-targets.json", "saga-scan/staleness-targets", True),
    ("suppression.json", "saga-scan/reproposal-suppression", True),
    ("intentions.json", "saga-scan/intentions", False),
]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AssembleError(Exception):
    """A hard assembly failure (nonzero exit; not a degrade)."""


# --------------------------------------------------------------------------- #
# time helpers -- wall-clock lives ONLY in as_of fields
# --------------------------------------------------------------------------- #
def parse_ts(value):
    """Parse an ISO-ish timestamp to an aware UTC datetime, or None.

    Accepts date-only (YYYY-MM-DD), full ISO with/without offset, and a trailing
    'Z'. Naive values are assumed UTC. Never raises.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip().replace("Z", "+00:00")
    for fmt in (None,):  # fromisoformat first
        try:
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
        except ValueError:
            break
    # date-only or space-separated fallbacks
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(value.strip()[:19], fmt)
            return dt.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
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

    Unknown/unparseable as_of is treated as stale-unknown (fail-visible).
    """
    ts = parse_ts(as_of_str)
    if ts is None:
        return "STALE(?) "
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


# --------------------------------------------------------------------------- #
# model assembly
# --------------------------------------------------------------------------- #
def collect(now, manifest, backlog, surfaces_cache_dir, scan_dir):
    """Build the section data model + warnings + open-items. Pure data (no md)."""
    warnings = []
    manifest_as_of = manifest.get("as_of", "")

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
            warnings.append("surface for %s carries inline degraded probe(s)" % env)
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
    # feed c: standing ratification backlog
    if not backlog.get("present"):
        warnings.append("ratification backlog unavailable: %s"
                        % backlog.get("reason", "?"))
    for it in backlog.get("items", []):
        queue.append({
            "summary": it.get("summary", "") or it.get("id", ""),
            "kind": it.get("kind", "ratification"),
            "source": it.get("source", "ratification-backlog.json"),
            "as_of": it.get("waiting_since", "") or backlog.get("as_of", ""),
        })
    # deterministic order: oldest waiting first (missing as_of sorts last),
    # tie-break by source then summary.
    def _qkey(q):
        ts = parse_ts(q["as_of"])
        # missing timestamp -> sort last (large sentinel)
        epoch = ts.timestamp() if ts else float("inf")
        return (epoch, q["source"], q["summary"])
    queue.sort(key=_qkey)

    # -- section 4: scan feed -------------------------------------------------
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
        "the three available feeds (surface decisions[], next-session "
        "human_only[], ratification backlog) instead. Wire the COS v2 event "
        "source into this projection when it lands -- do NOT invent a reader.",
    ]

    return {
        "manifest_provenance": manifest.get("provenance", "manual"),
        "manifest_as_of": manifest_as_of,
        "topology": topo_entries,
        "surfaces": surface_repos,
        "queue": queue,
        "scan": scan,
        "backlog": backlog,
        "warnings": warnings,
        "open_items": open_items,
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
             "_fold of every discoverable <repo>/.macro/surface.json (surface/v1)_",
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
    lines = ["## 3. Unified decision queue (projection -- routes, never absorbs)",
             "_one ordered inbox, oldest waiting first; projection over surface "
             "decisions[], next-session human_only[], and the ratification "
             "backlog. See OPEN-ITEMS for COS v2 wiring._", ""]
    if not model["queue"]:
        lines.append("- (empty) no decisions, human-only items, or backlog "
                     "entries across the manifest.")
    for q in model["queue"]:
        sp = stale_prefix(now, q["as_of"], STALE_DECISIONS)
        lines.append("- %s[%s] %s %s"
                     % (sp, q["kind"], q["summary"], tag(q["source"], q["as_of"])))
    lines.append("")
    return "\n".join(lines)


def render_scan(model, now):
    lines = ["## 4. Scan / priorities feed",
             "_scan-consumption-interface-v1 (weights / scan-targets / "
             "suppression / optional intentions); PR #150 ratified -- real "
             "consumption, each artifact validated independently_", ""]
    scan = model["scan"]
    if not scan.get("present"):
        lines.append("- no scan artifacts found at %s %s"
                     % (scan.get("path") or "(no --scan-consumption-dir)",
                        tag(scan.get("reason", "absent"), scan.get("as_of", ""))))
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
    body += render_topology(model, now) + "\n"
    body += render_surfaces(model, now) + "\n"
    body += render_queue(model, now) + "\n"
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
               cap_tokens=HARD_CAP_TOKENS):
    """Assemble (markdown, sidecar_dict). ``now`` is injectable for determinism."""
    model = collect(now, manifest, backlog, surfaces_cache_dir, scan_dir)

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
        "sections": {
            "topology": {"provenance": model["manifest_provenance"],
                         "as_of": model["manifest_as_of"],
                         "entries": model["topology"]},
            "surfaces": model["surfaces"],
            "decision_queue": {"projection": True, "absorbs_ratification": False,
                               "items": model["queue"]},
            "scan": model["scan"],
        },
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
        description="D30 read-only boot-pack assembler (Macro Seat Artifact 3).")
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
                                 args.scan_consumption_dir)
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
