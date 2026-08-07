#!/usr/bin/env python3
"""check-intake-staleness.py -- liveness probe for a JSON intake state file.

Answers one question: did the pulse that owns this state file run recently
enough? The consumer of an intake pulse is often an LLM automation rather than
a daemon -- when that thread dies, intake stops silently and nothing in the
repo's SHA, PR count, or file listing changes. This probe converts that silence
into a boot WARNING.

Class: read-only, stateless, content-free. It reads the top-level
``committed_at`` timestamp and NOTHING else -- no meeting bodies, no captured
content, no logging of what it saw. Stdlib only, Python 3.9+, no network.

The exit code is the contract:
  0  fresh -- committed_at is within --max-age-hours (one line on stdout)
  1  stale, missing file, unreadable/unparseable JSON, or a missing or
     unparseable committed_at (one explanatory line on stderr)

Degrade-not-crash: every failure mode is caught and reported as a one-line
message. This never raises a traceback -- a probe that explodes tells the
surface generator less than one that says "1" with a reason.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys


UTC = datetime.timezone.utc


def fail(message):
    """One explanatory line to stderr; exit code 1 is the signal."""
    sys.stderr.write("stale/unknown: %s\n" % message)
    return 1


def parse_committed_at(raw):
    """Parse an ISO-8601 stamp; 'Z' suffix and naive stamps both mean UTC.

    Returns (datetime, None) or (None, reason).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "committed_at is not a non-empty string"
    text = raw.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None, "committed_at is not ISO-8601: %r" % raw
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC), None


def check(state_path, max_age_hours):
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return fail("state file not found: %s" % state_path)
    except json.JSONDecodeError as exc:
        return fail("state file is not valid JSON (%s): %s" % (exc, state_path))
    except OSError as exc:
        return fail("state file unreadable (%s): %s" % (exc, state_path))

    if not isinstance(payload, dict):
        return fail("state file is not a JSON object: %s" % state_path)

    if "committed_at" not in payload:
        return fail("state file has no committed_at key: %s" % state_path)

    stamp, reason = parse_committed_at(payload["committed_at"])
    if stamp is None:
        return fail("%s (%s)" % (reason, state_path))

    age_hours = (
        datetime.datetime.now(UTC) - stamp).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        return fail(
            "committed_at %s is %.1fh old (max %.4gh): %s"
            % (payload["committed_at"], age_hours, max_age_hours, state_path))

    sys.stdout.write(
        "fresh: committed_at %s (%.1fh ago)\n"
        % (payload["committed_at"], age_hours))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Exit 0 when a JSON state file's committed_at is fresh.")
    parser.add_argument(
        "--state", required=True,
        help="path to the JSON state file carrying a top-level committed_at")
    parser.add_argument(
        "--max-age-hours", type=float, default=24.0,
        help="freshness window in hours (default: 24)")
    args = parser.parse_args(argv)

    try:
        return check(args.state, args.max_age_hours)
    except Exception as exc:  # never surface a traceback from a probe
        return fail("unexpected probe error (%s): %s" % (type(exc).__name__, exc))


if __name__ == "__main__":
    sys.exit(main())
