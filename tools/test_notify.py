#!/usr/bin/env python3
"""Tests for notify.py -- no network, ever: the transport is always injected,
and the opt-in flag plus the topic-file override are set per test.

    uvx --with pytest python -m pytest -q tools/test_notify.py
    /usr/bin/python3 -m unittest tools.test_notify   (from the repo root)
"""

import os
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "notify", os.path.join(_HERE, "notify.py"))
notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify)


class _Transport:
    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    def __call__(self, url, data, headers):
        self.calls.append((url, data, headers))
        if self.raises:
            raise self.raises


class PushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.topic_path = os.path.join(self.tmp.name, "topic")
        with open(self.topic_path, "w", encoding="utf-8") as fh:
            fh.write("test-topic-abc123\n")

    def env(self, **extra):
        base = {"FULLY_AWARE_PUSH": "1",
                "FULLY_AWARE_NTFY_TOPIC_FILE": self.topic_path}
        base.update(extra)
        return mock.patch.dict(os.environ, base, clear=False)

    def test_disabled_without_the_opt_in_flag(self):
        transport = _Transport()
        with mock.patch.dict(os.environ,
                             {"FULLY_AWARE_NTFY_TOPIC_FILE": self.topic_path}):
            os.environ.pop("FULLY_AWARE_PUSH", None)
            self.assertFalse(notify.push("t", "m", transport=transport))
        self.assertEqual(transport.calls, [])

    def test_a_flag_value_other_than_1_stays_disabled(self):
        transport = _Transport()
        with self.env(FULLY_AWARE_PUSH="true"):
            self.assertFalse(notify.push("t", "m", transport=transport))
        self.assertEqual(transport.calls, [])

    def test_sends_with_flag_and_topic(self):
        transport = _Transport()
        with self.env():
            self.assertTrue(notify.push("Fixer blocked", "the message",
                                        priority="high", transport=transport))
        (url, data, headers), = transport.calls
        self.assertEqual(url, "https://ntfy.sh/test-topic-abc123")
        self.assertEqual(data, b"the message")
        self.assertEqual(headers["Title"], "Fixer blocked")
        self.assertEqual(headers["Priority"], "high")

    def test_missing_topic_file_means_no_push(self):
        transport = _Transport()
        with self.env(FULLY_AWARE_NTFY_TOPIC_FILE=self.topic_path + ".nope"):
            self.assertFalse(notify.push("t", "m", transport=transport))
        self.assertEqual(transport.calls, [])

    def test_empty_or_multiword_topic_is_refused(self):
        transport = _Transport()
        for bad in ("", "   \n", "two words\n", "a/b\n"):
            with open(self.topic_path, "w", encoding="utf-8") as fh:
                fh.write(bad)
            with self.env():
                self.assertFalse(notify.push("t", "m", transport=transport))
        self.assertEqual(transport.calls, [])

    def test_a_transport_failure_is_a_silent_false(self):
        transport = _Transport(raises=OSError("network down"))
        with self.env():
            self.assertFalse(notify.push("t", "m", transport=transport))
        self.assertEqual(len(transport.calls), 1)

    def test_long_messages_are_truncated(self):
        transport = _Transport()
        with self.env():
            notify.push("t", "x" * 5000, transport=transport)
        (_url, data, _headers), = transport.calls
        self.assertEqual(len(data), notify.MESSAGE_LIMIT)
        self.assertTrue(data.endswith(b"..."))

    def test_newlines_are_flattened_out_of_title_and_body(self):
        transport = _Transport()
        with self.env():
            notify.push("line\r\nbroken  title", "body\nwith\nlines",
                        transport=transport)
        (_url, data, headers), = transport.calls
        self.assertEqual(headers["Title"], "line broken title")
        self.assertEqual(data, b"body with lines")

    def test_non_latin1_title_never_breaks_the_header(self):
        transport = _Transport()
        with self.env():
            self.assertTrue(notify.push("на111 → done", "m",
                                        transport=transport))
        (_url, _data, headers), = transport.calls
        headers["Title"].encode("latin-1")  # must not raise

    def test_an_unknown_priority_falls_back_to_default(self):
        transport = _Transport()
        with self.env():
            notify.push("t", "m", priority="shouty", transport=transport)
        (_url, _data, headers), = transport.calls
        self.assertEqual(headers["Priority"], "default")


if __name__ == "__main__":
    unittest.main()
