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


def _surface(env, generated_at, decisions=None, next_lanes=None, cold=None):
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
        "next_session": {"degraded": True, "reason": "none"},
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

    def test_unknown_as_of_is_visibly_stale(self):
        self.assertEqual(ab.stale_prefix(NOW, "", ab.STALE_DECISIONS), "STALE(?) ")


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
            i4 = md.index("## 4. Scan / priorities feed")
            self.assertLess(i1, i2)
            self.assertLess(i2, i3)
            self.assertLess(i3, i4)

    def test_sidecar_section_key_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, sidecar = self._build(tmp)
            self.assertEqual(list(sidecar["sections"].keys()),
                             ["topology", "surfaces", "decision_queue", "scan"])
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
            self.assertIn("## 4. Scan / priorities feed", md)
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


if __name__ == "__main__":
    unittest.main()
