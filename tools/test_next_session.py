#!/usr/bin/env python3
"""Tests for next_session.py -- stdlib unittest, no third-party deps.

All fixtures are SYNTHETIC. No real client names, no real handoff prose, no
content copied from any on-disk NEXT_SESSION.json. Run with:

    python3 -m unittest test_next_session -v
"""

import io
import json
import os
import tempfile
import unittest

import next_session as ns


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# --------------------------------------------------------------------------- #
# Synthetic fixtures (invented, schema-shaped)
# --------------------------------------------------------------------------- #
FIX_A = {
    "status": "done",
    "reason": "run_closed",
    "worktree": "/tmp/demo-worktree",
    "branch": "saga/demo-branch",
    "expected_head": "abc1234",
    "expected_head_mode": "ancestor",
    "prompt_file": None,
    "next_action": "NONE - run is closed; remaining gate is the merge.",
    "operator": "op-unit-7 (demo)",
    "id": "demo-run/HO/spine-1",
    "type": "HO",
    "run_id": "demo-run",
    "phase": "close",
    "date": "2026-01-02",
    "summary": "Demo spine did the demo thing and closed clean.",
    "commands_run": ["demo build", "demo test"],
    "procedures_abided": ["P-demo"],
    "issues_discovered": ["a synthetic pitfall to watch"],
    "self_heal": None,
    "cites_precedent": ["docs/demo-precedent.md"],
    "supersedes": None,
    "deprecates": None,
}

FIX_A_D38 = {
    "run_id": "demo-run-d38",
    "schema": "saga-next-session/v1-D38",
    "status": "closed-pr-open",
    "git_preconditions": {
        "repo": "demo-repo",
        "worktree": "/tmp/demo-d38",
        "branch": "demo/d38",
        "expected_head": "deadbeef",
        "expected_head_mode": "exact",
    },
    "prompt_pointer": "saga-bundle/accepted-contract.md",
    "next_action": "RUN COMPLETE. PR open; merge is the human's.",
    "commands_run": ["demo cmd"],
    "issues_discovered": [],
    "reason": "run complete",
    "last_updated": "2026-01-03",
}

# git_preconditions as prose (regression: must not crash on str.get).
FIX_A_PROSE_GP = {
    "status": "blocked",
    "blocked_reason": "waiting on a synthetic gate",
    "expected_head_mode": "ancestor",
    "self_heal": None,
    "worktree": "/tmp/demo-prose",
    "branch": "demo/prose",
    "expected_head": "cafe123",
    "git_preconditions": "branch demo/prose; working tree CLEAN; HEAD is a descendant.",
    "commands_run": [{"command": "demo canary", "result": "ok"}],
    "issues_discovered": ["one synthetic issue"],
}

FIX_B = {
    "schema": "next-session/v1",
    "written_at": "2026-01-04T03:45:00Z",
    "session": "Synthetic optimus session recap prose.",
    "state": {
        "blocked_on_anthony": "a synthetic decision is pending",
        "next_in_order": "do the next synthetic step",
    },
    "traps": ["synthetic trap one", "synthetic trap two"],
}

FIX_B_NEXTOBJ = {
    "schema": "next-session/v1",
    "written_at": "2026-01-05T00:00:00Z",
    "session": "Another synthetic session.",
    "state": "free-form prose state here",
    "landmines": ["synthetic landmine"],
    "next_action": {"who": "opus", "what": "ship the widget", "then": "await merge"},
}

FIX_D = {
    "version": 34,
    "written": "2026-01-06T07:30 local",
    "read_first": ["docs/demo-context.md", "docs/demo-roadmap.md"],
    "state": "Synthetic console state prose; everything green.",
    "next": ["first synthetic next step", "second synthetic next step"],
    "anthony_solo": ["a human-only synthetic decision"],
    "evidence": {"build": "green", "tests": "passing"},
    "gotchas": ["a synthetic gotcha"],
    "live_worktrees": ["/tmp/demo-live"],
}

