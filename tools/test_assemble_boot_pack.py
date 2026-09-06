#!/usr/bin/env python3
"""Tests for assemble-boot-pack.py -- stdlib unittest, no third-party deps.

All fixtures are SYNTHETIC. No real client names, no real repo content. The
assembler filename has a hyphen, so it is loaded via importlib. Run with:

    python3 -m unittest test_assemble_boot_pack -v
"""

import copy
import datetime
import importlib.util
import json
import os
import re
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_assembler():
    spec = importlib.util.spec_from_file_location(
        "assemble_boot_pack", os.path.join(_HERE, "assemble-boot-pack.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ab = _load_assembler()

NOW = datetime.datetime(2026, 7, 23, 12, 0, 0, tzinfo=datetime.timezone.utc)
UTC = datetime.timezone.utc


# --------------------------------------------------------------------------- #
# Synthetic input builders
# --------------------------------------------------------------------------- #
def _manifest(repos=None, as_of="2026-07-23"):
    return {
        "schema": "seed-manifest/v1",
        "provenance": "manual",
        "as_of": as_of,
        "repos": repos if repos is not None else [
            {"environment": "synth-a", "repo_path": "/nonexistent/synth-a",
             "role": "synthetic repo A", "status": "canonical", "kind": "test",
             "owning_system": "synth", "default_branch": "main", "pins": []},
        ],
    }


def _backlog(items=None, as_of="2026-07-01"):
    return {"present": True, "as_of": as_of,
            "source": "tools/configs/ratification-backlog.json",
            "items": items if items is not None else [
                {"id": "RB-1", "summary": "a synthetic standing ratification",
                 "kind": "ratification", "waiting_since": "2026-07-01",
                 "source": "ratification-backlog.json"}]}


def _next_session(status="parked", summary="a synthetic parked handoff",
                  as_of="2026-07-22T00:00:00+00:00"):
    """A surface next_session probe carrying a normalized v2 record."""
    return {"source": "next_session.py:v2", "as_of": as_of,
            "normalized": {"schema": "next-session/v2", "written_at": as_of,
                           "environment": "synth-a", "status": status,
                           "summary": summary,
                           "next_action": {"who": "", "what": "wait"}}}


def _surface(env, generated_at, decisions=None, next_lanes=None, cold=None,
             next_session=None):
    return {
        "schema": "surface/v1",
        "environment": env,
        "source_commit_time": "2026-07-20T00:00:00+00:00",
        "generated_at": generated_at,
        "identity": {"repo_path": "/nonexistent/%s" % env, "canonical": True,
                     "role": "synthetic %s" % env, "default_branch": "main",
                     "head_sha": "abc1234", "behind_origin_main": 0,
                     "source": "git", "as_of": "2026-07-20T00:00:00+00:00"},
        "cold_load": cold if cold is not None else [
            {"claim": "a synthetic cold fact", "source": "config",
             "as_of": "2026-07-20"}],
        "state": {"open_prs": [], "active_branches": [], "verification": []},
        "decisions": decisions or [],
        "next_lanes": next_lanes or [],
        "do_not_read": [], "pitfalls": [],
        "next_session": next_session if next_session is not None
        else {"degraded": True, "reason": "none"},
    }


def _write_surface(cache_dir, surface):
    os.makedirs(cache_dir, exist_ok=True)
    p = os.path.join(cache_dir, surface["environment"] + ".json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(surface, fh)
    return p


def _scan_artifact(schema, entries=None, extra=None):
    d = {"schema": schema, "generated_at": "2026-07-22T00:00:00+00:00",
         "producer": {"name": "doctrine-compiler", "version": "0.1.0",
                      "commit": "deadbeefcafe"},
         "entries": entries if entries is not None else []}
    if extra:
        d.update(extra)
    return d


def _write(path, obj_or_text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(obj_or_text, str):
            fh.write(obj_or_text)
        else:
            json.dump(obj_or_text, fh)
    return path


def _carve(obj):
    """Recursively blank volatile as_of/generated_at (wall-clock) fields."""
    obj = copy.deepcopy(obj)

    def scrub(x):
        if isinstance(x, dict):
            for k in list(x):
                if k in ("generated_at", "as_of"):
                    x[k] = "<carved>"
                else:
                    scrub(x[k])
        elif isinstance(x, list):
            for i in x:
                scrub(i)
    scrub(obj)
    return obj


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
class TimeHelpers(unittest.TestCase):
    def test_parse_ts_variants(self):
        for s in ("2026-07-01", "2026-07-01T00:00:00Z",
                  "2026-07-01T00:00:00+00:00", "2026-07-01 00:00:00"):
            self.assertIsNotNone(ab.parse_ts(s), s)
        self.assertIsNone(ab.parse_ts(""))
        self.assertIsNone(ab.parse_ts("not a date"))

    def test_stale_topology_threshold_7d(self):
        # 22 days old -> stale; same-day -> fresh.
        self.assertTrue(ab.stale_prefix(NOW, "2026-07-01", ab.STALE_TOPOLOGY))
        self.assertFalse(ab.stale_prefix(NOW, "2026-07-23", ab.STALE_TOPOLOGY))

    def test_stale_surfaces_threshold_24h(self):
        two_days = (NOW - datetime.timedelta(days=2)).isoformat()
        ten_min = (NOW - datetime.timedelta(minutes=10)).isoformat()
        self.assertTrue(ab.stale_prefix(NOW, two_days, ab.STALE_SURFACES))
        self.assertFalse(ab.stale_prefix(NOW, ten_min, ab.STALE_SURFACES))

    def test_stale_decisions_threshold_1h(self):
        two_h = (NOW - datetime.timedelta(hours=2)).isoformat()
        ten_min = (NOW - datetime.timedelta(minutes=10)).isoformat()
        self.assertTrue(ab.stale_prefix(NOW, two_h, ab.STALE_DECISIONS))
        self.assertFalse(ab.stale_prefix(NOW, ten_min, ab.STALE_DECISIONS))

    def test_unknown_as_of_renders_unparseable_marker(self):
        # No extractable date -> AS_OF-UNPARSEABLE (fail-visible), NOT STALE(?).
        self.assertEqual(ab.stale_prefix(NOW, "", ab.STALE_DECISIONS),
                         "AS_OF-UNPARSEABLE ")
        self.assertEqual(ab.stale_prefix(NOW, "no date here", ab.STALE_DECISIONS),
                         "AS_OF-UNPARSEABLE ")


# --------------------------------------------------------------------------- #
# Section assembly + order
# --------------------------------------------------------------------------- #
class SectionAssembly(unittest.TestCase):
    def _build(self, tmp, **kw):
        cache = os.path.join(tmp, "surfaces")
        _write_surface(cache, _surface("synth-a", NOW.isoformat()))
        md, sidecar = ab.build_pack(
            NOW, _manifest(), _backlog(), cache,
            kw.get("scan_dir"), cap_tokens=kw.get("cap", ab.HARD_CAP_TOKENS))
        return md, sidecar

    def test_all_four_sections_present_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, _ = self._build(tmp)
            i1 = md.index("## 1. Topology manifest")
            i2 = md.index("## 2. State surfaces")
            i3 = md.index("## 3. Unified decision queue")
            i_plans = md.index("## 4. Plans (ledger-backed, generated)")
            i4 = md.index("## 5. Scan / priorities feed")
            self.assertLess(i1, i2)
            self.assertLess(i2, i3)
            self.assertLess(i3, i_plans)
            self.assertLess(i_plans, i4)

    def test_sidecar_section_key_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sidecar = self._build(tmp)
            self.assertEqual(list(sidecar["sections"].keys()),
                             ["topology", "surfaces", "decision_queue",
                              "plans", "scan"])
            self.assertEqual(sidecar["schema"], "boot-pack/v1")

    def test_header_states_advisory_not_law(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp)
            self.assertIn("ADVISORY STATE, NOT LAW", md)
            self.assertIn("ADVISORY STATE, NOT LAW", sidecar["advisory"])

    def test_queue_is_projection_not_absorbing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sidecar = self._build(tmp)
            dq = sidecar["sections"]["decision_queue"]
            self.assertTrue(dq["projection"])
            self.assertFalse(dq["absorbs_ratification"])

    def test_cos_v2_open_item_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp)
            self.assertTrue(any("COS v2" in o for o in sidecar["open_items"]))
            self.assertIn("OPEN-ITEM:", md)
            self.assertIn("COS v2", md)


# --------------------------------------------------------------------------- #
# [source | as_of] provenance tagging
# --------------------------------------------------------------------------- #
class ProvenanceTagging(unittest.TestCase):
    _TAG = re.compile(r"\[[^\]]+\|[^\]]+\]")

    def test_every_topology_and_queue_line_is_tagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface(
                "synth-a", NOW.isoformat(),
                decisions=[{"id": "D-1", "summary": "synthetic decision",
                            "kind": "ruling", "waiting_since": "2026-07-01",
                            "source": "config"}]))
            md, _ = ab.build_pack(NOW, _manifest(), _backlog(), cache, None)
            # Topology entry path lines carry a tag.
            topo = md[md.index("## 1."):md.index("## 2.")]
            self.assertTrue(self._TAG.search(topo))
            # Queue lines carry a tag.
            q = md[md.index("## 3."):md.index("## 4.")]
            for line in q.splitlines():
                if line.startswith("- "):
                    self.assertTrue(self._TAG.search(line),
                                    "queue line untagged: %r" % line)


# --------------------------------------------------------------------------- #
# Degraded-source WARNING
# --------------------------------------------------------------------------- #
class DegradedSources(unittest.TestCase):
    def test_missing_surface_emits_warning_never_crashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")  # empty: no surface written
            os.makedirs(cache, exist_ok=True)
            md, sidecar = ab.build_pack(NOW, _manifest(), _backlog(), cache, None)
            self.assertTrue(any("surface DEGRADED for synth-a" in w
                                for w in sidecar["warnings"]))
            self.assertIn("## WARNINGS (degraded sources)", md)
            self.assertIn("WARNING: surface unavailable", md)

    def test_invalid_surface_json_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            _write(os.path.join(cache, "synth-a.json"), "{ not json")
            md, sidecar = ab.build_pack(NOW, _manifest(), _backlog(), cache, None)
            self.assertTrue(any("synth-a" in w for w in sidecar["warnings"]))


# --------------------------------------------------------------------------- #
# Staleness marking end-to-end
# --------------------------------------------------------------------------- #
class StalenessMarking(unittest.TestCase):
    def test_stale_surface_marked_fresh_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            stale_gen = (NOW - datetime.timedelta(days=3)).isoformat()
            _write_surface(cache, _surface("synth-a", stale_gen))
            md, _ = ab.build_pack(NOW, _manifest(), _backlog(), cache, None)
            surf = md[md.index("## 2."):md.index("## 3.")]
            self.assertIn("STALE(", surf)

            cache2 = os.path.join(tmp, "surfaces2")
            _write_surface(cache2, _surface("synth-a", NOW.isoformat()))
            md2, _ = ab.build_pack(NOW, _manifest(), _backlog(), cache2, None)
            surf2 = md2[md2.index("## 2."):md2.index("## 3.")]
            # The repo header line should not carry STALE when fresh.
            hdr = [l for l in surf2.splitlines() if "**synth-a**" in l][0]
            self.assertNotIn("STALE(", hdr)

    def test_stale_topology_when_manifest_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface("synth-a", NOW.isoformat()))
            md, _ = ab.build_pack(
                NOW, _manifest(as_of="2026-06-01"), _backlog(), cache, None)
            topo = md[md.index("## 1."):md.index("## 2.")]
            self.assertIn("STALE(", topo)

    def test_stale_decision_item_1h(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface("synth-a", NOW.isoformat()))
            # backlog item waiting since well over 1h -> STALE in queue.
            md, _ = ab.build_pack(NOW, _manifest(), _backlog(), cache, None)
            q = md[md.index("## 3."):md.index("## 4.")]
            self.assertIn("STALE(", q)


# --------------------------------------------------------------------------- #
# Decision-queue human_only feed (via M1 parser)
# --------------------------------------------------------------------------- #
class HumanOnlyFeed(unittest.TestCase):
    def test_human_only_from_next_session_projected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo-a")
            os.makedirs(repo)
            _write(os.path.join(repo, "NEXT_SESSION.json"), {
                "schema": "next-session/v2", "written_at": "2026-07-22T00:00:00Z",
                "environment": "synth-a", "status": "blocked",
                "summary": "synthetic", "next_action": {"who": "a", "what": "b"},
                "human_only": ["a human-only synthetic decision"]})
            man = _manifest([{"environment": "synth-a", "repo_path": repo,
                              "role": "r", "status": "canonical", "kind": "t",
                              "owning_system": "s", "default_branch": "main",
                              "pins": []}])
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface("synth-a", NOW.isoformat()))
            md, sidecar = ab.build_pack(NOW, man, _backlog(items=[]), cache, None)
            items = sidecar["sections"]["decision_queue"]["items"]
            self.assertTrue(any(i["kind"] == "human_only" and
                                "human-only synthetic" in i["summary"]
                                for i in items))
            self.assertIn("a human-only synthetic decision", md)


