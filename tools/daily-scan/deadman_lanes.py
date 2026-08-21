#!/usr/bin/env python3
"""deadman_lanes.py -- deterministic post-processors for the daily brief.

Two subcommands, stdlib-only, run by run-daily-scan.sh stage 3 immediately
before the brief is copied to LATEST.md (W9; motivated by
AUTONOMY-AUDIT-2026-08-18: the brief must never silently omit a lane that
died -- absence of news must read as news -- and no section may bloat past
10 items):

  deadman   Print the `### LANES THAT DID NOT RUN` markdown section for the
            launchd lane of state/automation-map.json. ARMING comes from
            live `launchctl list` output, never from the map's `status`
            field (the map is a snapshot; launchctl is now): a node is
            armed iff its title appears as the label -- the 3rd
            whitespace-separated column -- in that output. HEALTH is the
            newest mtime among the node's declared stdout/stderr artifacts.
            Dead lanes are a REPORT, not an error: exit 0 even when every
            lane is down; non-zero only on an internal crash. Unusable
            inputs (empty launchctl output, missing/unparseable map) still
            print the section with a DEAD-MAN CHECK INCONCLUSIVE body --
            fail loud, never silent-green.

  cap       Hard-cap every consecutive markdown list run in the brief at
            --max-items items, editing the file in place. Suppressed items
            are counted in one trailing `(+K more suppressed)` line in the
            same list style. Continuation lines indented under a kept item
            stay; continuations under a suppressed item go. A file with no
            over-long run is left byte-identical.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import sys

SECTION_HEADER = "### LANES THAT DID NOT RUN"

# Staleness thresholds. Interval jobs (`every <N>s`) tolerate three missed
# fires but never less than 3 hours of quiet; everything else (calendar,
# daily, unknown) gets a day plus slack -- a 06:15 daily job read at 06:00
# the next morning must not count as dead.
INTERVAL_FLOOR_S = 3 * 3600
CALENDAR_THRESHOLD_S = 26 * 3600
MAP_STALE_DAYS = 7

_EVERY = re.compile(r"^every (\d+)s$")


# --------------------------------------------------------------------------- #
# deadman
# --------------------------------------------------------------------------- #

def _parse_now(raw):
    if not raw:
        return datetime.datetime.now(datetime.timezone.utc)
    dt = datetime.datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _read_launchctl_labels(path):
    """-> (labels, error_reason_or_None). Labels are the 3rd whitespace-
    separated column, exactly as `launchctl list` prints them; the header
    line contributes the literal "Label", which no real job title matches."""
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
    except OSError as exc:
        return set(), "launchctl list input unreadable (%s)" % exc
    if not text.strip():
        return set(), "launchctl list input is empty"
    labels = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            labels.add(parts[2])
    if not labels:
        return set(), "no labels parsed from launchctl list input"
    return labels, None


def _threshold_s(subtitle):
    m = _EVERY.match(subtitle or "")
    if m:
        return max(3 * int(m.group(1)), INTERVAL_FLOOR_S)
    return CALENDAR_THRESHOLD_S


def _newest_artifact_mtime(node):
    """Newest mtime among the node's declared stdout/stderr files that
    exist; None when neither does ("no artifact ever")."""
    detail = node.get("detail") or {}
    mtimes = []
    for key in ("stdout", "stderr"):
        path = detail.get(key)
        if path and os.path.exists(path):
            try:
                mtimes.append(os.path.getmtime(path))
            except OSError:
                pass
    return max(mtimes) if mtimes else None


def _humanize(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, minutes)
    return "%dm" % minutes


def _stale_map_warning(amap, now):
    """WARNING body line when generated_at is over MAP_STALE_DAYS old.
    A missing or unparseable generated_at also warns -- the map's age is
    then unknown, and unknown must not read as fresh."""
    raw = amap.get("generated_at")
    try:
        gen = datetime.datetime.fromisoformat(raw)
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return ("WARNING: automation-map.json is stale "
                "(generated %s)." % (raw or "unknown"))
    if (now - gen) > datetime.timedelta(days=MAP_STALE_DAYS):
        return ("WARNING: automation-map.json is stale "
                "(generated %s)." % gen.date().isoformat())
    return None


def render_deadman(map_path, launchctl_path, now, max_lines):
    """The full markdown section, trailing newline included. Never raises
    on bad INPUTS (that is an INCONCLUSIVE body); a genuine bug still
    propagates to a non-zero exit."""
    out = [SECTION_HEADER, ""]

    labels, lc_err = _read_launchctl_labels(launchctl_path)
    try:
        with open(map_path, encoding="utf-8") as fh:
            amap = json.load(fh)
        nodes = amap.get("nodes")
        if not isinstance(nodes, list):
            raise ValueError("no nodes[] list in the map")
    except (OSError, ValueError) as exc:
        out.append("DEAD-MAN CHECK INCONCLUSIVE: automation map unusable "
                   "at %s (%s)" % (map_path, exc))
        return "\n".join(out) + "\n"
    if lc_err:
        out.append("DEAD-MAN CHECK INCONCLUSIVE: %s" % lc_err)
        return "\n".join(out) + "\n"

    warning = _stale_map_warning(amap, now)
    if warning:
        out.append(warning)

    disarmed, stale, armed_count = [], [], 0
    for node in nodes:
        if node.get("lane") != "launchd":
            continue
        title = node.get("title") or node.get("id") or "?"
        if title not in labels:
            disarmed.append("- %s — DISARMED" % title)
            continue
        armed_count += 1
        mtime = _newest_artifact_mtime(node)
        if mtime is None:
            stale.append((math.inf, "- %s — armed, no artifact ever" % title))
            continue
        seen = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)
        age_s = (now - seen).total_seconds()
        if age_s > _threshold_s(node.get("subtitle")):
            stale.append((age_s, "- %s — armed, no artifact since %s (%s)"
                          % (title, seen.isoformat(timespec="seconds"),
                             _humanize(age_s))))

    disarmed.sort()
    stale.sort(key=lambda pair: (-pair[0], pair[1]))  # stalest first
    report = disarmed + [line for _age, line in stale]

    if not report:
        out.append("All %d armed launchd lanes ran within threshold."
                   % armed_count)
    elif len(report) > max_lines:
        out.extend(report[:max_lines])
        out.append("- (+%d more)" % (len(report) - max_lines))
    else:
        out.extend(report)
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# cap
# --------------------------------------------------------------------------- #

_BULLET = re.compile(r"^([-*]) ")
_NUMBERED = re.compile(r"^(\d+)([.)]) ")


def _is_item(line):
    return bool(_BULLET.match(line) or _NUMBERED.match(line))


def _is_continuation(line):
    return line[:1] in (" ", "\t") and bool(line.strip())


def _suppressed_line(first_item_line, kept, extra):
    """The replacement line, in the run's own list style."""
    m = _BULLET.match(first_item_line)
    if m:
        return "%s (+%d more suppressed)\n" % (m.group(1), extra)
    m = _NUMBERED.match(first_item_line)
    return "%d%s (+%d more suppressed)\n" % (kept + 1, m.group(2), extra)