FIX_E_VALID = {
    "written": "2026-01-07",
    "by": "Synthetic Codex packet session",
    "next_session_role": "continue the synthetic engine sync",
    "worktree": "/tmp/demo-e",
    "branch": "demo/e-branch",
    "base": "1111111",
    "local_commit": "2222222",
    "pull_request": {"url": "https://example.invalid/pr/1", "state": "OPEN",
                     "mergeStateStatus": "BLOCKED"},
    "read_in_order": ["docs/demo-read-1.md", "docs/demo-read-2.md"],
    "deliverables": ["synthetic deliverable A"],
    "completed": ["synthetic completed B"],
    "gates_passed": ["synthetic gate passed"],
    "verification": ["synthetic verification line"],
    "warnings": ["a synthetic warning"],
    "next_actions": ["do the next synthetic packet action"],
}

# Deliberately INVALID: two E packets concatenated with the first packet's
# array left unterminated before the second packet's keys begin -- the exact
# corruption shape the E salvage adapter must survive.
FIX_E_INVALID = """{
  "written": "2026-01-08",
  "by": "Synthetic Codex packet one",
  "next_session_role": "first synthetic role",
  "worktree": "/tmp/demo-e1",
  "branch": "demo/e1",
  "read_in_order": ["docs/demo-a.md", "docs/demo-b.md"],
  "gates": [
    "first synthetic gate",
    "second synthetic gate",
  "by": "Synthetic Codex packet two",
  "next_session_role": "second synthetic role",
  "worktree": "/tmp/demo-e2",
  "branch": "demo/e2",
  "pull_request": {
    "url": "https://example.invalid/pr/2",
"""

FIX_V2 = {
    "schema": "next-session/v2",
    "written_at": "2026-01-09T00:00:00Z",
    "environment": "demo-env",
    "branch": "demo/v2",
    "worktree": "/tmp/demo-v2",
    "expected_head": "3333333",
    "status": "in-progress",
    "summary": "A synthetic v2 handoff.",
    "next_action": {"who": "fable", "what": "review the synthetic diff", "then": "delegate"},
    "human_only": ["a synthetic human decision"],
    "pitfalls": ["synthetic pitfall"],
    "read_first": ["docs/demo-v2.md"],
    "evidence": {"ci": "green"},
}

# The real-world shape (NOT the real content) of the v2-TAGGED files on this
# machine: the v2 schema tag over the free-form fallback field set. Passthrough
# alone left these with an empty summary, so authored content -- including a
# parked directive -- never reached a reader.
FIX_V2_FALLBACK_SHAPE = {
    "schema": "next-session/v2",
    "session_date": "2026-01-11",
    "project": "demo-parked",
    "state": "PARKED pending a synthetic ruling: nothing built here, no daemons "
             "installed. No build work until the ruling lands.",
    "launch_one_liner": "Parked -- consult the synthetic ruling first.",
    "next_action_for_agent": "Read the synthetic ruling doc. Do not install "
                             "anything or build until it lands.",
}

FIX_V2_FALLBACK_NO_ACTION = {
    "schema": "next-session/v2",
    "session_date": "2026-01-12",
    "project": "demo-live",
    "state": "Synthetic live lane, work in progress on the demo widget.",
    "launch_one_liner": "Check the synthetic queue before building.",
}

FIX_FALLBACK = {
    "session_date": "2026-01-10",
    "project": "Synthetic free-form project line.",
    "state": "No live processes; synthetic idle state.",
    "launch_one_liner": "Read the synthetic handoff and continue.",
    "next_action_for_agent": "confirm the synthetic queue then proceed",
    "anthony_decisions_pending": ["a synthetic pending decision"],
    "guardrails": ["a synthetic guardrail"],
}