# --------------------------------------------------------------------------- #
# Scan-artifact independent validation
# --------------------------------------------------------------------------- #
class ScanValidation(unittest.TestCase):
    def test_absent_dir_renders_no_artifacts_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface("synth-a", NOW.isoformat()))
            md, sidecar = ab.build_pack(NOW, _manifest(), _backlog(), cache,
                                        os.path.join(tmp, "no-such-scan"))
            self.assertIn("no scan artifacts found", md)
            self.assertFalse(sidecar["sections"]["scan"]["present"])

    def test_one_bad_artifact_does_not_kill_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = os.path.join(tmp, "scan")
            os.makedirs(scan)
            # valid weights
            _write(os.path.join(scan, "weights.json"), _scan_artifact(
                "saga-scan/doctrine-weights@v1",
                entries=[{"id": "w1"}],
                extra={"budget_split": {"doctrine": 0.7}}))
            # invalid suppression (not JSON)
            _write(os.path.join(scan, "suppression.json"), "{ broken")
            # rejected major on scan-targets (@v2)
            _write(os.path.join(scan, "scan-targets.json"), _scan_artifact(
                "saga-scan/staleness-targets@v2", entries=[{"id": "t1"}]))
            # intentions.json absent (optional)
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface("synth-a", NOW.isoformat()))
            md, sidecar = ab.build_pack(NOW, _manifest(), _backlog(), cache, scan)

            arts = {a["file"]: a for a in sidecar["sections"]["scan"]["artifacts"]}
            self.assertEqual(arts["weights.json"]["status"], "ok")
            self.assertEqual(arts["weights.json"]["entries"], 1)
            self.assertEqual(arts["suppression.json"]["status"], "invalid")
            self.assertEqual(arts["scan-targets.json"]["status"], "rejected-major")
            self.assertEqual(arts["intentions.json"]["status"], "absent")
            # cycle survived: pack still assembled with all 4 sections
            self.assertIn("## 5. Scan / priorities feed", md)
            self.assertIn("weights.json (required): OK", md)
            self.assertIn("WARNING", md)

    def test_unknown_keys_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = os.path.join(tmp, "scan")
            os.makedirs(scan)
            art = _scan_artifact("saga-scan/doctrine-weights@v1",
                                 entries=[{"id": "w1"}],
                                 extra={"totally_unknown_future_key": {"x": 1}})
            _write(os.path.join(scan, "weights.json"), art)
            res = ab.validate_scan_artifact(
                os.path.join(scan, "weights.json"), "saga-scan/doctrine-weights")
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["entries"], 1)


