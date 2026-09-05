#!/usr/bin/env python3
"""boot-digest.py -- D30-class slim boot digest over the assembled boot pack.

Emits ``state/BOOT-DIGEST.md``: a few-hundred-token attention summary a fresh
session can absorb at boot without loading the ~3k-token pack. The digest is a
POINTER, not a replacement -- it names what is degraded, what is open, and which
repos need attention, then points at ``state/BOOT-PACK.md`` for everything else.

Class D30 (read-only, stateless, non-enforcing, report-only, MANUALLY invoked --
never wired into CI/hooks/gates). The SessionStart hook does not contradict that:
the hook READS the digest file this generator already wrote, and never invokes
this generator -- the only caller is the morning-pack wrapper Anthony armed by
hand.

Stdlib only, Python 3.9+. NO git operations beyond the gitignore write-guard, NO
network, and exactly one read-only shell-out: ``launchctl list`` (see
``launchctl_warnings`` -- this generator never loads, unloads, or kickstarts a
job). It CONSUMES ``state/boot-pack.json`` (schema ``boot-pack/v1``) and never
writes, rewrites, or reshapes the pack -- its sole consumer archived 2026-08-04;
the schema is a revival contract. The only file this writes is its ``--out`` path
(written atomically via a sibling ``.tmp`` + ``os.replace``, so the hook never
reads a half-written digest).

The digest is ADVISORY STATE, NOT LAW: SAGA doctrine, repo CLAUDE.md, and
merge-is-Anthony's bind regardless of digest content.

Degrade-not-abort: a missing, unreadable, unparseable, or wrong-schema pack
writes NOTHING (any existing digest is left untouched -- the SessionStart hook
already flags a stale one) and exits 0. Only a refused write path is a hard
failure.

Content, in order: DEFECTS (the register's one summary line plus up to three of
Anthony's own items -- the first thing any session reads, and never shed; led by
a "register step FAILED" line when the morning job's own marker is newer than
the status file), NIGHTLY (one line for what the 02:30 fix lane did, or that it
is not armed -- the digest is the only place a fresh session would see it),
header (pack timestamp + age, ``STALE (>36h)`` when old),
WARNINGS (count + one compressed line each, led by this run's own launchd
probe), OPEN ITEMS (count + first sentence each), ATTENTION (only repos behind
their origin default branch or carrying decision-queue items -- healthy repos
are skipped entirely), an optional daily-brief pointer, an optional IMPRINT
summary, and the full-pack pointer.

Staleness is LOUD (2026-08-08 audit: silent staleness is this system's dominant
failure mode -- every gate checked one file's own mtime, so a dead upstream job
read as a healthy quiet one). Three additions carry that: an imprint export that
parses to zero records warns instead of vanishing, an export older than 26h says
so (with the live store's own last-write time beside it, making export-vs-store
lag visible), and any ``com.anthonyflores.*`` / ``com.saga.*`` launchd job whose
last run exited nonzero is named in WARNINGS.

Hard cap: 500 tokens (chars/4 estimate, the assembler's idiom). Overflow sheds
lowest-priority content first -- open items, then warnings, then attention lines
-- each with an explicit ``[truncated]`` marker. The DEFECTS lines, the header,
the daily-brief pointer, and the full-pack pointer are NEVER shed.

The IMPRINT section (audit P1-1) sits OUTSIDE the 500-token cap with its own
independent byte cap. It summarizes ``state/imprint-store.md`` -- the bulk
markdown export morning-pack.sh writes from the imprint store. The export is
megabytes of URN-dense raw records (unfiltered by the pending capture-filter),
so the digest deliberately carries a compact SUMMARY (approximate record count
+ newest capture timestamp) and the absolute pointer, never a head-slice of the
content. Selection policy for actual record content at boot is an open ruling
(audit R2); until it lands, the export file is the grep-on-demand channel.

Determinism: wall-clock enters only through the pack-age arithmetic, and
``build_digest`` takes an injectable ``now``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

PACK_SCHEMA = "boot-pack/v1"

HARD_CAP_TOKENS = 500

# Independent cap for the IMPRINT section (mirrors session-digest-hook.sh's
# byte-cap idiom). The summary is ~5 lines in practice; the cap is a guard
# against a pathological export, not a target.
IMPRINT_CAP_BYTES = 4000

# A pack older than this is stale enough that the session must not trust it
# (the daily LaunchAgent runs at 05:45, so >36h means a missed run).
STALE_PACK = 36 * 3600

# The imprint export is rewritten by the same 05:45 run, so anything past a day
# plus the slack a late run needs means that step failed and the summary below
# describes yesterday's judgment.
STALE_EXPORT = 26 * 3600

# The defect status is rewritten by the same 05:45 run. The boot pack marks its
# summary line STALE past 36h; the digest rebuilds that line from the same
# counts, so it must carry the same mark or a session reads old numbers as new.
STALE_DEFECTS = 36 * 3600
# 36h is far too patient on its own: the register step runs seconds before the
# assembler in the same wrapper, so a status file more than an hour behind the
# pack it was folded into is a step that failed or never ran. Below the stale
# threshold the line carries its own age instead of nothing at all.
DEFECT_LAG = 3600
DEFENSE_LINES_SENTINEL = object()

# The days-open figure at which tools/hooks/defect_gate.py starts refusing Task,
# Agent and Workflow calls on this Mac. Duplicated, not imported (the hook is
# also installed outside this repo and must stay import-free) -- the drift test
# in test_boot_digest.py reads the hook's own constant and fails if they part.
# The digest names the number because a session that reads "oldest 6d" has no
# way to know that 7 is where every subagent stops.
GATE_BLOCKS_AT_DAYS = 7

# The marker tools/morning-pack.sh writes when the register step itself fails.
# It sits beside the status file, so the path is derived from whatever status
# file the pack names rather than hardcoded.
DEFECTS_FAILED_SUFFIX = ".FAILED"

# The live store the markdown export is dumped from. Existence-guarded and
# READ-ONLY (mtime only): this box has it, CI does not, and its absence costs
# only the export-vs-store lag line.
IMPRINT_DB = os.path.expanduser("~/.local/share/imprint/anthony/imprint.db")

# launchd labels this system owns. A third-party updater's failure is noise; a
# failure under these prefixes is a dead automation the session must hear about.
LAUNCHD_PREFIXES = ("com.anthonyflores.", "com.saga.")

# One compressed line per warning / open item; long lines are clipped, never
# wrapped (a digest line is a pointer, the pack holds the full text).
MAX_LINE_CHARS = 120

TITLE = "# FULLY AWARE -- BOOT DIGEST (advisory, not law)"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Where tools/nightly-fix.py leaves one markdown run log per attempt, named
# <YYYYMMDD>-<id>.md. The lane runs at 02:30 and the digest is what a fresh
# session actually reads, so without this the night is invisible: a pull request
# opened, a Codex failure, a failed check and a lane that never ran all produced
# an identical digest.
NIGHTLY_DIR = os.path.join(_REPO_ROOT, "state", "nightly-fix")

# Where plan-status's ledger.py export leaves the generated lane snapshot. It
# lives OUTSIDE this repo (the ledgers are estate-wide, not fully-aware's), and
# the digest reads it directly rather than through the pack: a session that
# reads only the digest still needs to see which lanes are stalled and what
# waits on Anthony.
PLANS_SNAPSHOT = os.path.expanduser("~/code/state/plans-snapshot.json")

# The digest is read in SESSIONS, whose cwd is arbitrary (folder = activity, but
# not this folder). A repo-relative pointer resolves against the wrong directory
# there, so both the pack pointer and the [truncated] markers name the pack by
# ABSOLUTE path, derived from this file's repo root.
PACK_MD = os.path.join(_REPO_ROOT, "state", "BOOT-PACK.md")
PACK_POINTER = "Full pack: %s" % PACK_MD

# Decision-queue items carry a "<feed>:<environment>" source; these feeds are
# repo/system-attributable (a ratification-backlog item is not). The
# adjudication feed attributes to "atlas-v2" -- a system, not a repo, but the
# ATTENTION list is exactly where its unactioned queues belong.
_REPO_FEEDS = ("next-session:", "surface:", "adjudication:")

# Defects (pack section 0) lead the digest: the count line, then at most this
# many of Anthony's own items. These lines are NEVER shed -- see _shed_one.
DEFECT_LINES = 3

_SENTENCE_RE = re.compile(r"^(.*?[.!?])(?:\s|$)", re.DOTALL)
_WS_RE = re.compile(r"\s+")


class DigestError(Exception):
    """A hard digest failure (nonzero exit; not a degrade)."""


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
def est_tokens(text):
    return (len(text) + 3) // 4


def compress(text, limit=MAX_LINE_CHARS):
    """Collapse a free-text blob to one clipped single-space line."""
    line = _WS_RE.sub(" ", str(text)).strip()
    if len(line) > limit:
        line = line[:limit - 3].rstrip() + "..."
    return line


def first_sentence(text):
    """The first sentence of a free-text blob, compressed (whole blob if none)."""
    line = _WS_RE.sub(" ", str(text)).strip()
    m = _SENTENCE_RE.match(line)
    return compress(m.group(1) if m else line)


def _parse_iso(token):
    """Parse an ISO-8601 timestamp to aware UTC, or None. Never raises."""
    if not isinstance(token, str) or not token.strip():
        return None
    try:
        dt = datetime.datetime.fromisoformat(token.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _ago(seconds):
    """Coarse relative age: ``42m ago`` / ``3h ago`` / ``2d ago``.

    A clock-skewed (future) mtime reads as ``0m ago`` rather than a negative.
    """
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)


def _rel(path):
    """Render an in-repo path repo-relative; anything else stays absolute."""
    ap = os.path.abspath(path)
    try:
        if os.path.commonpath([ap, _REPO_ROOT]) == _REPO_ROOT:
            return os.path.relpath(ap, _REPO_ROOT)
    except ValueError:
        pass
    return ap


# --------------------------------------------------------------------------- #
# inputs -- the pack is READ ONLY; its schema is a revival contract
# --------------------------------------------------------------------------- #
def load_pack(path):
    """Load the boot-pack sidecar, or None with a reason on any problem."""
    if not os.path.isfile(path):
        return None, "no boot pack at %s" % path
    try:
        with open(path, "r", encoding="utf-8") as fh:
            pack = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "unreadable boot pack at %s: %s" % (path, exc)
    if not isinstance(pack, dict):
        return None, "boot pack at %s is not an object" % path
    if pack.get("schema") != PACK_SCHEMA:
        return None, ("boot pack at %s is schema %r, expected %s"
                      % (path, pack.get("schema"), PACK_SCHEMA))
    return pack, None


# A record entry is a top-level bullet whose urn is immediately followed by the
# bracketed metadata run (``** [captured · ... valid ...]``). Bare urn mentions
# inside captured record BODIES lack the bracket and must not count. No
# per-section attribution: record bodies embed arbitrary markdown, including
# ``## ...`` headings indistinguishable from the export's own section headings,
# so any section split would misattribute records (verified against the real
# export). Total + newest is what survives that data.
_IMPRINT_ENTRY_RE = re.compile(r"^- \*\*urn:imprint:[a-z]+:[0-9a-f-]+\*\* \[")
_IMPRINT_VALID_RE = re.compile(r"valid (\d{4}-\d{2}-\d{2})")


def load_imprint(path):
    """Compact summary of the imprint markdown export, or None.

    Returns ``{"records": n, "newest": "YYYY-MM-DD"|None, "bytes": n,
    "mtime": epoch, "path": abspath}``. The count is approximate by design: a
    captured record body that QUOTES other records (handoffs, past exports)
    contributes their bullets too. The digest says "~N records" accordingly.

    Degrades to None only on a MISSING or unreadable file. A file that is
    present but yields zero records comes back with ``records == 0``, not None:
    that is the export drifting away from the line format above, and the
    2026-08-08 audit found this exact case silently deleting the whole section
    -- an unparseable export must be loud (see ``imprint_lines``).
    """
    if not path or not os.path.isfile(path):
        return None
    records = 0
    newest = None
    try:
        st = os.stat(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if _IMPRINT_ENTRY_RE.match(raw):
                    records += 1
                    v = _IMPRINT_VALID_RE.search(raw)
                    if v and (newest is None or v.group(1) > newest):
                        newest = v.group(1)
    except OSError:
        return None
    return {"records": records, "newest": newest, "bytes": st.st_size,
            "mtime": st.st_mtime, "path": os.path.abspath(path)}


def live_store_mtime(path=IMPRINT_DB):
    """Last-write time of the live imprint store, or None if unreadable.

    Never raises: the store is a machine-local file the digest merely peeks at,
    so an absent one (CI, another box) drops the lag line and nothing else.
    """
    try:
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            return None
        return os.path.getmtime(path)
    except OSError:
        return None


def imprint_lines(summary, now=None, db_path=IMPRINT_DB):
    """Render the IMPRINT section lines from a load_imprint summary.

    Loud on both failure modes the audit found silent: an export that parses to
    zero records (line-format drift), and an export the 05:45 run did not
    refresh. The live store's own last-write time rides alongside so
    export-vs-store lag is visible rather than inferred. ``now`` is injectable
    for determinism (the module docstring's only wall-clock rule).
    """
    if not summary:
        return []
    now_ts = (now or datetime.datetime.now(datetime.timezone.utc)).timestamp()
    lines = ["IMPRINT (captured judgment -- bulk channel):"]
    if summary["records"]:
        lines.append("- ~%d records; newest capture %s"
                     % (summary["records"], summary["newest"] or "unknown"))
    else:
        lines.append("- WARNING: imprint export present but unparseable "
                     "(line-format drift?)")
    age = now_ts - summary["mtime"]
    if age > STALE_EXPORT:
        lines.append("- WARNING: imprint export STALE (>26h at digest generation); exported %s -- 05:45 export may have failed"
                     % datetime.datetime.fromtimestamp(summary["mtime"], datetime.timezone.utc).isoformat())
    lines.append("- full export: %s (%.1f MB; grep on demand, `imprint history "
                 "<urn>` for provenance)"
                 % (summary["path"], summary["bytes"] / (1024.0 * 1024.0)))
    live = live_store_mtime(db_path)
    if live is not None:
        lines.append("- live store last written %s"
                     % datetime.datetime.fromtimestamp(live, datetime.timezone.utc).isoformat())
    lines.append("")
    # Independent byte cap (defensive; the summary is ~5 lines in practice).
    # Shedding from the tail keeps the warnings, which are the point.
    while len("\n".join(lines).encode("utf-8")) > IMPRINT_CAP_BYTES \
            and len(lines) > 1:
        lines.pop()
    return lines


def parse_launchctl(text):
    """``[(label, exit_code), ...]`` for OUR launchd jobs that last exited nonzero.

    Pure, so the raw text is the whole contract and tests inject canned output.
    ``launchctl list`` prints a header row then three TAB-separated columns --
    PID, last exit status, label -- where ``-`` means "not applicable" (not
    running / never exited). A non-numeric status is not a failure signal, so
    it is skipped rather than guessed at.
    """
    failing = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        _pid, status, label = (p.strip() for p in parts)
        if not label.startswith(LAUNCHD_PREFIXES):
            continue
        try:
            code = int(status)
        except ValueError:
            continue  # '-' (never exited), or the header row's "Status"
        if code != 0:
            failing.append((label, code))
    return failing


def _launchctl_list():
    """``launchctl list`` output. READ-ONLY -- never load/unload/kickstart."""
    return subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                          timeout=10, check=True).stdout


def launchctl_warnings(runner=None):
    """WARNING lines for our launchd jobs whose last run exited nonzero.

    A dead scheduled job is invisible to every mtime-based freshness check --
    its artifacts simply stop moving -- so the digest asks launchd directly.
    Returns [] when launchctl is absent (Linux CI) or the probe fails: a digest
    that cannot reach launchd says nothing about launchd rather than guessing.
    """
    if runner is None:
        if not shutil.which("launchctl"):
            return []
        runner = _launchctl_list
    try:
        text = runner()
    except Exception:  # noqa: BLE001 - a dead probe degrades, never aborts
        return []
    return ["- WARNING: launchd job %s last exit %d" % (label, code)
            for label, code in parse_launchctl(text)]


def load_daily_brief(path):
    """First non-empty, non-heading line of the daily-scan brief, or None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                return compress(line)
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
def queue_counts(pack):
    """Decision-queue item counts per repo-attributable environment."""
    counts = {}
    queue = (pack.get("sections") or {}).get("decision_queue") or {}
    for item in queue.get("items") or []:
        src = item.get("source") if isinstance(item, dict) else None
        if not isinstance(src, str):
            continue
        for feed in _REPO_FEEDS:
            if src.startswith(feed):
                env = src[len(feed):].strip()
                if env:
                    counts[env] = counts.get(env, 0) + 1
                break
    return counts


def defects_summary_line(counts, threshold=None):
    """The ONE defect line, built from the pack sidecar's counts block.

    Deliberately duplicated in verify-defects.py and assemble-boot-pack.py
    rather than imported: the three tools couple by the defect-status/v1 data
    contract only.

    The digest's copy says one thing the other two do not: where the defect gate
    trips. "oldest 6d" told a session nothing about the machine-wide block that
    starts at 7d, so the day it arrives was always a surprise. The line now
    names the trip point, and warns on the eve of it.
    """
    counts = counts or {}
    threshold = GATE_BLOCKS_AT_DAYS if threshold is None else int(threshold)
    p0 = counts.get("P0") or {}
    p1 = counts.get("P1") or {}
    p2 = counts.get("P2") or {}
    p0_open = int(p0.get("open") or 0)
    oldest_days = int(p0.get("oldest_days") or 0)
    oldest = ""
    warning = ""
    if p0_open:
        oldest = " (oldest %dd; gate blocks at %dd)" % (oldest_days, threshold)
        if oldest_days >= threshold:
            warning = " — gate is blocking now"
        elif oldest_days == threshold - 1:
            warning = " — gate arms tomorrow"
    owners = counts.get("open_by_owner") or {}
    return ("DEFECTS -- P0: %d%s · P1: %d · P2: %d · "
            "fixed since yesterday: %d · yours today: %d · "
            "no real check yet: %d · accepted: %d%s"
            % (p0_open, oldest, int(p1.get("open") or 0), int(p2.get("open") or 0),
               int(counts.get("fixed_since_last") or 0),
               int(owners.get("anthony") or 0),
               int(counts.get("provisional") or 0),
               int(counts.get("accepted") or 0), warning))


def defect_items(section):
    """``{id: record}`` for the defect items, so a line can carry its symptom.

    The pack sidecar records ids and counts, not per-item prose, so the detail
    comes from the status file the sidecar NAMES (``status_path``) -- read-only,
    and optional: an unreadable or absent one costs the symptom text, never the
    count line. A sidecar that inlines ``items`` (a future pack) is preferred.
    """
    inline = section.get("items")
    records = inline if isinstance(inline, list) else None
    if records is None:
        path = section.get("status_path")
        if not isinstance(path, str) or not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        records = data.get("items") if isinstance(data, dict) else None
        if not isinstance(records, list):
            return {}
    return {r["id"]: r for r in records
            if isinstance(r, dict) and isinstance(r.get("id"), str)}


def register_failed_line(section):
    """``"DEFECTS -- register step FAILED at <time>"``, or None.

    tools/morning-pack.sh writes a marker beside the status file when the
    register step itself fails. The pack is still assembled and still looks
    fresh, so without this the only trace is a warning in a launchd log while
    the digest shows yesterday's counts as if they were today's.

    Only a marker NEWER than the status file counts: an old marker beside a
    status file that has since been rewritten is a failure already recovered.
    """
    path = section.get("status_path")
    if not isinstance(path, str) or not path:
        return None
    marker = os.path.splitext(path)[0] + DEFECTS_FAILED_SUFFIX
    try:
        marker_mtime = os.path.getmtime(marker)
    except OSError:
        return None
    try:
        if os.path.getmtime(path) >= marker_mtime:
            return None
    except OSError:
        pass                     # no status file at all: the marker stands
    when = ""
    try:
        with open(marker, "r", encoding="utf-8") as fh:
            text = fh.read(400)
        for token in text.split():
            if token.startswith("at="):
                when = token[3:]
                break
    except OSError:
        text = ""
    if not when:
        when = datetime.datetime.fromtimestamp(marker_mtime).isoformat(
            timespec="seconds")
    return "DEFECTS -- register step FAILED at %s" % when


def defect_lines(now, pack, limit=DEFENSE_LINES_SENTINEL):
    """The digest's opening lines: the defect count, then Anthony's items today.

    A pack with no ``sections.defects`` (anything assembled before the register
    existed) contributes NOTHING -- an old pack must not be made to say zero
    defects. A pack that carries the section but no counts says so out loud.
    A status older than 36h is prefixed STALE(<hours>h), exactly as the boot
    pack prefixes it; a stamp that cannot be parsed is marked AS_OF-UNPARSEABLE;
    and a status merely older than the PACK it was folded into is prefixed
    AS_OF(<hours>h), because a register step that failed leaves a fresh pack
    carrying old counts. A failed register step leads with its own line.
    """
    if limit is DEFENSE_LINES_SENTINEL:
        limit = DEFECT_LINES
    section = (pack.get("sections") or {}).get("defects")
    if not isinstance(section, dict):
        return []
    counts = section.get("counts")
    if not isinstance(counts, dict) or not counts:
        return ["DEFECTS -- no status yet (run tools/verify-defects.py)"]
    prefix = ""
    stamp = section.get("generated_at")
    if stamp is not None:
        gen = _parse_iso(stamp)
        if gen is None:
            prefix = "AS_OF-UNPARSEABLE "
        else:
            age = (now - gen).total_seconds()
            pack_gen = _parse_iso(pack.get("generated_at"))
            if age > STALE_DEFECTS:
                prefix = "STALE(%dh) " % (age // 3600)
            elif (pack_gen is not None
                  and (pack_gen - gen).total_seconds() > DEFECT_LAG):
                prefix = "AS_OF(%dh) " % (age // 3600)
    lines = []
    failed = register_failed_line(section)
    if failed:
        lines.append(failed)
    lines.append(prefix + defects_summary_line(counts))
    yours = [i for i in (section.get("yours_today") or []) if isinstance(i, str)]
    by_id = defect_items(section)

    for item_id in yours[:limit]:
        rec = by_id.get(item_id) or {}
        symptom = rec.get("symptom")
        days = rec.get("days_open")
        # days_open arrives straight from the status file, which the pack
        # assembler's own item guard never sees. A string "3" would raise
        # TypeError on the %d below and take the WHOLE digest down; True would
        # quietly print as "1d". Only a real, non-negative count earns the
        # detailed line -- anything else falls back to the id-only one.
        if not isinstance(days, int) or isinstance(days, bool) or days < 0:
            days = None
        if symptom and days is not None:
            lines.append(compress("- %s — %dd — %s" % (item_id, days, symptom)))
        else:
            lines.append("- %s — yours today" % item_id)
    return lines


_NIGHTLY_HEADER_RE = re.compile(r"^#\s*Nightly fix\s+(\S+)")
_NIGHTLY_OUTCOME_RE = re.compile(r"^Outcome:\s*(.+?)\s*$", re.MULTILINE)
# The lane writes four files per attempt under the same <date>-<id> stem; only
# the bare .md is the plain-English run log.
_NIGHTLY_SKIP = (".prompt.md", ".last.md")


def nightly_line(state_dir=None):
    """ONE line about last night's fix lane, for the file a session actually reads.

    ``NIGHTLY -- lane not armed`` when the run-log folder does not exist yet,
    ``NIGHTLY -- no run recorded`` when it exists but holds no run log, and
    ``NIGHTLY -- <id>: <outcome>`` from the newest log otherwise. The outcome is
    the lane's own word for what happened (pr-opened, pr-check-failed,
    codex-failed, no-changes, ...), quoted rather than reinterpreted.

    Read-only and never fatal: an unreadable log costs the outcome, not the line.
    """
    directory = NIGHTLY_DIR if state_dir is None else state_dir
    if not os.path.isdir(directory):
        return "NIGHTLY -- lane not armed"
    newest = None
    try:
        names = os.listdir(directory)
    except OSError:
        return "NIGHTLY -- no run recorded"
    for name in names:
        if not name.endswith(".md") or name.endswith(_NIGHTLY_SKIP):
            continue
        path = os.path.join(directory, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if newest is None or (mtime, name) > (newest[0], newest[1]):
            newest = (mtime, name, path)
    if newest is None:
        return "NIGHTLY -- no run recorded"
    _mtime, name, path = newest
    item_id = name[:-len(".md")]
    outcome = ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read(4000)
    except OSError:
        text = ""
    header = _NIGHTLY_HEADER_RE.search(text)
    if header:
        item_id = header.group(1)
    elif "-" in item_id:
        item_id = item_id.split("-", 1)[1]
    found = _NIGHTLY_OUTCOME_RE.search(text)
    if found:
        outcome = found.group(1)
    return "NIGHTLY -- %s: %s" % (item_id, outcome or "outcome not recorded")


def plans_line(path=None):
    """ONE line about where every registered plan lane stands.

    Read straight from the snapshot ledger.py export writes, not from the pack:
    the digest is what a fresh session actually reads, and a stalled lane or a
    lane waiting on Anthony is invisible everywhere else it looks.

    ``PLANS -- 5 lanes: 1 stalled (x idle 9d) - 3 waiting on Anthony - 12
    unregistered plan files``, with only the nonzero parts. Never fatal: a
    missing or unreadable snapshot costs the counts, not the line.
    """
    path = PLANS_SNAPSHOT if path is None else path
    if not os.path.isfile(path):
        return "PLANS -- no snapshot (ledger.py export writes it)"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return "PLANS -- snapshot unreadable"

    lanes = data.get("lanes")
    lanes = [l for l in lanes if isinstance(l, dict)] if isinstance(lanes, list) else []
    unregistered = data.get("unregistered_plan_files")
    unregistered = len(unregistered) if isinstance(unregistered, list) else 0

    stalled = [l for l in lanes if l.get("health") == "stalled"]
    waiting = [l for l in lanes if l.get("health") == "waiting"]
    complete = [l for l in lanes if l.get("health") == "complete"]

    def _idle(lane):
        days = lane.get("idle_days")
        return days if isinstance(days, int) and not isinstance(days, bool) else 0

    segments = []
    if stalled:
        worst = max(stalled, key=_idle)
        segments.append("%d stalled (%s idle %dd)"
                        % (len(stalled), worst.get("name", "?"), _idle(worst)))
    if waiting:
        segments.append("%d waiting on Anthony" % len(waiting))
    if complete:
        segments.append("%d complete" % len(complete))
    if unregistered:
        segments.append("%d unregistered plan files" % unregistered)

    if not lanes:
        head = "PLANS -- no registered lanes"
        return compress(head + (" · " + segments[0] if segments else ""))
    if not segments:
        return compress("PLANS -- %d lanes: all active" % len(lanes))
    return compress("PLANS -- %d lanes: %s" % (len(lanes), " · ".join(segments)))


def attention_lines(pack):
    """One line per repo that is behind its origin default branch or carries queue items.

    Healthy repos are skipped ENTIRELY -- the digest exists to route attention,
    and a clean repo needs none. A degraded behind-count is not a number, so it
    never counts as behind; the pack's own WARNINGS already carry it.
    """
    counts = queue_counts(pack)
    lines = []
    seen = set()
    for surface in (pack.get("sections") or {}).get("surfaces") or []:
        if not isinstance(surface, dict):
            continue
        env = surface.get("environment") or "?"
        seen.add(env)
        behind = surface.get("behind_origin_main")
        behind = behind if isinstance(behind, int) and not isinstance(behind, bool) else 0
        pending = counts.get(env, 0)
        if behind <= 0 and pending <= 0:
            continue
        bits = []
        if behind > 0:
            bits.append("%d behind origin default" % behind)
        if pending > 0:
            bits.append("%d decision-queue item(s)" % pending)
        lines.append("- %s: %s" % (env, ", ".join(bits)))
    # Queue items can name an environment with no surface in the pack (a
    # next-session feed for a repo outside the manifest) -- never drop those.
    for env in sorted(set(counts) - seen):
        lines.append("- %s: %d decision-queue item(s)" % (env, counts[env]))
    return lines


def default_taste_health():
    config_path = os.environ.get("IMPRINT_CONFIG", os.path.expanduser("~/.config/imprint/config.json"))
    try:
        with open(config_path, encoding="utf-8") as fh:
            config = json.load(fh)
        return os.path.join(config["data_root"], config["operator_slug"], "macroseat", "distill-health.json")
    except (OSError, ValueError, KeyError, TypeError):
        return None


def taste_health_line(path, now):
    """Content-free semantic health; terminal errors survive an empty worker batch."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            health = json.load(fh)
        keys = ("errors", "retry_exhausted", "transcript_missing", "queue_bad_records", "batch_count", "batch_errors")
        if not isinstance(health, dict) or health.get("schema") != "taste-distiller-health/v1":
            raise ValueError("schema")
        if any(type(health.get(k)) is not int or health[k] < 0 for k in keys):
            raise ValueError("counts")
        at = _parse_iso(health.get("generated_at"))
        if at is None or health.get("status") not in ("healthy", "degraded"):
            raise ValueError("state")
        if health["retry_exhausted"] > health["errors"] or health["batch_errors"] > health["batch_count"]:
            raise ValueError("counts")
        stale = (now - at).total_seconds() > 45 * 60 or (at - now).total_seconds() > 300
        degraded = stale or health["status"] == "degraded" or health.get("failure") or any(
            health[k] for k in ("errors", "retry_exhausted", "transcript_missing", "queue_bad_records"))
        return ("TASTE: %s; errors %d (%d retry-exhausted), missing %d, bad queue %d; observed %s%s%s"
                % ("DEGRADED" if degraded else "healthy", health["errors"], health["retry_exhausted"],
                   health["transcript_missing"], health["queue_bad_records"], health["generated_at"],
                   "; STALE (>45m or future clock)" if stale else "",
                   "; worker state unavailable" if health.get("failure") else ""))
    except (OSError, ValueError, TypeError, OverflowError):
        return "TASTE: DEGRADED; health unavailable or malformed (no success inferred)"


def decay_line(pack):
    decay = (pack.get("sections") or {}).get("decay")
    if not decay:
        return None
    counts = decay.get("state_counts")
    keys = ("total", "pending", "needs_update", "deferred", "reviewed")
    if not decay.get("present") or not isinstance(counts, dict) or any(
            type(counts.get(k)) is not int or counts[k] < 0 for k in keys):
        return "DECAY: weekly feed unavailable; pending work unknown"
    return ("DECAY: %d total; %d pending, %d needs update, %d deferred, %d reviewed; weekly; observed %s%s"
            % tuple([counts[k] for k in keys] + [decay.get("as_of", "unknown"),
                    " STALE (work retained)" if decay.get("freshness") != "fresh" else ""]))


def collect(now, pack, daily_brief=None, daily_brief_path=None,
            extra_warnings=None, plans_snapshot=None, taste_health=None):
    """Fold the pack into the digest model (plain lists, shed-able).

    ``extra_warnings`` are pre-rendered lines from THIS run's own probes (the
    launchd exit-code sweep), not from the pack. They lead the section: the
    shed loop pops from the tail, so a locally-observed dead job outlives the
    pack's own warnings under cap pressure.
    """
    generated_at = pack.get("generated_at") or ""
    gen = _parse_iso(generated_at)
    age_hours = None
    if gen is not None:
        age_hours = max(0, int((now - gen).total_seconds()) // 3600)
    attention = attention_lines(pack)
    warnings = list(extra_warnings or []) + \
        ["- " + compress(w) for w in pack.get("warnings") or []]
    return {
        "defects": defect_lines(now, pack),
        "nightly": nightly_line(),
        "taste_health": taste_health,
        "decay": decay_line(pack),
        "plans": plans_line(plans_snapshot),
        "generated_at": generated_at if gen is not None else "unknown",
        "age_hours": age_hours,
        "stale": gen is not None and (now - gen).total_seconds() > STALE_PACK,
        "warnings": warnings,
        "warnings_total": len(warnings),
        "warnings_truncated": 0,
        "open_items": ["- " + first_sentence(o) for o in pack.get("open_items") or []],
        "open_items_total": len(pack.get("open_items") or []),
        "open_items_truncated": 0,
        "attention": attention,
        "attention_total": len(attention),
        "attention_truncated": 0,
        "daily_brief": daily_brief,
        "daily_brief_path": daily_brief_path,
    }


def _shed_one(model):
    """Shed one line from the lowest-priority section that still has some.

    Priority (last to go): attention lines route today's work; warnings say the
    pack itself is degraded; open items are standing design gaps. The DEFECTS
    lines, the NIGHTLY line, the header, and both pointers are never shed -- the
    defect count is the whole point of the first line, and the night's outcome
    is reachable nowhere else a fresh session looks.
    """
    for key in ("open_items", "warnings", "attention"):
        if model[key]:
            model[key].pop()
            model[key + "_truncated"] += 1
            return True
    return False


def _section(lines, title, total, truncated):
    if not total:
        return []
    out = ["%s (%d):" % (title, total)]
    out.extend(lines)
    if truncated:
        out.append("- [truncated] %d more -- see %s" % (truncated, PACK_MD))
    out.append("")
    return out


def render_md(model, imprint=None):
    """Render the digest markdown from the model.

    ``imprint`` is a pre-rendered line list (see ``imprint_lines``); it sits
    between the daily-brief line and the pack pointer.
    """
    header = "Pack generated: %s" % model["generated_at"]
    if model["stale"]:
        header += " STALE (>36h)"

    lines = [TITLE]
    if model.get("defects"):
        lines.extend(model["defects"])
    if model.get("nightly"):
        lines.append(model["nightly"])
    if model.get("plans"):
        lines.append(model["plans"])
    if model.get("taste_health"):
        lines.append(model["taste_health"])
    if model.get("decay"):
        lines.append(model["decay"])
    if model.get("defects") or model.get("nightly") or model.get("plans"):
        lines.append("")
    lines.extend([header, ""])
    lines.extend(_section(model["warnings"], "WARNINGS",
                          model["warnings_total"], model["warnings_truncated"]))
    lines.extend(_section(model["open_items"], "OPEN ITEMS",
                          model["open_items_total"], model["open_items_truncated"]))
    lines.extend(_section(model["attention"], "ATTENTION",
                          model["attention_total"], model["attention_truncated"]))
    if model["daily_brief"]:
        lines.append("Daily brief: %s (%s)"
                     % (model["daily_brief"],
                        os.path.abspath(model["daily_brief_path"])))
    if imprint:
        if model["daily_brief"]:
            lines.append("")
        lines.extend(imprint)
    lines.append(PACK_POINTER)
    return "\n".join(lines) + "\n"


def build_digest(now, pack, daily_brief=None, daily_brief_path=None,
                 cap_tokens=HARD_CAP_TOKENS, imprint=None,
                 extra_warnings=None, plans_snapshot=None, taste_health=None):
    """Build the digest markdown. ``now`` is injectable for determinism.

    The 500-token shed loop runs WITHOUT the imprint section: the IMPRINT
    summary rides outside the token cap on its own byte cap (see module
    docstring), so it must never pressure the shed loop into dropping
    warnings/attention lines that the cap exists to protect.
    """
    model = collect(now, pack, daily_brief, daily_brief_path, extra_warnings,
                    plans_snapshot=plans_snapshot, taste_health=taste_health)
    md = render_md(model)
    while est_tokens(md) > cap_tokens:
        if not _shed_one(model):
            break  # header + pointers alone are already over cap; render honestly
        md = render_md(model)
    if imprint:
        md = render_md(model, imprint=imprint)
    return md


# --------------------------------------------------------------------------- #
# gitignore guard (state/ outputs must be gitignored) -- assembler pattern
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
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    proc = subprocess.run(
        ["git", "-C", _REPO_ROOT, "check-ignore", "--quiet", out_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        raise DigestError(
            "refusing to write %s: digest output must be gitignored (state/ is "
            "local-only). Add it to .gitignore or pass an --out path outside "
            "the repo." % out_path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _default(*parts):
    return os.path.join(_REPO_ROOT, *parts)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="D30 read-only slim boot digest over state/boot-pack.json.")
    ap.add_argument("--pack", default=_default("state", "boot-pack.json"),
                    help="boot-pack/v1 sidecar to read (never written)")
    ap.add_argument("--daily-scan",
                    default=_default("state", "daily-scan", "LATEST.md"),
                    help="optional daily-scan brief to point at (absent -> no line)")
    ap.add_argument("--imprint",
                    default=_default("state", "imprint-store.md"),
                    help="imprint markdown export to summarize (absent -> no "
                         "IMPRINT section)")
    ap.add_argument("--plans-snapshot", default=PLANS_SNAPSHOT,
                    help="plans-snapshot.json written by ledger.py export "
                         "(missing -> absence line, never fatal)")
    ap.add_argument("--taste-health", default=default_taste_health(),
                    help="content-free taste worker health (configured missing feed degrades)")
    ap.add_argument("--out", default=_default("state", "BOOT-DIGEST.md"))
    ap.add_argument("--stdout", action="store_true",
                    help="write the digest to stdout (skips the file write)")
    ap.add_argument("--cap-tokens", type=int, default=HARD_CAP_TOKENS,
                    help="hard cap, chars/4 estimate (default %d)" % HARD_CAP_TOKENS)
    args = ap.parse_args(argv)

    pack, reason = load_pack(args.pack)
    if pack is None:
        # Degrade-not-abort: no pack, no digest, nothing overwritten, exit 0.
        sys.stderr.write("boot-digest: skipped -- %s\n" % reason)
        return 0

    now = datetime.datetime.now(datetime.timezone.utc)
    md = build_digest(now, pack,
                      daily_brief=load_daily_brief(args.daily_scan),
                      daily_brief_path=args.daily_scan,
                      cap_tokens=args.cap_tokens,
                      imprint=imprint_lines(load_imprint(args.imprint), now=now),
                      extra_warnings=launchctl_warnings(),
                      plans_snapshot=args.plans_snapshot,
                      taste_health=taste_health_line(args.taste_health, now))

    if args.stdout:
        sys.stdout.write(md)
        _report(sys.stderr, md, args.cap_tokens, dest="stdout")
        return 0

    out = os.path.abspath(args.out)
    try:
        assert_gitignored(out)
    except DigestError as exc:
        sys.stderr.write("boot-digest: DIGEST FAILURE: %s\n" % exc)
        return 2
    d = os.path.dirname(out)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    # Atomic: the SessionStart hook is a concurrent READER of this exact path,
    # and a session that boots mid-write would read a truncated digest. Write a
    # sibling .tmp (same filesystem, so os.replace is a rename) and swap it in.
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(md)
    os.replace(tmp, out)
    _report(sys.stderr, md, args.cap_tokens, dest=out)
    return 0


def _report(err, md, cap, dest):
    err.write("boot-digest: digest written (~%d tokens, cap %d)\n"
              % (est_tokens(md), cap))
    err.write("  output: %s\n" % dest)


if __name__ == "__main__":
    raise SystemExit(main())
