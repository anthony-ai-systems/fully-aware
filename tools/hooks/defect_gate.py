#!/usr/bin/env python3
"""defect_gate -- while a serious defect sits unfixed, no session starts new background work.

PreToolUse hook on Task|Agent|Workflow. Refuses to start NEW background work
(subagents, workflows) while a P0 defect has been open seven days or more,
unless this session has said it is here to fix one. Written rules only advise;
this hook enforces one of them.

It reads the status file the morning job writes:

    /Users/anthonyflores/code/fully-aware/state/defects-status.json

and blocks (exit 2 + one short message on stderr) only when that file is fresh
and something in it genuinely qualifies. Everything else passes.

FAIL-OPEN, always. No status file, an unreadable one, a stale one, a surprise
exception anywhere -- exit 0 and say nothing. A gate that jams a session shut
because a nightly job did not run is worse than the defect it is guarding.

Escape hatches, in the order a session would reach for them:
  * declare a fix session -- touch ~/.claude/defect-fix-mode/<session_id>
    (or .../ALL for every session on this Mac); good for 12 hours
  * emergency off -- DEFECT_GATE_DISABLE=1 in the environment

Environment overrides (tests use all four; day-to-day nothing sets them):
  DEFECT_GATE_STATUS      path to the status file
  DEFECT_GATE_MARKER_DIR  directory holding fix-mode markers
  DEFECT_GATE_DAYS        days-open threshold (default 7)
  DEFECT_GATE_DISABLE     "1" disables the gate entirely

Copied to ~/.claude/hooks/defect_gate.py by tools/install-defect-gate.sh, so it
is stdlib-only, Python 3.9-clean, and imports nothing from this repo.
"""

import datetime
import json
import os
import sys
import time

GATED_TOOLS = ("Task", "Agent", "Workflow")

STATUS_PATH = "/Users/anthonyflores/code/fully-aware/state/defects-status.json"
MARKER_DIR = os.path.expanduser("~/.claude/defect-fix-mode")
DEFECTS_MD = "~/code/fully-aware/state/DEFECTS.md"

DEFAULT_DAYS = 7
STATUS_MAX_AGE_HOURS = 36.0
MARKER_MAX_AGE_HOURS = 12.0

# Blocking needs an item that is actually OPEN. "error" (the verify timed out),
# "provisional" (the verify is a placeholder that always fails) and "deferred"
# (not_before has not arrived) are all states where the register cannot honestly
# say the defect is sitting there unfixed.
BLOCKING_STATUS = "open"
BLOCKING_SEVERITY = "P0"

_MAX_QUOTE = 200


def _env(name, default=None):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _local_tz():
    return datetime.datetime.now().astimezone().tzinfo


def _parse_iso(text):
    """Parse an ISO timestamp into an aware datetime, or None.

    Tolerates a trailing Z (3.9's fromisoformat does not) and naive stamps,
    which are read as local time -- that is what the morning job writes.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    parsed = None
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.datetime.strptime(raw[:len(fmt) + 2], fmt)
                break
            except Exception:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_local_tz())
    return parsed


def _parse_date(text):
    """Parse an ISO date (leading YYYY-MM-DD is enough) into a date, or None."""
    if not isinstance(text, str) or len(text.strip()) < 10:
        return None
    try:
        return datetime.date(*(int(part) for part in text.strip()[:10].split("-")))
    except Exception:
        return None


def _threshold_days():
    raw = _env("DEFECT_GATE_DAYS")
    if raw is None:
        return DEFAULT_DAYS
    try:
        return int(str(raw).strip())
    except Exception:
        return DEFAULT_DAYS


def _status_is_fresh(payload):
    stamp = _parse_iso(payload.get("generated_at"))
    if stamp is None:
        # No usable timestamp: treat the file as untrustworthy, not as evidence.
        return False
    age_hours = (
        datetime.datetime.now(datetime.timezone.utc) - stamp
    ).total_seconds() / 3600.0
    if age_hours < -1.0:
        # A stamp meaningfully in the future is a bad clock or a hand-edited
        # file, not a fresh morning run. Treat it like any other bad file:
        # fail open, so a broken status can never hold a session shut.
        return False
    return age_hours <= STATUS_MAX_AGE_HOURS


def _days_open(item, today):
    """Days this item has been open: the file's number, else open_since."""
    raw = item.get("days_open")
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    since = _parse_date(item.get("open_since"))
    if since is None:
        return None
    return (today - since).days


def _qualifying(payload, threshold, today):
    """Every P0 that is open and has been open at least `threshold` days."""
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("severity", "")).strip().upper() != BLOCKING_SEVERITY:
            continue
        if str(item.get("status", "")).strip().lower() != BLOCKING_STATUS:
            continue
        days = _days_open(item, today)
        if days is None or days < threshold:
            continue
        out.append((days, item))
    # Oldest first; ties broken by id so the message is stable run to run.
    out.sort(key=lambda pair: (-pair[0], str(pair[1].get("id", ""))))
    return out


def _fix_mode(session_id):
    """True if this session (or every session) is declared a fix session."""
    marker_dir = _env("DEFECT_GATE_MARKER_DIR", MARKER_DIR)
    names = ["ALL"]
    if session_id:
        names.insert(0, str(session_id))
    cutoff = time.time() - MARKER_MAX_AGE_HOURS * 3600.0
    for name in names:
        # Guard against a session_id that is a path; markers are flat files.
        if os.sep in name or name in (".", ".."):
            continue
        path = os.path.join(marker_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) >= cutoff:
                return True
        except Exception:
            continue
    return False


def _quote(text):
    """One-line, length-capped quote of register prose."""
    flat = " ".join(str(text or "").split())
    if not flat:
        return "(no detail recorded)"
    if len(flat) > _MAX_QUOTE:
        flat = flat[:_MAX_QUOTE].rstrip() + "..."
    return flat


def _message(days, item, extra_count, session_id):
    ident = str(item.get("id") or "an unnamed P0")
    label = session_id if session_id else "<session_id>"
    lines = [
        "defect-gate: %s has blocked unattended work for %d days: %s"
        % (ident, days, _quote(item.get("symptom"))),
    ]
    if extra_count:
        lines.append(
            "%d P0 defects are past the line; this is the oldest."
            % (extra_count + 1)
        )
    lines.append("Fix: %s" % _quote(item.get("fix_hint")))
    lines.append(
        "To work on it in this session run: mkdir -p ~/.claude/defect-fix-mode "
        "&& touch ~/.claude/defect-fix-mode/%s  (12h)." % label
    )
    lines.append("Full list: %s" % DEFECTS_MD)
    return "\n".join(lines) + "\n"


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)
    if data.get("tool_name") not in GATED_TOOLS:
        sys.exit(0)
    if str(_env("DEFECT_GATE_DISABLE", "")).strip() == "1":
        sys.exit(0)

    status_path = _env("DEFECT_GATE_STATUS", STATUS_PATH)
    try:
        with open(status_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        sys.exit(0)          # missing or unreadable -- never block on absence
    if not isinstance(payload, dict) or not _status_is_fresh(payload):
        sys.exit(0)          # stale morning job -- never block on staleness

    today = datetime.datetime.now().date()
    blockers = _qualifying(payload, _threshold_days(), today)
    if not blockers:
        sys.exit(0)

    session_id = str(data.get("session_id") or "").strip()
    if _fix_mode(session_id):
        sys.exit(0)          # this session said it is here to fix one

    days, item = blockers[0]
    sys.stderr.write(_message(days, item, len(blockers) - 1, session_id))
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)          # a broken gate must never be a broken session