# --------------------------------------------------------------------------- #
# Hard-cap truncation
# --------------------------------------------------------------------------- #
class Truncation(unittest.TestCase):
    def test_truncates_next_lanes_with_explicit_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            lanes = [{"entry": "L-%02d synthetic lane with padding text" % i,
                      "source": "config", "as_of": "2026-07-20"}
                     for i in range(40)]
            _write_surface(cache, _surface("synth-a", NOW.isoformat(),
                                           next_lanes=lanes))
            # Cap set above the irreducible base-pack floor but below
            # base+lanes, so shedding the next_lanes tier lands under the cap.
            md, sidecar = ab.build_pack(NOW, _manifest(), _backlog(), cache, None,
                                        cap_tokens=800)
            self.assertIn("TRUNCATED:", md)
            self.assertTrue(sidecar["truncation"])
            self.assertGreater(sidecar["truncation"][0]["next_lanes_truncated"], 0)
            self.assertLessEqual(sidecar["token_estimate"], 800)

    def test_no_truncation_under_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface(
                "synth-a", NOW.isoformat(),
                next_lanes=[{"entry": "L-1", "source": "config",
                             "as_of": "2026-07-20"}]))
            md, sidecar = ab.build_pack(NOW, _manifest(), _backlog(), cache, None)
            self.assertNotIn("TRUNCATED:", md)
            self.assertEqual(sidecar["truncation"], [])


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
class Determinism(unittest.TestCase):
    def _inputs(self, tmp):
        cache = os.path.join(tmp, "surfaces")
        _write_surface(cache, _surface(
            "synth-a", (NOW - datetime.timedelta(days=2)).isoformat(),
            decisions=[{"id": "D-1", "summary": "synthetic decision",
                        "kind": "ruling", "waiting_since": "2026-07-01",
                        "source": "config"}],
            next_lanes=[{"entry": "L-1", "source": "config",
                         "as_of": "2026-07-20"}]))
        scan = os.path.join(tmp, "scan")
        os.makedirs(scan)
        _write(os.path.join(scan, "weights.json"),
               _scan_artifact("saga-scan/doctrine-weights@v1",
                              entries=[{"id": "w1"}]))
        return _manifest(), _backlog(), cache, scan

    def test_byte_identical_same_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            man, bl, cache, scan = self._inputs(tmp)
            md1, s1 = ab.build_pack(NOW, man, bl, cache, scan)
            md2, s2 = ab.build_pack(NOW, man, bl, cache, scan)
            self.assertEqual(md1, md2)
            self.assertEqual(ab.render_sidecar(s1), ab.render_sidecar(s2))

    def test_sidecar_identical_across_different_now_after_carving(self):
        with tempfile.TemporaryDirectory() as tmp:
            man, bl, cache, scan = self._inputs(tmp)
            _, s1 = ab.build_pack(NOW, man, bl, cache, scan)
            later = NOW + datetime.timedelta(minutes=17)
            _, s2 = ab.build_pack(later, man, bl, cache, scan)
            self.assertEqual(json.dumps(_carve(s1), sort_keys=True),
                             json.dumps(_carve(s2), sort_keys=True))

    def test_render_single_trailing_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            man, bl, cache, scan = self._inputs(tmp)
            md, _ = ab.build_pack(NOW, man, bl, cache, scan)
            self.assertTrue(md.endswith("\n"))
            self.assertFalse(md.endswith("\n\n\n"))


# --------------------------------------------------------------------------- #
# Fix 1: free-text as_of date extraction + unparseable fallback + sort
# --------------------------------------------------------------------------- #
class FreeTextAsOfExtraction(unittest.TestCase):
    def test_extracts_iso_prefix_from_smc_freetext(self):
        # The exact live SMC form: leading ISO datetime + trailing prose.
        s = "2026-07-23T07:30 local (approx, overnight 2026-07-23 session)"
        ts = ab.parse_ts(s)
        self.assertIsNotNone(ts)
        self.assertEqual(ts, datetime.datetime(2026, 7, 23, 7, 30, tzinfo=UTC))

    def test_extracts_date_only_prefix(self):
        ts = ab.parse_ts("2026-07-20 overnight session, see handoff")
        self.assertEqual(ts, datetime.datetime(2026, 7, 20, tzinfo=UTC))

    def test_extracts_prefix_with_seconds_and_offset(self):
        ts = ab.parse_ts("2026-07-23T07:30:15-07:00 (approx)")
        self.assertEqual(
            ts, datetime.datetime(2026, 7, 23, 14, 30, 15, tzinfo=UTC))

    def test_clean_values_still_parse(self):
        for s in ("2026-07-01", "2026-07-01T00:00:00Z",
                  "2026-07-01T00:00:00+00:00", "2026-07-01 00:00:00"):
            self.assertIsNotNone(ab.parse_ts(s), s)

    def test_truly_unparseable_returns_none(self):
        self.assertIsNone(ab.parse_ts("overnight session, no date"))
        self.assertIsNone(ab.parse_ts(""))

    def test_freetext_asof_not_marked_unparseable_in_queue(self):
        # A human_only item whose written_at is free-text with an ISO prefix
        # must show a real age, never AS_OF-UNPARSEABLE.
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo-smc")
            os.makedirs(repo)
            _write(os.path.join(repo, "NEXT_SESSION.json"), {
                "schema": "next-session/v2",
                "written_at":
                    "2026-07-23T07:30 local (approx, overnight 2026-07-23 session)",
                "environment": "synth-smc", "status": "blocked",
                "summary": "s", "next_action": {"who": "a", "what": "b"},
                "human_only": ["fire the p1-loop real-feature run in-app"]})
            man = _manifest([{"environment": "synth-smc", "repo_path": repo,
                              "role": "r", "status": "canonical", "kind": "t",
                              "owning_system": "s", "default_branch": "main",
                              "pins": []}])
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface("synth-smc", NOW.isoformat()))
            md, _ = ab.build_pack(NOW, man, _backlog(items=[]), cache, None)
            q = md[md.index("## 3."):md.index("## 4.")]
            line = [l for l in q.splitlines() if "p1-loop" in l][0]
            self.assertNotIn("AS_OF-UNPARSEABLE", line)
            self.assertIn("STALE(", line)  # written days before NOW -> stale age

    def test_unparseable_asof_marker_and_sorts_last(self):
        # Two queue items: one dated, one with no extractable date. The
        # undated one must render AS_OF-UNPARSEABLE and sort LAST.
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo-x")
            os.makedirs(repo)
            _write(os.path.join(repo, "NEXT_SESSION.json"), {
                "schema": "next-session/v2", "written_at": "no date at all",
                "environment": "synth-x", "status": "blocked", "summary": "s",
                "next_action": {"who": "a", "what": "b"},
                "human_only": ["ZZZ undated item text"]})
            man = _manifest([{"environment": "synth-x", "repo_path": repo,
                              "role": "r", "status": "canonical", "kind": "t",
                              "owning_system": "s", "default_branch": "main",
                              "pins": []}])
            cache = os.path.join(tmp, "surfaces")
            # a surface decision WITH a date so it sorts before the undated one
            _write_surface(cache, _surface(
                "synth-x", NOW.isoformat(),
                decisions=[{"id": "D-1", "summary": "AAA dated decision",
                            "kind": "ruling", "waiting_since": "2026-07-01",
                            "source": "config"}]))
            md, sidecar = ab.build_pack(NOW, man, _backlog(items=[]), cache, None)
            items = sidecar["sections"]["decision_queue"]["items"]
            summaries = [i["summary"] for i in items]
            self.assertEqual(summaries[-1], "ZZZ undated item text")
            q = md[md.index("## 3."):md.index("## 4.")]
            undated = [l for l in q.splitlines() if "ZZZ undated" in l][0]
            self.assertIn("AS_OF-UNPARSEABLE", undated)

    def test_multiple_unparseable_stable_tiebreak(self):
        # Two undated items sort last, deterministically by (source, summary).
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo-y")
            os.makedirs(repo)
            _write(os.path.join(repo, "NEXT_SESSION.json"), {
                "schema": "next-session/v2", "written_at": "nope",
                "environment": "synth-y", "status": "blocked", "summary": "s",
                "next_action": {"who": "a", "what": "b"},
                "human_only": ["BBB second undated", "AAA first undated"]})
            man = _manifest([{"environment": "synth-y", "repo_path": repo,
                              "role": "r", "status": "canonical", "kind": "t",
                              "owning_system": "s", "default_branch": "main",
                              "pins": []}])
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface("synth-y", NOW.isoformat()))
            md1, s1 = ab.build_pack(NOW, man, _backlog(items=[]), cache, None)
            md2, s2 = ab.build_pack(NOW, man, _backlog(items=[]), cache, None)
            self.assertEqual(md1, md2)  # deterministic
            tail = [i["summary"] for i in
                    s1["sections"]["decision_queue"]["items"]][-2:]
            self.assertEqual(tail, ["AAA first undated", "BBB second undated"])


