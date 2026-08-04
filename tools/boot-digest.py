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
network. It CONSUMES ``state/boot-pack.json`` (schema ``boot-pack/v1``) and never
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
WARNINGS (count + one compressed line each), OPEN ITEMS (count + first sentence
each), ATTENTION (only repos behind their origin default branch or carrying decision-queue
items -- healthy repos are skipped entirely), an optional daily-brief pointer,
and the full-pack pointer.

Hard cap: 500 tokens (chars/4 estimate, the assembler's idiom). Overflow sheds
lowest-priority content first -- open items, then warnings, then attention lines
-- each with an explicit ``[truncated]`` marker. The header, the daily-brief
pointer, and the full-pack pointer are NEVER shed.

Determinism: wall-clock enters only through the pack-age arithmetic, and
``build_digest`` takes an injectable ``now``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

PACK_SCHEMA = "boot-pack/v1"

HARD_CAP_TOKENS = 500

# A pack older than this is stale enough that the session must not trust it
# (the daily LaunchAgent runs at 05:45, so >36h means a missed run).
STALE_PACK = 36 * 3600

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


def collect(now, pack, daily_brief=None, daily_brief_path=None):
    """Fold the pack into the digest model (plain lists, shed-able)."""
    generated_at = pack.get("generated_at") or ""
    gen = _parse_iso(generated_at)
    age_hours = None
    if gen is not None:
        age_hours = max(0, int((now - gen).total_seconds()) // 3600)
    attention = attention_lines(pack)
    return {
        "generated_at": generated_at or "unknown",
        "age_hours": age_hours,
        "stale": gen is not None and (now - gen).total_seconds() > STALE_PACK,
        "warnings": ["- " + compress(w) for w in pack.get("warnings") or []],
        "warnings_total": len(pack.get("warnings") or []),
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


def render_md(model):
    """Render the digest markdown from the model."""
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
    lines.append(PACK_POINTER)
    return "\n".join(lines) + "\n"


def build_digest(now, pack, daily_brief=None, daily_brief_path=None,
                 cap_tokens=HARD_CAP_TOKENS):
    """Build the digest markdown. ``now`` is injectable for determinism."""
    model = collect(now, pack, daily_brief, daily_brief_path)
    md = render_md(model)
    while est_tokens(md) > cap_tokens:
        if not _shed_one(model):
            break  # header + pointers alone are already over cap; render honestly
        md = render_md(model)
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
    import subprocess
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
                      cap_tokens=args.cap_tokens)

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