class AdapterTests(unittest.TestCase):
    def _require(self, v2):
        for f in ("schema", "written_at", "environment", "status", "summary",
                  "next_action"):
            self.assertIn(f, v2, f"missing required v2 field: {f}")
        self.assertEqual(v2["schema"], ns.V2_SCHEMA)
        self.assertIn(v2["status"], ns.V2_STATUS)
        self.assertIn("who", v2["next_action"])
        self.assertIn("what", v2["next_action"])

    def test_A(self):
        v2 = ns.adapt_A(FIX_A, "demo-env")
        self._require(v2)
        self.assertEqual(v2["status"], "done")
        self.assertEqual(v2["written_at"], "2026-01-02")
        self.assertEqual(v2["branch"], "saga/demo-branch")
        self.assertEqual(v2["expected_head"], "abc1234")
        self.assertEqual(v2["next_action"]["who"], "op-unit-7 (demo)")
        self.assertIn("a synthetic pitfall to watch", v2["pitfalls"])
        self.assertIn("docs/demo-precedent.md", v2["read_first"])
        self.assertEqual(v2["evidence"]["commands_run"], ["demo build", "demo test"])
        # unmapped fields land verbatim in legacy
        self.assertIn("id", v2["legacy"])
        self.assertIn("procedures_abided", v2["legacy"])

    def test_A_d38_nested_git(self):
        v2 = ns.adapt_A(FIX_A_D38, "")
        self._require(v2)
        self.assertEqual(v2["branch"], "demo/d38")
        self.assertEqual(v2["worktree"], "/tmp/demo-d38")
        self.assertEqual(v2["expected_head"], "deadbeef")
        self.assertEqual(v2["environment"], "demo-repo")
        self.assertEqual(v2["written_at"], "2026-01-03")

    def test_A_prose_git_preconditions_does_not_crash(self):
        v2 = ns.adapt_A(FIX_A_PROSE_GP, "demo-env")  # must not raise
        self._require(v2)
        self.assertEqual(v2["status"], "blocked")

    def test_B_state_object(self):
        v2 = ns.adapt_B(FIX_B, "demo-env")
        self._require(v2)
        self.assertEqual(v2["next_action"]["what"], "do the next synthetic step")
        self.assertEqual(v2["status"], "blocked")
        self.assertEqual(set(v2["pitfalls"]),
                         {"synthetic trap one", "synthetic trap two"})

    def test_B_next_action_object_wins(self):
        v2 = ns.adapt_B(FIX_B_NEXTOBJ, "demo-env")
        self._require(v2)
        self.assertEqual(v2["next_action"]["who"], "opus")
        self.assertEqual(v2["next_action"]["what"], "ship the widget")
        self.assertEqual(v2["next_action"]["then"], "await merge")
        self.assertIn("synthetic landmine", v2["pitfalls"])

    def test_D(self):
        v2 = ns.adapt_D(FIX_D, "demo-env")
        self._require(v2)
        self.assertEqual(v2["next_action"]["what"], "first synthetic next step")
        self.assertIn("second synthetic next step", v2["next_action"]["then"])
        self.assertEqual(v2["human_only"], ["a human-only synthetic decision"])
        self.assertIn("a synthetic gotcha", v2["pitfalls"])
        self.assertEqual(v2["read_first"], ["docs/demo-context.md", "docs/demo-roadmap.md"])
        self.assertEqual(v2["evidence"]["build"], "green")

    def test_E_valid_single(self):
        v2 = ns.adapt_E([FIX_E_VALID], "demo-env", recovered=False)
        self._require(v2)
        self.assertEqual(v2["next_action"]["who"], "Synthetic Codex packet session")
        self.assertIn("next synthetic packet action", v2["next_action"]["what"])
        self.assertEqual(v2["status"], "blocked")  # PR OPEN + BLOCKED
        self.assertEqual(v2["branch"], "demo/e-branch")
        self.assertEqual(v2["expected_head"], "2222222")  # local_commit
        self.assertIn("a synthetic warning", v2["pitfalls"])
        self.assertEqual(v2["read_first"], ["docs/demo-read-1.md", "docs/demo-read-2.md"])
        self.assertIn("gates_passed", v2["evidence"])

    def test_E_invalid_concatenated_salvage(self):
        records, mode = ns._lenient_parse(FIX_E_INVALID)
        self.assertTrue(records, "salvage must recover at least one record")
        self.assertEqual(mode, "salvaged")
        v2 = ns.adapt_E(records, "demo-env", recovered=True)
        self._require(v2)
        # Leading packet's early fields survive the corruption.
        self.assertEqual(v2["written_at"], "2026-01-08")
        self.assertEqual(v2["next_action"]["who"], "Synthetic Codex packet one")
        self.assertTrue(v2["legacy"].get("_recovered_from_invalid_json"))

    def test_v2_passthrough(self):
        v2 = ns.adapt_v2(FIX_V2, "other-env")
        self._require(v2)
        self.assertEqual(v2["environment"], "demo-env")  # keeps its own
        self.assertEqual(v2["status"], "in-progress")
        self.assertEqual(v2["next_action"]["then"], "delegate")

    def test_v2_tagged_fallback_shape_is_gap_filled(self):
        v2 = ns.adapt_v2(FIX_V2_FALLBACK_SHAPE, "demo-env")
        self._require(v2)
        self.assertTrue(v2["summary"], "v2-tagged fallback shape lost its summary")
        self.assertIn("PARKED pending a synthetic ruling", v2["summary"])
        self.assertEqual(v2["status"], "parked")
        self.assertEqual(v2["written_at"], "2026-01-11")
        self.assertIn("Read the synthetic ruling doc", v2["next_action"]["what"])

    def test_v2_tagged_fallback_falls_back_to_launch_one_liner(self):
        v2 = ns.adapt_v2(FIX_V2_FALLBACK_NO_ACTION, "demo-env")
        self._require(v2)
        self.assertEqual(v2["next_action"]["what"],
                         "Check the synthetic queue before building.")
        self.assertEqual(v2["status"], "in-progress")  # from state prose
        self.assertEqual(v2["written_at"], "2026-01-12")

    def test_v2_gap_fill_leaves_conforming_v2_untouched(self):
        conforming = ns.adapt_v2(FIX_V2, "other-env")
        self.assertEqual(conforming["summary"], "A synthetic v2 handoff.")
        self.assertEqual(conforming["status"], "in-progress")
        self.assertEqual(conforming["written_at"], "2026-01-09T00:00:00Z")
        self.assertEqual(conforming["next_action"]["what"],
                         "review the synthetic diff")
        # A v2 file that also carries a fallback key keeps its own fields.
        mixed = dict(FIX_V2)
        mixed["state"] = "some stray prose that must not win"
        mixed["session_date"] = "1999-01-01"
        out = ns.adapt_v2(mixed, "other-env")
        self.assertEqual(out["summary"], "A synthetic v2 handoff.")
        self.assertEqual(out["written_at"], "2026-01-09T00:00:00Z")
        self.assertEqual(out["status"], "in-progress")

    def test_v2_tagged_fallback_shape_through_normalize_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(os.path.join(d, "NEXT_SESSION.json"),
                       json.dumps(FIX_V2_FALLBACK_SHAPE))
            rec = ns.normalize_file(p)
            self.assertEqual(rec["detected_schema"], "v2")
            self.assertIn("PARKED", rec["normalized"]["summary"])

    def test_fallback(self):
        v2 = ns.adapt_fallback(FIX_FALLBACK, "demo-env")
        self._require(v2)
        self.assertEqual(v2["next_action"]["what"], "confirm the synthetic queue then proceed")
        self.assertIn("a synthetic pending decision", v2["human_only"])
        self.assertIn("a synthetic guardrail", v2["pitfalls"])