# --------------------------------------------------------------------------- #
# Fix 2: placeholder backlog item skipped + provenance footer
# --------------------------------------------------------------------------- #
class BacklogPlaceholder(unittest.TestCase):
    def _build(self, tmp, items):
        cache = os.path.join(tmp, "surfaces")
        _write_surface(cache, _surface("synth-a", NOW.isoformat()))
        return ab.build_pack(NOW, _manifest(), _backlog(items=items), cache, None)

    def test_placeholder_skipped_and_footer_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, [
                {"id": "RB-PLACEHOLDER-1",
                 "summary": "PLACEHOLDER -- replace me",
                 "kind": "ratification", "waiting_since": "2026-07-23",
                 "source": "tools/configs/ratification-backlog.json",
                 "placeholder": True}])
            items = sidecar["sections"]["decision_queue"]["items"]
            self.assertFalse(any("PLACEHOLDER" in i["summary"] for i in items))
            self.assertFalse(any(i["kind"] == "ratification" for i in items))
            q = md[md.index("## 3."):md.index("## 4.")]
            self.assertIn(
                "ratification backlog: 0 live items (seed placeholder skipped)", q)
            self.assertIn("tools/configs/ratification-backlog.json", q)

    def test_real_item_projected_no_placeholder_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, [
                {"id": "RB-1", "summary": "a real standing ratification",
                 "kind": "ratification", "waiting_since": "2026-07-01",
                 "source": "ratification-backlog.json"}])
            items = sidecar["sections"]["decision_queue"]["items"]
            self.assertTrue(any("a real standing ratification" in i["summary"]
                                for i in items))
            q = md[md.index("## 3."):md.index("## 4.")]
            self.assertIn("ratification backlog: 1 live items", q)
            self.assertNotIn("seed placeholder skipped", q)

    def test_mixed_real_and_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, [
                {"id": "RB-SEED", "summary": "PLACEHOLDER seed",
                 "kind": "ratification", "waiting_since": "2026-07-23",
                 "source": "ratification-backlog.json", "placeholder": True},
                {"id": "RB-REAL", "summary": "a genuine parked ratification",
                 "kind": "ratification", "waiting_since": "2026-07-01",
                 "source": "ratification-backlog.json"}])
            items = sidecar["sections"]["decision_queue"]["items"]
            self.assertTrue(any("genuine parked" in i["summary"] for i in items))
            self.assertFalse(any("PLACEHOLDER" in i["summary"] for i in items))
            q = md[md.index("## 3."):md.index("## 4.")]
            self.assertIn("ratification backlog: 1 live items "
                          "(seed placeholder skipped)", q)


# --------------------------------------------------------------------------- #
# Fix 3: degraded-probe warnings name the probe + reason
# --------------------------------------------------------------------------- #
class NamedDegradedProbes(unittest.TestCase):
    def test_warning_names_probe_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            surf = _surface("synth-a", NOW.isoformat())
            surf["next_session"] = {"degraded": True,
                                    "reason": "no root-level NEXT_SESSION.json"}
            _write_surface(cache, surf)
            md, sidecar = ab.build_pack(NOW, _manifest(), _backlog(), cache, None)
            want = ("surface for synth-a: degraded probe next_session "
                    "(no root-level NEXT_SESSION.json)")
            self.assertTrue(any(w == want for w in sidecar["warnings"]),
                            sidecar["warnings"])
            self.assertIn(want, md)
            # the old vague copy must be gone
            self.assertFalse(any("carries inline degraded probe(s)" in w
                                 for w in sidecar["warnings"]))

    def test_multiple_probes_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            surf = _surface("synth-a", NOW.isoformat())
            surf["next_session"] = {"degraded": True, "reason": "r1"}
            surf["identity"]["behind_origin_main"] = {"degraded": True,
                                                      "reason": "r2"}
            _write_surface(cache, surf)
            _, sidecar = ab.build_pack(NOW, _manifest(), _backlog(), cache, None)
            w = [x for x in sidecar["warnings"] if "degraded probe" in x][0]
            self.assertIn("next_session (r1)", w)
            self.assertIn("identity.behind_origin_main (r2)", w)

    def test_collect_degraded_probes_paths(self):
        data = {"a": {"degraded": True, "reason": "ra"},
                "b": {"c": [{"degraded": True, "reason": "rc"}]}}
        probes = dict(ab.collect_degraded_probes(data))
        self.assertEqual(probes["a"], "ra")
        self.assertEqual(probes["b.c[0]"], "rc")


# --------------------------------------------------------------------------- #
# Fix 5: surface.next_session projected into the pack (one line per repo)
# --------------------------------------------------------------------------- #
class NextSessionProjection(unittest.TestCase):
    def _build(self, tmp, surface):
        cache = os.path.join(tmp, "surfaces")
        _write_surface(cache, surface)
        return ab.build_pack(NOW, _manifest(), _backlog(), cache, None)

    def _ns_lines(self, md):
        surf = md[md.index("## 2."):md.index("## 3.")]
        return [l for l in surf.splitlines() if "next-session[" in l]

    def test_projected_into_md_and_sidecar(self):
        # The proof case: a parked directive that used to dead-end at the
        # surface must now be readable in the pack itself.
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, _surface(
                "synth-a", NOW.isoformat(),
                next_session=_next_session(
                    status="parked",
                    summary="PARKED pending a synthetic ruling -- do not "
                            "install anything")))
            lines = self._ns_lines(md)
            self.assertEqual(len(lines), 1, md)
            self.assertIn("next-session[parked]: PARKED pending a synthetic "
                          "ruling -- do not install anything", lines[0])
            self.assertIn("[next_session.py:v2 | 2026-07-22T00:00:00+00:00]",
                          lines[0])
            proj = sidecar["sections"]["surfaces"][0]["next_session"]
            self.assertEqual(proj["status"], "parked")
            self.assertIn("PARKED pending", proj["summary"])
            self.assertEqual(proj["as_of"], "2026-07-22T00:00:00+00:00")

    def test_absent_when_probe_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, _surface(
                "synth-a", NOW.isoformat(),
                next_session={"degraded": True,
                              "reason": "no root-level NEXT_SESSION.json"}))
            self.assertEqual(self._ns_lines(md), [])
            self.assertIsNone(sidecar["sections"]["surfaces"][0]["next_session"])
            # the degradation is still reported, just not as a projection
            self.assertTrue(any("degraded probe next_session" in w
                                for w in sidecar["warnings"]))

    def test_absent_when_surface_has_no_next_session_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            surf = _surface("synth-a", NOW.isoformat())
            del surf["next_session"]
            md, sidecar = self._build(tmp, surf)
            self.assertEqual(self._ns_lines(md), [])
            self.assertIsNone(sidecar["sections"]["surfaces"][0]["next_session"])

    def test_absent_when_record_carries_no_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, _surface(
                "synth-a", NOW.isoformat(),
                next_session={"source": "next_session.py:v2", "as_of": "",
                              "normalized": {"status": "", "summary": ""}}))
            self.assertEqual(self._ns_lines(md), [])
            self.assertIsNone(sidecar["sections"]["surfaces"][0]["next_session"])

    def test_long_summary_hard_truncated_to_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_summary = ("synthetic sentence number %02d with padding text\n"
                            % 0) + " ".join(
                "synthetic sentence number %02d with padding text" % i
                for i in range(1, 20))
            md, sidecar = self._build(tmp, _surface(
                "synth-a", NOW.isoformat(),
                next_session=_next_session(status="blocked",
                                           summary=long_summary)))
            proj = sidecar["sections"]["surfaces"][0]["next_session"]
            self.assertLessEqual(len(proj["summary"]),
                                 ab.NEXT_SESSION_SUMMARY_CHARS)
            self.assertTrue(proj["summary"].endswith("..."))
            self.assertNotIn("\n", proj["summary"])
            lines = self._ns_lines(md)
            self.assertEqual(len(lines), 1)
            self.assertIn("next-session[blocked]:", lines[0])

    def test_short_summary_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sidecar = self._build(tmp, _surface(
                "synth-a", NOW.isoformat(),
                next_session=_next_session(summary="a short synthetic recap")))
            proj = sidecar["sections"]["surfaces"][0]["next_session"]
            self.assertEqual(proj["summary"], "a short synthetic recap")

    def test_end_to_end_from_v2_tagged_fallback_shape_file(self):
        # generate-surface's probe shape, fed by the M1 parser: a v2-TAGGED
        # file carrying the free-form field set still reaches the pack.
        with tempfile.TemporaryDirectory() as tmp:
            rec = ab.ns.normalize_file(_write(
                os.path.join(tmp, "repo-a", "NEXT_SESSION.json"),
                {"schema": "next-session/v2", "session_date": "2026-07-22",
                 "project": "synth-a",
                 "state": "PARKED pending a synthetic ruling; nothing installed.",
                 "launch_one_liner": "Parked -- read the ruling first.",
                 "next_action_for_agent": "Do not build until the ruling lands."}))
            md, _ = self._build(tmp, _surface(
                "synth-a", NOW.isoformat(),
                next_session={"source": "next_session.py:%s"
                                        % rec["detected_schema"],
                              "as_of": NOW.isoformat(),
                              "normalized": rec["normalized"]}))
            lines = self._ns_lines(md)
            self.assertEqual(len(lines), 1)
            self.assertIn("next-session[parked]: PARKED pending a synthetic "
                          "ruling; nothing installed.", lines[0])


