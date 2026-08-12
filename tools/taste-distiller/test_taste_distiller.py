#!/usr/bin/env python3
"""Tests for the M3 taste distiller -- stdlib unittest.

All fixtures are SYNTHETIC transcripts/configs in a tempdir; the real imprint
data root, ~/.claude/settings.json, and the claude/imprint binaries are NEVER
touched (model + ingest calls are mocked). Run with:

    cd tools/taste-distiller && python3 -m unittest test_taste_distiller -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import backfill_seed  # noqa: E402
import taste_distiller as td  # noqa: E402

HOOK = os.path.join(_HERE, "distill_spool_hook.py")


def _make_operator_root(tmp):
    """A synthetic imprint config + operator layout; returns (cfg_path, root)."""
    data_root = os.path.join(tmp, "imprint-data")
    cfg = {"data_root": data_root, "operator_slug": "testop"}
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    root = os.path.join(data_root, "testop", "macroseat")
    return cfg_path, root


def _write_transcript(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")


def _turn(typ, text, sidechain=False, blocks=None):
    content = blocks if blocks is not None else text
    return {"type": typ, "isSidechain": sidechain,
            "message": {"role": typ, "content": content}}


REGISTRY = [
    {"slug": "acme", "kind": "client", "aliases": ["Acme Corp", "ACME"],
     "project_dir_globs": ["*/acme-*"]},
    {"slug": "rex", "kind": "collaborator", "aliases": ["Rex"],
     "project_dir_globs": []},
]


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path, self.root = _make_operator_root(self.tmp)
        self.env = dict(os.environ, IMPRINT_CONFIG=self.cfg_path)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _run(self, stdin):
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, HOOK], input=stdin,
                              capture_output=True, text=True, env=self.env)
        return proc, (time.monotonic() - t0) * 1000

    def test_appends_queue_line_and_is_fast(self):
        event = {"session_id": "s1", "transcript_path": "/tmp/t.jsonl",
                 "cwd": "/Users/x/code/acme-site"}
        proc, ms = self._run(json.dumps(event))
        self.assertEqual(proc.returncode, 0)
        entries, bad = td.read_queue(os.path.join(self.root, "distill-queue.ndjson"))
        self.assertEqual(bad, 0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["session_id"], "s1")
        self.assertEqual(entries[0]["project_dir"], "/Users/x/code/acme-site")
        # Acceptance bar is 200ms of hook budget; allow interpreter-start slack
        # in the test but fail on anything egregious.
        self.assertLess(ms, 1000)

    def test_fail_open_on_garbage_and_missing_config(self):
        proc, _ = self._run("not json at all")
        self.assertEqual(proc.returncode, 0)
        env = dict(os.environ, IMPRINT_CONFIG=os.path.join(self.tmp, "absent.json"))
        proc = subprocess.run([sys.executable, HOOK],
                              input='{"session_id":"s2"}',
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "distill-queue.ndjson")))


class QueueLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_read_queue_lenient(self):
        path = os.path.join(self.tmp, "q.ndjson")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"session_id":"a","transcript_path":"/t"}\n')
            fh.write("garbage\n\n")
            fh.write('{"no_session": true}\n')
            fh.write('{"session_id":"b"}\n')
        entries, bad = td.read_queue(path)
        self.assertEqual([e["session_id"] for e in entries], ["a", "b"])
        self.assertEqual(bad, 2)

    def test_ledger_roundtrip_and_corrupt_recovery(self):
        path = os.path.join(self.tmp, "ledger.json")
        td.save_ledger(path, {"a": {"status": "distilled"}})
        self.assertEqual(td.load_ledger(path)["a"]["status"], "distilled")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        self.assertEqual(td.load_ledger(path), {})
        self.assertTrue(os.path.exists(path + ".corrupt"))


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_extract_turns_filters(self):
        path = os.path.join(self.tmp, "t.jsonl")
        _write_transcript(path, [
            _turn("user", "keep this"),
            _turn("user", "sidechain", sidechain=True),
            {"type": "summary", "summary": "not a turn"},
            _turn("assistant", None, blocks=[
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "assistant text"},
                {"type": "tool_use", "name": "Bash"}]),
            _turn("user", "<command-name>/foo</command-name>"),
            _turn("user", None, blocks=[{"type": "tool_result", "content": "x"}]),
            _turn("user", "second real turn"),
        ])
        turns = td.extract_turns(path)
        self.assertEqual([(r, t) for _, r, t in turns],
                         [("user", "keep this"),
                          ("assistant", "assistant text"),
                          ("user", "second real turn")])
        # Line numbers are 1-indexed positions in the raw file (provenance).
        self.assertEqual([n for n, _, _ in turns], [1, 4, 7])

    def test_decision_tool_results_kept_as_user_turns(self):
        path = os.path.join(self.tmp, "d.jsonl")
        _write_transcript(path, [
            _turn("assistant", None, blocks=[
                {"type": "text", "text": "Four decisions for you."},
                {"type": "tool_use", "name": "AskUserQuestion", "id": "tu_1"},
                {"type": "tool_use", "name": "Bash", "id": "tu_2"}]),
            _turn("user", None, blocks=[
                {"type": "tool_result", "tool_use_id": "tu_1",
                 "content": [{"type": "text", "text": "Item 1: YES — ratify"}]},
                {"type": "tool_result", "tool_use_id": "tu_2",
                 "content": "bash output noise"}]),
        ])
        turns = td.extract_turns(path)
        self.assertIn(("user", "[decision] Item 1: YES — ratify"),
                      [(r, t) for _, r, t in turns])
        self.assertNotIn("bash output noise", " ".join(t for _, _, t in turns))

    def test_build_excerpt_drops_assistant_first_and_marks(self):
        turns = [(1, "user", "u1"), (2, "assistant", "a" * 400),
                 (3, "assistant", "b" * 400), (4, "user", "u2")]
        excerpt, truncated = td.build_excerpt(turns, max_chars=120)
        self.assertTrue(truncated)
        self.assertIn("u1", excerpt)
        self.assertIn("u2", excerpt)
        self.assertNotIn("aaaa", excerpt)
        self.assertIn("omitted for budget", excerpt)

    def test_find_taste_markers_in_text_and_tool_input(self):
        block = ("## TASTE MARKER (macro-seat distiller anchors)\n"
                 "- RULED: tabs not spaces\n- CORRECTED: never push to main\n"
                 "## Next section\n- not a marker")
        path = os.path.join(self.tmp, "m.jsonl")
        _write_transcript(path, [
            # Handoffs land via the Write tool: the block is JSON-escaped
            # inside the tool_use input, not a rendered text turn.
            _turn("assistant", None, blocks=[
                {"type": "tool_use", "name": "Write",
                 "input": {"file_path": "/tmp/h.md", "content": block}}]),
            _turn("assistant", "Handoff written.\n" + block),  # echo: deduped
        ])
        markers = td.find_taste_markers(path)
        self.assertEqual([m for _, m in markers],
                         ["RULED: tabs not spaces", "CORRECTED: never push to main"])
        self.assertEqual(markers[0][0], 1)  # first sighting wins (tool input)


class ParseTest(unittest.TestCase):
    def test_parse_specimens_fenced_and_prose(self):
        fenced = "```json\n[{\"quote\": \"q\", \"kind\": \"ruling\"}]\n```"
        self.assertEqual(td.parse_specimens(fenced)[0]["quote"], "q")
        prose = 'Here you go:\n[{"quote": "q2"}]\nDone.'
        self.assertEqual(td.parse_specimens(prose)[0]["quote"], "q2")
        with self.assertRaises(ValueError):
            td.parse_specimens("no array here")
        # Quoteless items are dropped, not fatal.
        self.assertEqual(td.parse_specimens('[{"kind": "ruling"}]'), [])


class ScopeTest(unittest.TestCase):
    def test_dir_unique_is_confident(self):
        spec = {"quote": "do it this way", "why": ""}
        self.assertEqual(td.assign_scope(spec, "/Users/x/code/acme-site", REGISTRY),
                         ("entity:acme", False))

    def test_cue_only_is_flagged(self):
        spec = {"quote": "Rex prefers short intros", "why": ""}
        self.assertEqual(td.assign_scope(spec, "/Users/x/code/other", REGISTRY),
                         ("entity:rex", True))

    def test_conflict_and_none(self):
        spec = {"quote": "ACME and Rex disagree", "why": ""}
        self.assertEqual(td.assign_scope(spec, "/Users/x/code/other", REGISTRY),
                         ("global", True))
        spec = {"quote": "generic judgment", "why": ""}
        self.assertEqual(td.assign_scope(spec, "/Users/x/code/other", REGISTRY),
                         ("global", False))

    def test_alias_needs_word_boundary(self):
        spec = {"quote": "regexes are great", "why": ""}  # 'rex' inside 'regexes'
        self.assertEqual(td.assign_scope(spec, "/Users/x/code/other", REGISTRY),
                         ("global", False))

    def test_model_hint_counts_as_cue(self):
        spec = {"quote": "generic judgment", "why": "", "scope_hint": "acme"}
        self.assertEqual(td.assign_scope(spec, "/Users/x/code/other", REGISTRY),
                         ("entity:acme", True))


class CandidateTest(unittest.TestCase):
    def test_shape_and_stable_content(self):
        entry = {"session_id": "s1", "transcript_path": "/t/x.jsonl",
                 "project_dir": "/Users/x/code/acme-site"}
        specs = [{"quote": "never use em-dashes", "why": "PG copy standards",
                  "kind": "preference", "line_start": 10, "line_end": 12,
                  "marker_derived": True}]
        c1 = td.build_candidates(specs, entry, REGISTRY, "2026-08-11T00:00:00Z")
        c2 = td.build_candidates(specs, entry, REGISTRY, "2026-08-12T09:00:00Z")
        self.assertEqual(len(c1), 1)
        cand = c1[0]
        self.assertEqual(cand["source_kind"], "session_transcript")
        self.assertEqual(cand["source_locator"], "/t/x.jsonl#L10-L12")
        self.assertEqual(cand["metadata"]["tenant_scope"], "entity:acme")
        self.assertTrue(cand["metadata"]["marker_derived"])
        env = cand["extensions"]["taste.v1"]
        self.assertEqual(env["kind"], "preference")
        self.assertEqual(env["scope"], "entity:acme")
        self.assertFalse(env["scope_ambiguous"])
        # content is the sha-dedupe key: identical across runs (no timestamps).
        self.assertEqual(cand["content"], c2[0]["content"])


class WorkerTest(unittest.TestCase):
    """End-to-end drain with the model + ingest mocked out."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path, self.root = _make_operator_root(self.tmp)
        os.makedirs(self.root)
        self.transcript = os.path.join(self.tmp, "sess.jsonl")
        _write_transcript(self.transcript, [
            _turn("user", "please refactor the parser " + "x" * 2000),
            _turn("assistant", "done"),
            _turn("user", "ruling: always keep the parser lenient"),
        ])
        with open(td.queue_path(self.root), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"session_id": "sess", "transcript_path":
                                 self.transcript, "project_dir": "/p"}) + "\n")
            fh.write(json.dumps({"session_id": "gone", "transcript_path":
                                 "/nope.jsonl", "project_dir": "/p"}) + "\n")
            fh.write(json.dumps({"session_id": "tiny", "transcript_path":
                                 self.transcript_tiny(), "project_dir": "/p"}) + "\n")

    def transcript_tiny(self):
        path = os.path.join(self.tmp, "tiny.jsonl")
        _write_transcript(path, [_turn("user", "hi")])
        return path

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_drain_ledgers_every_entry_and_never_keeps(self):
        model_out = json.dumps([{"quote": "always keep the parser lenient",
                                 "why": "explicit ruling", "kind": "ruling",
                                 "line_start": 3, "line_end": 3,
                                 "scope_hint": "global",
                                 "marker_derived": False}])
        calls = []
        with unittest.mock.patch.dict(os.environ,
                                      {"IMPRINT_CONFIG": self.cfg_path,
                                       "MACROSEAT_QUIET_SECONDS": "0"}), \
             unittest.mock.patch.object(td, "call_model", return_value=model_out), \
             unittest.mock.patch.object(td, "ingest_scan",
                                        side_effect=lambda c: calls.append(c) or "ok"):
            rc = td.main([])
        self.assertEqual(rc, 0)
        ledger = td.load_ledger(td.ledger_path(self.root))
        self.assertEqual(ledger["sess"]["status"], "distilled")
        self.assertEqual(ledger["sess"]["specimens"], 1)
        self.assertEqual(ledger["gone"]["status"], "transcript_missing")
        self.assertEqual(ledger["tiny"]["status"], "skipped_trivial")
        # Exactly one ingest_scan batch; keep/kill never invoked (no such
        # attribute is even exercised — quarantine only).
        self.assertEqual(len(calls), 1)

    def test_quiet_gate_defers_fresh_transcripts(self):
        # Transcripts written moments ago (default 1800s gate): nothing is
        # processed, nothing is ledgered — the entries wait for a later tick.
        with unittest.mock.patch.dict(os.environ,
                                      {"IMPRINT_CONFIG": self.cfg_path}), \
             unittest.mock.patch.object(td, "call_model") as model, \
             unittest.mock.patch.object(td, "ingest_scan", return_value="ok"):
            td.main([])
        self.assertEqual(model.call_count, 0)
        ledger = td.load_ledger(td.ledger_path(self.root))
        self.assertNotIn("sess", ledger)
        self.assertNotIn("tiny", ledger)
        # Missing transcript is NOT gated (nothing to wait for): ledgered now.
        self.assertEqual(ledger["gone"]["status"], "transcript_missing")

    def test_error_retries_then_goes_terminal(self):
        env = {"IMPRINT_CONFIG": self.cfg_path, "MACROSEAT_QUIET_SECONDS": "0"}
        with unittest.mock.patch.dict(os.environ, env), \
             unittest.mock.patch.object(td, "call_model",
                                        side_effect=RuntimeError("model down")), \
             unittest.mock.patch.object(td, "ingest_scan", return_value="ok"):
            for expect in (1, 2, 3, 3):  # 4th run: capped, not attempted
                td.main([])
                ledger = td.load_ledger(td.ledger_path(self.root))
                self.assertEqual(ledger["sess"]["status"], "error")
                self.assertEqual(ledger["sess"]["attempts"], expect)

    def test_force_session_bypasses_ledger(self):
        env = {"IMPRINT_CONFIG": self.cfg_path, "MACROSEAT_QUIET_SECONDS": "0"}
        with unittest.mock.patch.dict(os.environ, env), \
             unittest.mock.patch.object(td, "call_model", return_value="[]") as model, \
             unittest.mock.patch.object(td, "ingest_scan", return_value="ok"):
            td.main([])                      # ledgers sess
            model.reset_mock()
            td.main(["--session", "sess"])   # forced reprocess, only sess
        self.assertEqual(model.call_count, 1)

    def test_second_run_is_noop_via_ledger(self):
        with unittest.mock.patch.dict(os.environ,
                                      {"IMPRINT_CONFIG": self.cfg_path,
                                       "MACROSEAT_QUIET_SECONDS": "0"}), \
             unittest.mock.patch.object(td, "call_model") as model, \
             unittest.mock.patch.object(td, "ingest_scan", return_value="ok"):
            model.return_value = "[]"
            td.main([])
            first = dict(td.load_ledger(td.ledger_path(self.root)))
            model.reset_mock()
            td.main([])
        self.assertEqual(model.call_count, 0)
        self.assertEqual(td.load_ledger(td.ledger_path(self.root)), first)


class BackfillTest(unittest.TestCase):
    def test_decode_project_dir(self):
        real = os.path.dirname(_HERE)  # .../fully-aware/tools — exists on disk
        mangled = real.replace("/", "-")
        self.assertEqual(backfill_seed.decode_project_dir(mangled), real)
        self.assertEqual(backfill_seed.decode_project_dir("plainname"), "plainname")


if __name__ == "__main__":
    unittest.main()