class StatusMappingTests(unittest.TestCase):
    def test_maps(self):
        self.assertEqual(ns._norm_status("done"), "done")
        self.assertEqual(ns._norm_status("run_closed"), "done")
        self.assertEqual(ns._norm_status("blocked on the gate"), "blocked")
        self.assertEqual(ns._norm_status("awaiting merge"), "blocked")
        self.assertEqual(ns._norm_status("work in progress"), "in-progress")
        self.assertEqual(ns._norm_status("parked for now"), "parked")
        self.assertIsNone(ns._norm_status("qwerty gibberish"))
        self.assertIsNone(ns._norm_status(""))

    def test_default_parked_when_undeterminable(self):
        v2 = ns._v2(written_at="", environment="e", status=None, summary="s",
                    who="", what="")
        self.assertEqual(v2["status"], "parked")


class ClassifyTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(ns._classify_dict(FIX_A), "A")
        self.assertEqual(ns._classify_dict(FIX_A_D38), "A")
        self.assertEqual(ns._classify_dict(FIX_B), "B")
        self.assertEqual(ns._classify_dict(FIX_D), "D")
        self.assertEqual(ns._classify_dict(FIX_E_VALID), "E")
        self.assertEqual(ns._classify_dict(FIX_V2), "v2")
        self.assertEqual(ns._classify_dict(FIX_FALLBACK), "fallback")