# --------------------------------------------------------------------------- #
# Feed d: atlas-v2 adjudication queues
# --------------------------------------------------------------------------- #
class AdjudicationFeed(unittest.TestCase):
    _FINDING = "### `~/somefile.md:1` (pr_state, STALE)\n\nbody\n"
    _BOXES_UNCHECKED = "- [ ] **APPLY**\n- [ ] **REJECT**\n- [ ] **DEFER**\n"
    _BOXES_ONE_CHECKED = "- [x] **APPLY**\n- [ ] **REJECT**\n- [ ] **DEFER**\n"

    def _queue_file(self, n_findings, checked=0):
        parts = ["# Adjudication queue: synthetic\n"]
        for i in range(n_findings):
            parts.append(self._FINDING)
            parts.append(self._BOXES_ONE_CHECKED if i < checked
                         else self._BOXES_UNCHECKED)
        return "".join(parts)

    def _build(self, tmp, adjudication_dir):
        cache = os.path.join(tmp, "surfaces")
        _write_surface(cache, _surface("synth-a", NOW.isoformat()))
        return ab.build_pack(NOW, _manifest(), _backlog(items=[]), cache, None,
                             adjudication_dir=adjudication_dir)

    def test_newest_file_per_prefix_wins_and_unrelated_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            pend = os.path.join(tmp, "pending")
            _write(os.path.join(pend, "AUTO-APPLY-2026-08-09.md"),
                   self._queue_file(9))
            _write(os.path.join(pend, "AUTO-APPLY-2026-08-10.md"),
                   self._queue_file(3))
            # the real pending dir also holds handoffs/memory files -- ignored
            _write(os.path.join(pend, "handoff-2026-07-03-synthetic.md"),
                   "### not a finding\n")
            _write(os.path.join(pend, "memory-project_synth.md"),
                   "- [x] not an actioned box\n")
            md, sidecar = self._build(tmp, pend)
            items = [i for i in sidecar["sections"]["decision_queue"]["items"]
                     if i["kind"] == "adjudication"]
            self.assertEqual(len(items), 1)
            self.assertIn("AUTO-APPLY-2026-08-10.md", items[0]["summary"])
            self.assertIn("3 finding(s)", items[0]["summary"])
            self.assertEqual(items[0]["as_of"], "2026-08-10")

    def test_finding_and_actioned_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            pend = os.path.join(tmp, "pending")
            _write(os.path.join(pend, "BACKLOG-2026-08-10.md"),
                   self._queue_file(5, checked=2))
            adj = ab.load_adjudication(pend)
            self.assertTrue(adj["present"])
            self.assertEqual(len(adj["queues"]), 1)
            q = adj["queues"][0]
            self.assertEqual(q["queue"], "review backlog")
            self.assertEqual(q["findings"], 5)
            self.assertEqual(q["actioned"], 2)
            self.assertEqual(q["as_of"], "2026-08-10")

    def test_feed_d_items_render_with_kind_source_and_queue_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            pend = os.path.join(tmp, "pending")
            _write(os.path.join(pend, "AUTO-APPLY-2026-08-10.md"),
                   self._queue_file(2))
            _write(os.path.join(pend, "BACKLOG-2026-08-10.md"),
                   self._queue_file(4, checked=1))
            _write(os.path.join(pend, "RIPPLE-2026-07-20.md"),
                   self._queue_file(1))
            md, sidecar = self._build(tmp, pend)
            items = [i for i in sidecar["sections"]["decision_queue"]["items"]
                     if i["kind"] == "adjudication"]
            self.assertEqual(len(items), 3)
            self.assertTrue(all(i["source"] == "adjudication:atlas-v2"
                                for i in items))
            q = md[md.index("## 3."):md.index("## 4.")]
            self.assertIn("atlas-v2 mechanical auto-apply queue: 2 finding(s) "
                          "awaiting checkbox pass (0 actioned) -- "
                          "AUTO-APPLY-2026-08-10.md", q)
            self.assertIn("atlas-v2 review backlog: 4 finding(s) awaiting "
                          "checkbox pass (1 actioned) -- BACKLOG-2026-08-10.md",
                          q)
            self.assertIn("atlas-v2 ripple review: 1 finding(s) awaiting "
                          "checkbox pass (0 actioned) -- RIPPLE-2026-07-20.md",
                          q)

    def test_unconfigured_dir_is_silently_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, None)
            items = sidecar["sections"]["decision_queue"]["items"]
            self.assertFalse(any(i["kind"] == "adjudication" for i in items))
            self.assertFalse(any("adjudication" in w
                                 for w in sidecar["warnings"]))

    def test_configured_but_missing_dir_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, os.path.join(tmp, "no-such-pending"))
            items = sidecar["sections"]["decision_queue"]["items"]
            self.assertFalse(any(i["kind"] == "adjudication" for i in items))
            self.assertTrue(any("adjudication" in w
                                for w in sidecar["warnings"]),
                            sidecar["warnings"])