def cap_brief(path, max_items):
    """Truncate every over-long consecutive list run in place. The file is
    rewritten only when something was actually suppressed, so a compliant
    brief stays byte-identical (newline='' keeps CRLFs as found)."""
    with open(path, encoding="utf-8", errors="surrogateescape",
              newline="") as fh:
        lines = fh.readlines()

    out, changed, i = [], False, 0
    while i < len(lines):
        if not _is_item(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        # A run: consecutive item lines plus indented continuations; any
        # other line (blank included) ends it.
        item_starts, j = [], i
        while j < len(lines):
            if _is_item(lines[j]):
                item_starts.append(j)
            elif not _is_continuation(lines[j]):
                break
            j += 1
        if len(item_starts) > max_items:
            cut = item_starts[max_items]  # first suppressed item line
            out.extend(lines[i:cut])
            out.append(_suppressed_line(lines[i], max_items,
                                        len(item_starts) - max_items))
            changed = True
        else:
            out.extend(lines[i:j])
        i = j

    if changed:
        with open(path, "w", encoding="utf-8", errors="surrogateescape",
                  newline="") as fh:
            fh.writelines(out)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    dm = sub.add_parser("deadman",
                        help="print the LANES THAT DID NOT RUN section")
    dm.add_argument("--map", dest="map_path", required=True,
                    help="path to automation-map.json")
    dm.add_argument("--launchctl-list", dest="launchctl", required=True,
                    help="captured `launchctl list` output; - reads stdin")
    dm.add_argument("--now", default=None,
                    help="ISO8601 clock override (tests); default: now")
    dm.add_argument("--max-lines", type=int, default=10)

    cp = sub.add_parser("cap", help="hard-cap list runs in the brief")
    cp.add_argument("--brief", required=True)
    cp.add_argument("--max-items", type=int, default=10)

    args = ap.parse_args(argv)
    if args.cmd == "deadman":
        sys.stdout.write(render_deadman(args.map_path, args.launchctl,
                                        _parse_now(args.now), args.max_lines))
        return 0
    return cap_brief(args.brief, args.max_items)


if __name__ == "__main__":
    sys.exit(main())
