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
writes, rewrites, or reshapes the pack -- the pack has external consumers, and
its schema is the contract. The only file this writes is its ``--out`` path
(written atomically via a sibling ``.tmp`` + ``os.replace``, so the hook never
reads a half-written digest).

The digest is ADVISORY STATE, NOT LAW: SAGA doctrine, repo CLAUDE.md, and
merge-is-Anthony's bind regardless of digest content.

Degrade-not-abort: a missing, unreadable, unparseable, or wrong-schema pack
writes NOTHING (any existing digest is left untouched -- the SessionStart hook
already flags a stale one) and exits 0. Only a refused write path is a hard
failure.

Content, in order: header (pack timestamp + age, ``STALE (>36h)`` when old),
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
-- each with an explicit ``[truncated]`` marker. The header, the daily-brief
pointer, and the full-pack pointer are NEVER shed.

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

# The digest is read in SESSIONS, whose cwd is arbitrary (folder = activity, but
# not this folder). A repo-relative pointer resolves against the wrong directory
# there, so both the pack pointer and the [truncated] markers name the pack by
# ABSOLUTE path, derived from this file's repo root.
PACK_MD = os.path.join(_REPO_ROOT, "state", "BOOT-PACK.md")
PACK_POINTER = "Full pack: %s" % PACK_MD

# Decision-queue items carry a "<feed>:<environment>" source; these two feeds
# are repo-attributable (a ratification-backlog item is not).
_REPO_FEEDS = ("next-session:", "surface:")

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
# inputs -- the pack is READ ONLY; its schema is an external contract
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
        lines.append("- WARNING: imprint export STALE: %dh old -- 05:45 export "
                     "may have failed" % (age // 3600))
    lines.append("- full export: %s (%.1f MB; grep on demand, `imprint history "
                 "<urn>` for provenance)"
                 % (summary["path"], summary["bytes"] / (1024.0 * 1024.0)))
    live = live_store_mtime(db_path)
    if live is not None:
        lines.append("- live store last written %s" % _ago(now_ts - live))
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


def collect(now, pack, daily_brief=None, daily_brief_path=None,
            extra_warnings=None):
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
        "generated_at": generated_at or "unknown",
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
    pack itself is degraded; open items are standing design gaps. The header and
    both pointers are never shed.
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
    if model["age_hours"] is None:
        age = "age unknown"
    else:
        age = "%dh ago" % model["age_hours"]
    header = "Pack generated: %s (%s)" % (model["generated_at"], age)
    if model["stale"]:
        header += " STALE (>36h)"

    lines = [TITLE, header, ""]
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
                 extra_warnings=None):
    """Build the digest markdown. ``now`` is injectable for determinism.

    The 500-token shed loop runs WITHOUT the imprint section: the IMPRINT
    summary rides outside the token cap on its own byte cap (see module
    docstring), so it must never pressure the shed loop into dropping
    warnings/attention lines that the cap exists to protect.
    """
    model = collect(now, pack, daily_brief, daily_brief_path, extra_warnings)
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
                      extra_warnings=launchctl_warnings())

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