# --------------------------------------------------------------------------- #
# Feed e: Atlas-v2 weekly DECAY review queue
# --------------------------------------------------------------------------- #
class DecayFeed(unittest.TestCase):
    _BLOCK = """### `/synthetic/memory/%s.md` (decay, project, 2d over horizon)

> A synthetic belief.
- **last_verified:** 2026-06-15 · **half-life:** 30d
%s
"""
    _CHECKS = {
        "reviewed": "- [x] **STILL TRUE** — bump last_verified to today\n"
                    "- [ ] **NEEDS UPDATE** — content is stale\n"
                    "- [ ] **DEFER**\n",
        "needs_update": "- [ ] **STILL TRUE** — bump last_verified to today\n"
                        "- [x] **NEEDS UPDATE** — content is stale\n"
                        "- [ ] **DEFER**\n",
        "deferred": "- [ ] **STILL TRUE** — bump last_verified to today\n"
                    "- [ ] **NEEDS UPDATE** — content is stale\n"
                    "- [x] **DEFER**\n",
        "pending": "- [ ] **STILL TRUE** — bump last_verified to today\n"
                   "- [ ] **NEEDS UPDATE** — content is stale\n"
                   "- [ ] **DEFER**\n",
    }
    _FIXTURE_DIR = os.path.join(_HERE, "fixtures", "decay")
    _PRODUCER_SHA256 = (
        "a169a8b6c0ed07ce5d0dbd5e555b5470644dc783fec743eda94bf1e229f1795f")

    def _queue_file(self, run_date="2026-07-20", states=None):
        states = states or ["reviewed", "needs_update", "deferred", "pending"]
        return ("# Decay review queue (Loop 3) — %s\n\n" % run_date
                + "\n".join(self._BLOCK % ("belief-%02d" % i,
                                              self._CHECKS[state])
                                for i, state in enumerate(states, 1)))

    def _build(self, tmp, decay_dir, adjudication_dir=None):
        cache = os.path.join(tmp, "surfaces")
        _write_surface(cache, _surface("synth-a", NOW.isoformat()))
        return ab.build_pack(
            NOW, _manifest(), _backlog(items=[]), cache, None,
            adjudication_dir=adjudication_dir, decay_dir=decay_dir)

    def test_fixture_metadata_pins_installed_producer(self):
        with open(os.path.join(self._FIXTURE_DIR, "producer-metadata.json"),
                  encoding="utf-8") as fh:
            metadata = json.load(fh)
        self.assertEqual(metadata["producer_sha256"], self._PRODUCER_SHA256)
        self.assertEqual(metadata["sha256"], self._PRODUCER_SHA256)
        self.assertEqual(metadata["schema"], "decay-fixture/v1")
        with tempfile.TemporaryDirectory() as tmp:
            _, sidecar = self._build(tmp, self._FIXTURE_DIR)
            self.assertEqual(
                sidecar["sections"]["decay"]["state_counts"], {
                    "total": 4, "reviewed": 1, "needs_update": 1,
                    "deferred": 1, "pending": 1, "unchecked": 1})

    def test_decay_only_projects_states_and_weekly_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            decay = os.path.join(tmp, "pending")
            _write(os.path.join(decay, "DECAY-2026-07-20.md"),
                   self._queue_file())
            md, sidecar = self._build(tmp, decay)
            section = sidecar["sections"]["decay"]
            self.assertTrue(section["present"])
            self.assertEqual(section["file"], "DECAY-2026-07-20.md")
            self.assertEqual(section["cadence"], "weekly (Monday)")
            self.assertEqual(section["freshness"], "fresh")
            self.assertEqual(section["state_counts"], {
                "total": 4, "reviewed": 1, "needs_update": 1,
                "deferred": 1, "pending": 1, "unchecked": 1})
            self.assertEqual(
                [i["state"] for i in section["items"]],
                ["reviewed", "needs_update", "deferred", "pending"])
            decay_items = [i for i in
                           sidecar["sections"]["decision_queue"]["items"]
                           if i["kind"] == "decay"]
            self.assertEqual(len(decay_items), 1)
            self.assertEqual(decay_items[0]["stale_after_seconds"],
                             ab.STALE_DECAY)
            self.assertIn("weekly DECAY review", md)
            self.assertIn("1 reviewed, 1 needs update, 1 deferred, 1 pending",
                          md)

    def test_mixed_feed_preserves_daily_queue_and_uses_weekly_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = os.path.join(tmp, "pending")
            _write(os.path.join(pending, "AUTO-APPLY-2026-07-20.md"),
                   AdjudicationFeed()._queue_file(1))
            _write(os.path.join(pending, "DECAY-2026-07-20.md"),
                   self._queue_file(states=["pending"]))
            md, sidecar = self._build(tmp, pending, adjudication_dir=pending)
            items = sidecar["sections"]["decision_queue"]["items"]
            self.assertTrue(any(i["kind"] == "adjudication" for i in items))
            self.assertTrue(any(i["kind"] == "decay" for i in items))
            queue = md[md.index("## 3."):md.index("## 4.")].splitlines()
            daily_line = [l for l in queue if "mechanical auto-apply" in l][0]
            decay_line = [l for l in queue if "weekly DECAY review" in l][0]
            # The same 3-day-old run is stale for the daily feed but fresh for
            # the weekly Monday feed.
            self.assertIn("STALE(", daily_line)
            self.assertNotIn("STALE(", decay_line)

    def test_numeric_suffix_is_ordered_as_a_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            decay = os.path.join(tmp, "pending")
            _write(os.path.join(decay, "DECAY-2026-07-20-2.md"),
                   self._queue_file(states=["deferred"]))
            _write(os.path.join(decay, "DECAY-2026-07-20-10.md"),
                   self._queue_file(states=["reviewed"]))
            _, sidecar = self._build(tmp, decay)
            section = sidecar["sections"]["decay"]
            self.assertEqual(section["file"], "DECAY-2026-07-20-10.md")
            self.assertEqual(section["suffix"], 10)
            self.assertEqual(section["state_counts"]["reviewed"], 1)

    def test_old_run_is_marked_stale_but_unresolved_state_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            decay = os.path.join(tmp, "pending")
            _write(os.path.join(decay, "DECAY-2026-07-10.md"),
                   self._queue_file(run_date="2026-07-10",
                                    states=["pending"]))
            md, sidecar = self._build(tmp, decay)
            section = sidecar["sections"]["decay"]
            self.assertEqual(section["freshness"], "stale")
            self.assertEqual(section["state_counts"]["pending"], 1)
            self.assertEqual(len([i for i in
                                  sidecar["sections"]["decision_queue"]["items"]
                                  if i["kind"] == "decay"]), 1)
            self.assertIn("STALE(", [l for l in md.splitlines()
                                      if l.startswith("- ")
                                      and "weekly DECAY review" in l][0])

    def test_configured_missing_or_empty_feed_warns_without_claiming_no_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing")
            md, sidecar = self._build(tmp, missing)
            self.assertIn("decay", sidecar["sections"])
            self.assertEqual(sidecar["sections"]["decay"]["state_counts"], {})
            self.assertTrue(any("DECAY feed unavailable" in w
                                and "does not exist" in w
                                for w in sidecar["warnings"]))
            self.assertIn("weekly DECAY unavailable", md)

            empty = os.path.join(tmp, "empty")
            os.makedirs(empty)
            empty_md, empty_sidecar = self._build(tmp, empty)
            self.assertTrue(any("no work cannot be inferred" in w
                                for w in empty_sidecar["warnings"]))
            self.assertIn("no run recorded", empty_md)
            self.assertIn("no work cannot be inferred", empty_md)

    def test_unreadable_selected_file_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            decay = os.path.join(tmp, "pending")
            path = _write(os.path.join(decay, "DECAY-2026-07-20.md"),
                          self._queue_file(states=["pending"]))
            with mock.patch.object(ab, "open",
                                   side_effect=OSError("permission denied")):
                loaded = ab.load_decay(decay)
            self.assertFalse(loaded["present"])
            self.assertIn("unreadable", loaded["reason"])
            self.assertIn(os.path.basename(path), loaded["reason"])

    def test_unconfigured_feed_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, None)
            self.assertNotIn("decay", sidecar["sections"])
            self.assertFalse(any("DECAY" in w for w in sidecar["warnings"]))
            self.assertNotIn("weekly DECAY review", md)


