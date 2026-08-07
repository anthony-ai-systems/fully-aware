#!/usr/bin/env python3
"""Tests for check-intake-staleness.py -- stdlib unittest, no third-party deps.

All fixtures are SYNTHETIC: throwaway state files in a tempdir, no real intake
content. The script is exercised through subprocess so the assertions are on
the actual contract (exit code + one line of output), not on internals.

    python3 -m unittest test_check_intake_staleness -v
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "check-intake-staleness.py")
UTC = datetime.timezone.utc


def _run(state_path, *extra):
    """Run the probe; returns (returncode, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, _SCRIPT, "--state", state_path, *extra],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _write(tmp, payload, name="state.json"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as handle:
        if isinstance(payload, str):
            handle.write(payload)
        else:
            json.dump(payload, handle)
    return path


def _stamp(hours_ago):
    when = datetime.datetime.now(UTC) - datetime.timedelta(hours=hours_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class CheckIntakeStalenessTest(unittest.TestCase):

    def test_fresh_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"committed_at": _stamp(1)})
            rc, out, err = _run(path)
        self.assertEqual(rc, 0, err)
        self.assertIn("fresh:", out)
        self.assertEqual(err, "")

    def test_stale_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"committed_at": _stamp(48)})
            rc, out, err = _run(path)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("old", err)

    def test_custom_window_makes_recent_stamp_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"committed_at": _stamp(2)})
            rc, _out, _err = _run(path, "--max-age-hours", "1")
            self.assertEqual(rc, 1)
            rc, _out, _err = _run(path, "--max-age-hours", "72")
            self.assertEqual(rc, 0)

    def test_missing_file_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, err = _run(os.path.join(tmp, "absent.json"))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("not found", err)

    def test_garbage_json_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "{not json at all,,,")
            rc, out, err = _run(path)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("not valid JSON", err)

    def test_missing_committed_at_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"meetings": {}})
            rc, out, err = _run(path)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("no committed_at", err)

    def test_unparseable_committed_at_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"committed_at": "last tuesday"})
            rc, _out, err = _run(path)
        self.assertEqual(rc, 1)
        self.assertIn("ISO-8601", err)

    def test_z_suffix_with_milliseconds_parses(self):
        """The live granola state writes e.g. 2026-08-07T16:01:07.058Z."""
        with tempfile.TemporaryDirectory() as tmp:
            when = datetime.datetime.now(UTC) - datetime.timedelta(minutes=30)
            raw = when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            path = _write(tmp, {"committed_at": raw})
            rc, out, err = _run(path)
        self.assertEqual(rc, 0, err)
        self.assertIn(raw, out)
        self.assertIn("0.5h ago", out)

    def test_naive_timestamp_treated_as_utc(self):
        with tempfile.TemporaryDirectory() as tmp:
            when = datetime.datetime.now(UTC).replace(tzinfo=None)
            path = _write(tmp, {"committed_at": when.isoformat()})
            rc, _out, err = _run(path)
        self.assertEqual(rc, 0, err)

    def test_non_object_json_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, ["committed_at"])
            rc, _out, err = _run(path)
        self.assertEqual(rc, 1)
        self.assertIn("not a JSON object", err)


if __name__ == "__main__":
    unittest.main()