class FileAndScanTests(unittest.TestCase):
    def test_read_only_never_mutates(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(os.path.join(d, "NEXT_SESSION.json"), json.dumps(FIX_A))
            with open(p, encoding="utf-8") as fh:
                before = fh.read()
            ns.normalize_file(p)
            with open(p, encoding="utf-8") as fh:
                after = fh.read()
            self.assertEqual(before, after)

    def test_c_exclusion_by_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(
                os.path.join(d, "tools", "mission-control", "tests", "fixtures",
                             "heartbeat-bundle", "NEXT_SESSION.json"),
                json.dumps(FIX_A))
            rec = ns.normalize_file(p)
            self.assertEqual(rec["detected_schema"], "C")
            self.assertTrue(rec["excluded"])
            self.assertNotIn("normalized", rec)

    def test_invalid_e_file_normalizes(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(os.path.join(d, "NEXT_SESSION.json"), FIX_E_INVALID)
            rec = ns.normalize_file(p)
            self.assertEqual(rec["detected_schema"], "E")
            self.assertIn("normalized", rec)
            self.assertFalse(rec.get("unparseable"))

    def test_unrecoverable_is_explicit_unparseable(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(os.path.join(d, "NEXT_SESSION.json"), "}}}not json at all[[[")
            rec = ns.normalize_file(p)
            self.assertTrue(rec["unparseable"])
            self.assertIn("error", rec)

    def test_scan_counts_and_no_unparseable(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "a", "NEXT_SESSION.json"), json.dumps(FIX_A))
            _write(os.path.join(d, "b", "NEXT_SESSION.json"), json.dumps(FIX_B))
            _write(os.path.join(d, "dd", "NEXT_SESSION.json"), json.dumps(FIX_D))
            _write(os.path.join(d, "e", "NEXT_SESSION.json"), FIX_E_INVALID)
            _write(os.path.join(d, "v", "NEXT_SESSION.json"), json.dumps(FIX_V2))
            # excluded fixture + skipped node_modules
            _write(os.path.join(d, "x", "tests", "fixtures", "heartbeat-bundle",
                                "NEXT_SESSION.json"), json.dumps(FIX_A))
            _write(os.path.join(d, "node_modules", "pkg", "NEXT_SESSION.json"),
                   json.dumps(FIX_A))
            out, err = io.StringIO(), io.StringIO()
            summary = ns.scan(d, jsonl=True, out=out, err=err)
            self.assertEqual(summary["unparseable"], 0)
            self.assertEqual(summary.get("A"), 1)
            self.assertEqual(summary.get("E"), 1)
            self.assertEqual(summary.get("C"), 1)
            self.assertEqual(summary["total"], 6)  # node_modules skipped
            lines = [json.loads(x) for x in out.getvalue().splitlines()]
            self.assertEqual(len(lines), 6)


if __name__ == "__main__":
    unittest.main()