# --------------------------------------------------------------------------- #
# Fix 4: scan-section absence copy (two variants)
# --------------------------------------------------------------------------- #
class ScanAbsenceCopy(unittest.TestCase):
    def test_no_dir_configured_clean_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface("synth-a", NOW.isoformat()))
            md, _ = ab.build_pack(NOW, _manifest(), _backlog(), cache, None)
            scan_sec = md[md.index("## 5."):]
            self.assertIn("scan consumption dir not configured", scan_sec)
            self.assertIn("pass --scan-consumption-dir", scan_sec)
            self.assertIn("[config |", scan_sec)
            # the double-rendered placeholder copy must be gone
            self.assertNotIn("no scan artifacts found at (no --scan-consumption-dir)",
                             scan_sec)
            self.assertNotIn("no scan artifacts found", scan_sec)

    def test_configured_empty_dir_keeps_no_artifacts_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface("synth-a", NOW.isoformat()))
            empty = os.path.join(tmp, "empty-scan")
            os.makedirs(empty)
            md, sidecar = ab.build_pack(NOW, _manifest(), _backlog(), cache, empty)
            scan_sec = md[md.index("## 5."):]
            self.assertIn("no scan artifacts found at %s" % empty, scan_sec)
            self.assertNotIn("scan consumption dir not configured", scan_sec)
            self.assertFalse(sidecar["sections"]["scan"]["present"])




# --------------------------------------------------------------------------- #
# Section 4: plans (plans-snapshot.json written by ledger.py export)
# --------------------------------------------------------------------------- #
def _plans_snapshot(generated=None, lanes=None, unregistered=None):
    """A synthetic plans snapshot -- shape only, no real lane or plan names."""
    return {
        "generated": generated or NOW.isoformat(),
        "idle_threshold_days": 7,
        "lanes": lanes if lanes is not None else [
            {"name": "synth-lane",
             "step": "First sentence. Second sentence.",
             "waiting_on_anthony": ["Merge the synthetic PR. Extra detail."],
             "blocked": [],
             "updated": "2026-07-23",
             "idle_days": 0,
             "health": "waiting",
             "health_phrase": "waiting on Anthony 1 day",
             "finish_line": [{"text": "item one", "met": True}],
             "finish_progress": "1/2",
             "stations": {}},
        ],
        "unregistered_plan_files": unregistered if unregistered is not None else [
            {"path": "/nonexistent/NEXT_SESSION-orphan.json",
             "mtime": "2026-07-01T00:00:00+00:00"},
        ],
    }


class PlansSection(unittest.TestCase):
    def _build(self, tmp, snapshot=None, path=None, configured=True):
        cache = os.path.join(tmp, "surfaces")
        _write_surface(cache, _surface("synth-a", NOW.isoformat()))
        if not configured:
            return ab.build_pack(NOW, _manifest(), _backlog(), cache, None)
        snap_path = path or os.path.join(tmp, "plans-snapshot.json")
        if snapshot is not None:
            _write(snap_path, snapshot)
        return ab.build_pack(NOW, _manifest(), _backlog(), cache, None,
                             plans_snapshot_path=snap_path)

    def test_lane_line_carries_step_progress_and_what_waits_on_anthony(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, _plans_snapshot())
            self.assertIn("## 4. Plans", md)
            self.assertIn(
                "synth-lane: waiting on Anthony 1 day -- First sentence.", md)
            # first sentence ONLY -- the pack is a pointer, not the plan
            self.assertNotIn("Second sentence.", md)
            self.assertIn("finish line 1/2", md)
            self.assertIn("waiting on Anthony: Merge the synthetic PR.", md)
            self.assertIn("1 plan file(s) not registered", md)
            self.assertIn("NEXT_SESSION-orphan.json", md)
            self.assertIn("[plans-snapshot.json | ", md)
            self.assertTrue(sidecar["sections"]["plans"]["present"])

    def test_missing_snapshot_degrades_to_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope", "plans-snapshot.json")
            md, sidecar = self._build(tmp, None, path=missing)
            self.assertIn("no plans snapshot at", md)
            self.assertTrue(any("plans snapshot unavailable" in w
                                for w in sidecar["warnings"]),
                            sidecar["warnings"])

    def test_unconfigured_snapshot_says_so_without_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, configured=False)
            self.assertIn("plans snapshot not configured", md)
            self.assertFalse([w for w in sidecar["warnings"] if "plans" in w],
                             sidecar["warnings"])

    def test_stale_snapshot_warns(self):
        old = (NOW - datetime.timedelta(hours=40)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            _, sidecar = self._build(tmp, _plans_snapshot(generated=old))
            self.assertTrue(any("plans snapshot STALE" in w
                                for w in sidecar["warnings"]),
                            sidecar["warnings"])


# --------------------------------------------------------------------------- #
# Section 0: defects (defect-status/v1 written by verify-defects.py)
# --------------------------------------------------------------------------- #
def _defect_item(item_id, severity="P1", owner="session", status="open",
                 days=3, symptom=None):
    return {"id": item_id, "severity": severity, "owner": owner,
            "fix_scope": "local", "size": "S", "system": "synthetic system",
            "symptom": symptom or "a synthetic thing is broken",
            "fix_hint": "do the synthetic thing", "status": status,
            "exit": 1 if status == "open" else 0,
            "last_verified": "2026-07-23", "open_since": "2026-07-20",
            "days_open": days if status == "open" else None,
            "fixed_at": None, "duration_ms": 12, "stderr_tail": ""}


# The summary line from the spec, verbatim -- the ONE line every session sees.
EXPECTED_SUMMARY = ("DEFECTS -- P0: 3 (oldest 6d) · P1: 9 · P2: 7 · "
                    "fixed since yesterday: 1 · yours today: 4 · "
                    "no real check yet: 4 · accepted: 1")

DEFAULT_COUNTS = {
    "P0": {"open": 3, "oldest_days": 6},
    "P1": {"open": 9, "oldest_days": 12},
    "P2": {"open": 7, "oldest_days": 30},
    "fixed_since_last": 1, "provisional": 4, "deferred": 0, "error": 0,
    "accepted": 1,
    "open_by_owner": {"anthony": 4, "codex": 5, "session": 10},
}


def _defect_status(generated_at=None, counts=None, yours_today=None,
                   items=None):
    return {
        "schema": "defect-status/v1",
        "generated_at": generated_at or NOW.isoformat(timespec="seconds"),
        "register_updated": "2026-07-23",
        "counts": copy.deepcopy(counts if counts is not None else DEFAULT_COUNTS),
        "yours_today": ["SYN-A1"] if yours_today is None else list(yours_today),
        "nightly_eligible": [],
        "items": items if items is not None else [
            _defect_item("SYN-A1", severity="P0", owner="anthony", days=6),
            _defect_item("SYN-P1a", days=12),
            _defect_item("SYN-P1b", days=2),
        ],
    }


class DefectsSection(unittest.TestCase):
    def _build(self, tmp, status=None, path=None):
        cache = os.path.join(tmp, "surfaces")
        _write_surface(cache, _surface("synth-a", NOW.isoformat()))
        status_path = path or os.path.join(tmp, "defects-status.json")
        if status is not None:
            _write(status_path, status)
        return ab.build_pack(NOW, _manifest(), _backlog(), cache, None,
                             defects_status_path=status_path)

    def test_section_zero_is_rendered_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, _ = self._build(tmp, _defect_status())
            i0 = md.index("## 0. Defects (single register; verify exit 0 = fixed)")
            self.assertLess(i0, md.index("## 1. Topology manifest"))
            # ... and after the head block, never before the advisory banner.
            self.assertLess(md.index("ADVISORY STATE, NOT LAW"), i0)

    def test_first_content_line_is_the_summary_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, _ = self._build(tmp, _defect_status())
            section = md[md.index("## 0."):md.index("## 1.")]
            first = section.splitlines()[1]
            self.assertEqual(first, EXPECTED_SUMMARY)

    def test_yours_today_leads_then_oldest_open_p1_fills(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, _defect_status())
            section = md[md.index("## 0."):md.index("## 1.")]
            bullets = [ln for ln in section.splitlines() if ln.startswith("- ")]
            self.assertEqual([b.split(" ")[1] for b in bullets],
                             ["SYN-A1", "SYN-P1a", "SYN-P1b"])
            self.assertEqual(sidecar["sections"]["defects"]["rendered_ids"],
                             ["SYN-A1", "SYN-P1a", "SYN-P1b"])

    def test_bullets_are_compressed_to_120_chars(self):
        long_symptom = "x" * 400
        status = _defect_status(items=[
            _defect_item("SYN-A1", severity="P0", owner="anthony", days=6,
                         symptom=long_symptom)])
        with tempfile.TemporaryDirectory() as tmp:
            md, _ = self._build(tmp, status)
            section = md[md.index("## 0."):md.index("## 1.")]
            bullets = [ln for ln in section.splitlines() if ln.startswith("- ")]
            self.assertEqual(len(bullets), 1)
            self.assertLessEqual(len(bullets[0]), ab.DEFECT_BULLET_CHARS)
            self.assertTrue(bullets[0].endswith("..."))

    def test_missing_status_file_says_so_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, None)
            section = md[md.index("## 0."):md.index("## 1.")]
            self.assertIn("no defect status yet (run tools/verify-defects.py)",
                          section)
            self.assertTrue(any("defect status unavailable" in w
                                for w in sidecar["warnings"]),
                            sidecar["warnings"])
            self.assertEqual(sidecar["sections"]["defects"]["counts"], {})
            self.assertLess(md.index("## WARNINGS"), md.index("## OPEN-ITEMS"))
            self.assertLess(md.index("## OPEN-ITEMS"), md.index("## 0. Defects"))
            self.assertIn("- defect status unavailable:",
                          md[md.index("## WARNINGS"):md.index("## OPEN-ITEMS")])

    def test_unparseable_status_file_degrades_the_same_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "defects-status.json")
            _write(path, "{not json")
            md, sidecar = self._build(tmp, None, path=path)
            self.assertIn("no defect status yet", md)
            self.assertTrue(any("defect status unavailable" in w
                                for w in sidecar["warnings"]),
                            sidecar["warnings"])

    def test_stale_status_carries_the_stale_prefix(self):
        old = (NOW - datetime.timedelta(hours=40)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as tmp:
            md, _ = self._build(tmp, _defect_status(generated_at=old))
            section = md[md.index("## 0."):md.index("## 1.")]
            first = section.splitlines()[1]
            self.assertTrue(first.startswith("STALE(1d) "), first)
            self.assertIn(EXPECTED_SUMMARY, first)

    def test_fresh_status_has_no_stale_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            md, _ = self._build(tmp, _defect_status())
            section = md[md.index("## 0."):md.index("## 1.")]
            self.assertNotIn("STALE(", section)

    def test_status_becomes_stale_only_after_thirty_six_hours(self):
        for seconds_old, stale in ((36 * 3600, False), (36 * 3600 + 1, True)):
            with self.subTest(seconds_old=seconds_old):
                generated = (NOW - datetime.timedelta(seconds=seconds_old)).isoformat(
                    timespec="seconds")
                with tempfile.TemporaryDirectory() as tmp:
                    md, _ = self._build(
                        tmp, _defect_status(generated_at=generated))
                    section = md[md.index("## 0."):md.index("## 1.")]
                    self.assertEqual("STALE(" in section, stale)

    def test_section_never_exceeds_twelve_lines(self):
        items = [_defect_item("SYN-A%d" % i, severity="P0", owner="anthony",
                              days=i) for i in range(20)]
        status = _defect_status(items=items,
                                yours_today=[i["id"] for i in items])
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, status)
            section = md[md.index("## 0."):md.index("## 1.")].rstrip("\n")
            self.assertLessEqual(len(section.splitlines()),
                                 ab.DEFECTS_SECTION_MAX_LINES)
            self.assertEqual(len(sidecar["sections"]["defects"]["rendered_ids"]),
                             ab.DEFECTS_MAX_BULLETS)

    def test_sidecar_records_the_defect_section_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sidecar = self._build(tmp, _defect_status())
            self.assertEqual(list(sidecar["sections"].keys()),
                             ["defects", "topology", "surfaces",
                              "decision_queue", "plans", "scan"])
            d = sidecar["sections"]["defects"]
            self.assertEqual(sorted(d.keys()),
                             ["counts", "generated_at", "rendered_ids",
                              "status_path", "yours_today"])
            self.assertEqual(d["counts"], DEFAULT_COUNTS)
            self.assertEqual(d["yours_today"], ["SYN-A1"])
            self.assertTrue(d["status_path"].endswith("defects-status.json"))

    def test_unconfigured_feed_renders_no_section_at_all(self):
        """No --defects-status path -> silently absent (adjudication precedent)."""
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "surfaces")
            _write_surface(cache, _surface("synth-a", NOW.isoformat()))
            md, sidecar = ab.build_pack(NOW, _manifest(), _backlog(), cache, None)
            self.assertNotIn("## 0. Defects", md)
            self.assertNotIn("defects", sidecar["sections"])
            self.assertFalse(any("defect" in w for w in sidecar["warnings"]))

    def test_wrong_schema_is_not_trusted(self):
        bad = _defect_status()
        bad["schema"] = "defect-status/v2"
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, bad)
            self.assertIn("no defect status yet", md)
            self.assertTrue(any("defect status unavailable" in w
                                for w in sidecar["warnings"]))

    def test_empty_or_malformed_counts_degrade_instead_of_rendering_zero(self):
        for counts in ({}, {"P0": 7}):
            with self.subTest(counts=counts):
                bad = _defect_status(counts=counts)
                with tempfile.TemporaryDirectory() as tmp:
                    md, sidecar = self._build(tmp, bad)
                    section = md[md.index("## 0."):md.index("## 1.")]
                    self.assertIn("no defect status yet", section)
                    self.assertNotIn("DEFECTS -- P0: 0", section)
                    self.assertTrue(any("invalid counts block" in warning
                                        for warning in sidecar["warnings"]))

    def test_malformed_item_record_degrades_instead_of_crashing(self):
        """A bad id or days_open used to raise TypeError out of defect_bullets."""
        cases = {
            "list id": {"id": ["SYN-A1"]},
            "dict id": {"id": {"a": 1}},
            "empty id": {"id": ""},
            "int id": _defect_item(7),
            "string days_open": _defect_item("SYN-A1", days="12"),
            "dict days_open": _defect_item("SYN-A1", days={"a": 1}),
            "negative days_open": _defect_item("SYN-A1", days=-1),
            "bool days_open": _defect_item("SYN-A1", days=True),
            "item is not a dict": "SYN-A1",
        }
        for label, item in cases.items():
            with self.subTest(case=label):
                bad = _defect_status(items=[item], yours_today=[])
                with tempfile.TemporaryDirectory() as tmp:
                    md, sidecar = self._build(tmp, bad)
                    section = md[md.index("## 0."):md.index("## 1.")]
                    self.assertIn("no defect status yet", section)
                    self.assertTrue(any("malformed item record" in warning
                                        for warning in sidecar["warnings"]),
                                    sidecar["warnings"])

    def test_non_list_items_block_degrades(self):
        bad = _defect_status(items=[])
        bad["items"] = {"SYN-A1": {"id": "SYN-A1"}}
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, bad)
            self.assertIn("no defect status yet", md)
            self.assertTrue(any("non-list items block" in warning
                                for warning in sidecar["warnings"]))

    def test_a_fixed_item_with_no_days_open_is_still_valid(self):
        """days_open is None on a fixed record -- that must not be malformed."""
        status = _defect_status(
            items=[_defect_item("SYN-A1", status="fixed"),
                   _defect_item("SYN-P1a", days=4)],
            yours_today=["SYN-A1"])
        with tempfile.TemporaryDirectory() as tmp:
            md, sidecar = self._build(tmp, status)
            section = md[md.index("## 0."):md.index("## 1.")]
            self.assertNotIn("no defect status yet", section)
            self.assertEqual(sidecar["sections"]["defects"]["rendered_ids"],
                             ["SYN-A1", "SYN-P1a"])


if __name__ == "__main__":
    unittest.main()
