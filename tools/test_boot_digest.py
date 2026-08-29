#!/usr/bin/env python3
"""Tests for boot-digest.py + its hook/installer shell -- stdlib unittest.

All fixtures are SYNTHETIC boot-pack/v1 dicts written into a tempdir; no real
repo content, and the real ~/.claude/settings.json is NEVER read or written (the
installer test runs against a throwaway HOME). The digest filename has a hyphen,
so it is loaded via importlib. Run with:

    python3 -m unittest test_boot_digest -v
"""

import datetime
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_WRAPPER = os.path.join(_HERE, "morning-pack.sh")
_HOOK = os.path.join(_HERE, "session-digest-hook.sh")
_INSTALLER = os.path.join(_HERE, "install-digest-hook.sh")


def _load_digest():
    spec = importlib.util.spec_from_file_location(
        "boot_digest", os.path.join(_HERE, "boot-digest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bd = _load_digest()

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
GENERATED = (NOW - datetime.timedelta(hours=3)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Synthetic input builders
# --------------------------------------------------------------------------- #
def _surface(env, behind=0):
    return {"environment": env, "degraded": False,
            "behind_origin_main": behind, "next_lanes": []}


def _pack(generated_at=GENERATED, warnings=None, open_items=None,
          surfaces=None, queue_items=None, schema=bd.PACK_SCHEMA):
    return {
        "schema": schema,
        "generated_at": generated_at,
        "token_estimate": 2900,
        "hard_cap_tokens": 50000,
        "advisory": "ADVISORY STATE, NOT LAW -- synthetic fixture.",
        "warnings": warnings if warnings is not None else [],
        "open_items": open_items if open_items is not None else [],
        "truncation": [],
        "sections": {
            "topology": {"provenance": "manual", "as_of": "2026-07-23",
                         "entries": []},
            "surfaces": surfaces if surfaces is not None else [
                _surface("synth-healthy", behind=0)],
            "decision_queue": {"projection": True, "absorbs_ratification": False,
                               "items": queue_items or []},
            "scan": {},
        },
    }


def _write_pack(tmp, pack):
    path = os.path.join(tmp, "boot-pack.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pack, fh)
    return path


class Header(unittest.TestCase):
    def test_title_and_pack_timestamp(self):
        md = bd.build_digest(NOW, _pack())
        self.assertTrue(md.startswith(bd.TITLE + "\n"), md[:120])
        self.assertIn("advisory, not law", md)
        self.assertIn("Pack generated: %s (3h ago)" % GENERATED, md)

    def test_full_pack_pointer_is_the_last_line(self):
        md = bd.build_digest(NOW, _pack())
        self.assertEqual(md.rstrip("\n").splitlines()[-1], bd.PACK_POINTER)

    def test_pack_pointer_is_the_absolute_repo_path(self):
        """The digest is read in sessions with arbitrary cwd -- no relative path."""
        self.assertTrue(os.path.isabs(bd.PACK_MD), bd.PACK_MD)
        self.assertEqual(bd.PACK_MD,
                         os.path.join(os.path.dirname(_HERE), "state",
                                      "BOOT-PACK.md"))
        md = bd.build_digest(NOW, _pack())
        self.assertIn("Full pack: %s" % bd.PACK_MD, md)
        self.assertNotIn("Full pack: state/BOOT-PACK.md", md)

    def test_unparseable_generated_at_degrades_to_age_unknown(self):
        md = bd.build_digest(NOW, _pack(generated_at="whenever"))
        self.assertIn("age unknown", md)
        self.assertNotIn("STALE", md)


class Staleness(unittest.TestCase):
    def test_fresh_pack_has_no_stale_flag(self):
        md = bd.build_digest(NOW, _pack())
        self.assertNotIn("STALE", md)

    def test_pack_older_than_36h_is_flagged(self):
        old = (NOW - datetime.timedelta(hours=40)).isoformat(timespec="seconds")
        md = bd.build_digest(NOW, _pack(generated_at=old))
        self.assertIn("(40h ago) STALE (>36h)", md)

    def test_boundary_is_exclusive_at_36h(self):
        at_36 = (NOW - datetime.timedelta(hours=36)).isoformat(timespec="seconds")
        self.assertNotIn("STALE", bd.build_digest(NOW, _pack(generated_at=at_36)))
        past_36 = (NOW - datetime.timedelta(hours=36, minutes=1)).isoformat(
            timespec="seconds")
        self.assertIn("STALE", bd.build_digest(NOW, _pack(generated_at=past_36)))


class WarningsAndOpenItems(unittest.TestCase):
    def test_warning_count_and_one_compressed_line_each(self):
        md = bd.build_digest(NOW, _pack(warnings=[
            "surface for synth-a: degraded probe\n   next_session (no file)",
            "surface for synth-b: degraded probe identity"]))
        self.assertIn("WARNINGS (2):", md)
        self.assertIn("- surface for synth-a: degraded probe next_session (no file)", md)
        self.assertIn("- surface for synth-b: degraded probe identity", md)

    def test_open_items_are_first_sentence_only(self):
        md = bd.build_digest(NOW, _pack(open_items=[
            "Wire the synthetic feed. Everything after this sentence is "
            "background nobody needs at boot."]))
        self.assertIn("OPEN ITEMS (1):", md)
        self.assertIn("- Wire the synthetic feed.", md)
        self.assertNotIn("background nobody needs", md)

    def test_empty_sections_are_omitted_entirely(self):
        md = bd.build_digest(NOW, _pack())
        self.assertNotIn("WARNINGS", md)
        self.assertNotIn("OPEN ITEMS", md)
        self.assertNotIn("ATTENTION", md)


class AttentionLines(unittest.TestCase):
    def test_healthy_repo_is_skipped_and_behind_repo_is_included(self):
        md = bd.build_digest(NOW, _pack(surfaces=[
            _surface("synth-healthy", behind=0),
            _surface("synth-behind", behind=8)]))
        self.assertIn("ATTENTION (1):", md)
        self.assertIn("- synth-behind: 8 behind origin default", md)
        self.assertNotIn("synth-healthy", md)

    def test_queue_items_pull_a_healthy_repo_in(self):
        md = bd.build_digest(NOW, _pack(
            surfaces=[_surface("synth-healthy", behind=0)],
            queue_items=[{"summary": "a", "source": "next-session:synth-healthy"},
                         {"summary": "b", "source": "surface:synth-healthy"}]))
        self.assertIn("- synth-healthy: 2 decision-queue item(s)", md)

    def test_behind_and_queue_render_on_one_line(self):
        md = bd.build_digest(NOW, _pack(
            surfaces=[_surface("synth-both", behind=1)],
            queue_items=[{"summary": "a", "source": "next-session:synth-both"}]))
        self.assertIn("- synth-both: 1 behind origin default, 1 decision-queue item(s)",
                      md)

    def test_degraded_behind_marker_is_not_a_behind_count(self):
        degraded = _surface("synth-degraded")
        degraded["behind_origin_main"] = {
            "degraded": True, "reason": "no refs/remotes/origin/main ref"}
        md = bd.build_digest(NOW, _pack(surfaces=[degraded]))
        self.assertNotIn("ATTENTION", md)

    def test_queue_item_for_a_repo_with_no_surface_is_kept(self):
        md = bd.build_digest(NOW, _pack(
            surfaces=[_surface("synth-healthy", behind=0)],
            queue_items=[{"summary": "a", "source": "next-session:synth-offpack"}]))
        self.assertIn("- synth-offpack: 1 decision-queue item(s)", md)

    def test_adjudication_feed_attributes_to_atlas_v2(self):
        md = bd.build_digest(NOW, _pack(
            queue_items=[{"summary": "atlas-v2 review backlog: 4 finding(s)",
                          "source": "adjudication:atlas-v2"},
                         {"summary": "atlas-v2 auto-apply: 2 finding(s)",
                          "source": "adjudication:atlas-v2"}]))
        self.assertIn("- atlas-v2: 2 decision-queue item(s)", md)

    def test_unattributable_queue_source_makes_no_repo_line(self):
        md = bd.build_digest(NOW, _pack(
            queue_items=[{"summary": "a", "source": "ratification-backlog.json"}]))
        self.assertNotIn("ATTENTION", md)


class TokenCap(unittest.TestCase):
    def _fat_pack(self):
        return _pack(
            warnings=["surface for synth-%02d: degraded probe next_session "
                      "(no root-level NEXT_SESSION.json)" % i for i in range(40)],
            open_items=["Open item %d. Trailing background." % i for i in range(20)],
            surfaces=[_surface("synth-%02d" % i, behind=i + 1) for i in range(30)])

    def test_cap_respected_with_truncation_marker(self):
        md = bd.build_digest(NOW, self._fat_pack())
        self.assertLessEqual(bd.est_tokens(md), bd.HARD_CAP_TOKENS)
        self.assertIn("[truncated]", md)

    def test_truncation_markers_name_the_absolute_pack(self):
        md = bd.build_digest(NOW, self._fat_pack())
        marked = [ln for ln in md.splitlines() if "[truncated]" in ln]
        self.assertTrue(marked, md)
        for line in marked:
            self.assertIn(bd.PACK_MD, line)
            self.assertNotIn("see state/BOOT-PACK.md", line)

    def test_header_and_pointer_survive_truncation(self):
        md = bd.build_digest(NOW, self._fat_pack())
        self.assertTrue(md.startswith(bd.TITLE + "\n"))
        self.assertIn("Pack generated:", md)
        self.assertIn(bd.PACK_POINTER, md)

    def test_counts_stay_honest_after_shedding(self):
        md = bd.build_digest(NOW, self._fat_pack())
        self.assertIn("WARNINGS (40):", md)
        self.assertIn("OPEN ITEMS (20):", md)
        self.assertIn("ATTENTION (30):", md)

    def test_attention_is_shed_last(self):
        """Open items go first, warnings next, attention only as a last resort.

        Use a stable synthetic pointer so this priority test does not change
        behavior merely because the repository is cloned into a longer path.
        Absolute pointer behavior is covered independently above.
        """
        pack_md = "/synthetic/BOOT-PACK.md"
        with unittest.mock.patch.object(bd, "PACK_MD", pack_md), \
                unittest.mock.patch.object(
                    bd, "PACK_POINTER", "Full pack: %s" % pack_md):
            md = bd.build_digest(NOW, self._fat_pack(), cap_tokens=120)
        self.assertIn("- synth-00: 1 behind origin default", md)

    def test_a_normal_pack_stays_a_few_hundred_tokens(self):
        md = bd.build_digest(NOW, _pack(
            warnings=["surface for synth-a: degraded probe next_session"],
            open_items=["Wire the synthetic feed. Background."],
            surfaces=[_surface("synth-behind", behind=3)]))
        self.assertLess(bd.est_tokens(md), 200)


class DailyBrief(unittest.TestCase):
    def test_first_non_empty_non_heading_line_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = os.path.join(tmp, "LATEST.md")
            with open(latest, "w", encoding="utf-8") as fh:
                fh.write("# Daily scan 2026-07-23\n\n## Summary\n\n"
                         "three repos drifted overnight\nsecond line\n")
            md = bd.build_digest(NOW, _pack(),
                                 daily_brief=bd.load_daily_brief(latest),
                                 daily_brief_path=latest)
            self.assertIn("Daily brief: three repos drifted overnight", md)
            self.assertIn(latest, md)
            self.assertNotIn("second line", md)

    def test_brief_pointer_is_absolute(self):
        """Same contract as the pack pointer: arbitrary-cwd sessions need it."""
        with tempfile.TemporaryDirectory() as tmp:
            latest = os.path.join(tmp, "LATEST.md")
            with open(latest, "w", encoding="utf-8") as fh:
                fh.write("headline\n")
            rel = os.path.relpath(latest)
            md = bd.build_digest(NOW, _pack(),
                                 daily_brief=bd.load_daily_brief(rel),
                                 daily_brief_path=rel)
            self.assertIn("(%s)" % os.path.abspath(rel), md)
            self.assertNotIn("(%s)" % rel, md)

    def test_absent_brief_adds_no_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "LATEST.md")
            self.assertIsNone(bd.load_daily_brief(missing))
            md = bd.build_digest(NOW, _pack(),
                                 daily_brief=bd.load_daily_brief(missing),
                                 daily_brief_path=missing)
            self.assertNotIn("Daily brief:", md)


class ImprintSection(unittest.TestCase):
    """IMPRINT summary: counts + pointer, outside the token cap (audit P1-1).

    Every render passes an explicit ``db_path``: the live imprint store exists
    on Anthony's box and not in CI, and a test that reads whichever it lands on
    is a test that reports the machine, not the code.
    """

    NO_DB = "/nonexistent/imprint.db"

    EXPORT = (
        "# Imprint\n\n"
        "## Call\n\n"
        "- **urn:imprint:call:aaa** [captured · valid 2026-07-20T03:50:00Z..current] correct\n"
        "- **urn:imprint:call:bbb** [captured · valid 2026-08-01T00:00:00Z..current] prefer\n\n"
        "## Verdict\n\n"
        "- **urn:imprint:verdict:ccc** [captured · valid 2026-08-05T10:00:00Z..current] body follows\n"
        "## What you do\n"
        "- **urn:imprint:fake:ddd** this bullet sits under a record-body heading\n"
    )

    def _summary(self, tmp, text=None, age_hours=0):
        path = os.path.join(tmp, "imprint-store.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.EXPORT if text is None else text)
        # NOW is a fixed synthetic clock; age the export relative to IT so the
        # freshness arithmetic never depends on the real wall clock.
        stamp = NOW.timestamp() - age_hours * 3600
        os.utime(path, (stamp, stamp))
        return bd.load_imprint(path)

    def _lines(self, summary, db_path=None):
        return bd.imprint_lines(summary, now=NOW, db_path=db_path or self.NO_DB)

    def test_counts_newest_and_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self._summary(tmp)
            self.assertEqual(s["records"], 3)
            self.assertEqual(s["newest"], "2026-08-05")
            md = bd.build_digest(NOW, _pack(), imprint=self._lines(s))
            self.assertIn("IMPRINT (captured judgment", md)
            self.assertIn("~3 records", md)
            self.assertIn("newest capture 2026-08-05", md)
            self.assertIn(s["path"], md)
            self.assertNotIn("WARNING", md)

    def test_bare_urn_mentions_in_record_bodies_do_not_count(self):
        """Only the entry idiom (urn immediately followed by the bracketed
        metadata run) counts; a bare urn bullet inside a captured record body
        does not."""
        with tempfile.TemporaryDirectory() as tmp:
            s = self._summary(tmp)
            self.assertEqual(s["records"], 3)  # the fake:ddd bullet is excluded

    def test_missing_export_omits_the_section(self):
        self.assertIsNone(bd.load_imprint("/nonexistent/imprint-store.md"))
        md = bd.build_digest(NOW, _pack(), imprint=self._lines(None))
        self.assertNotIn("IMPRINT", md)

    def test_present_but_unparseable_export_warns_instead_of_vanishing(self):
        """Audit 2026-08-08: zero regex matches silently dropped the section,
        so a line-format drift in the export read as "no imprint configured"."""
        with tempfile.TemporaryDirectory() as tmp:
            s = self._summary(tmp, text="# Imprint\n\n* urn:imprint:call:aaa\n")
            self.assertIsNotNone(s)
            self.assertEqual(s["records"], 0)
            md = bd.build_digest(NOW, _pack(), imprint=self._lines(s))
            self.assertIn("IMPRINT (captured judgment", md)
            self.assertIn("WARNING: imprint export present but unparseable "
                          "(line-format drift?)", md)
            self.assertNotIn("records;", md)

    def test_export_older_than_26h_is_flagged_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = bd.build_digest(NOW, _pack(),
                                 imprint=self._lines(self._summary(tmp, age_hours=30)))
            self.assertIn("WARNING: imprint export STALE: 30h old -- 05:45 "
                          "export may have failed", md)

    def test_export_at_the_26h_boundary_is_not_flagged(self):
        """Exclusive, like the pack's own 36h boundary: 26h clean, past it stale."""
        with tempfile.TemporaryDirectory() as tmp:
            at = self._lines(self._summary(tmp, age_hours=26))
            self.assertNotIn("STALE", "\n".join(at))
            past = self._lines(self._summary(tmp, age_hours=26.5))
            self.assertIn("STALE", "\n".join(past))

    def test_live_store_lag_line_when_the_db_is_readable(self):
        """Export-vs-store lag has to be visible, not inferred."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "imprint.db")
            with open(db, "w", encoding="utf-8") as fh:
                fh.write("sqlite stand-in")
            stamp = NOW.timestamp() - 3 * 3600
            os.utime(db, (stamp, stamp))
            lines = self._lines(self._summary(tmp), db_path=db)
            self.assertIn("- live store last written 3h ago", lines)

    def test_absent_live_store_drops_only_that_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = self._lines(self._summary(tmp))
            self.assertNotIn("live store", "\n".join(lines))
            self.assertIn("- ~3 records; newest capture 2026-08-05", lines)

    def test_relative_age_rendering(self):
        self.assertEqual(bd._ago(90), "1m ago")
        self.assertEqual(bd._ago(3 * 3600 + 60), "3h ago")
        self.assertEqual(bd._ago(50 * 3600), "2d ago")
        self.assertEqual(bd._ago(-5), "0m ago")  # clock skew is not negative age

    def test_exempt_from_token_cap_and_pointer_stays_last(self):
        """A pack big enough to trigger shedding must shed the same lines with
        or without the imprint section, and the pack pointer stays last."""
        big = _pack(warnings=["w%d %s" % (i, "x" * 100) for i in range(40)])
        with tempfile.TemporaryDirectory() as tmp:
            lines = self._lines(self._summary(tmp))
            with_imprint = bd.build_digest(NOW, big, imprint=lines)
            without = bd.build_digest(NOW, big)
        self.assertIn("IMPRINT (captured judgment", with_imprint)
        # identical core: the imprint section never pressures the shed loop
        for line in without.splitlines():
            self.assertIn(line, with_imprint)
        self.assertEqual(with_imprint.splitlines()[-1], bd.PACK_POINTER)

    def test_byte_cap_is_enforced(self):
        huge = ["IMPRINT (captured judgment -- bulk channel):"] + \
               ["- filler %s" % ("y" * 200) for _ in range(100)]
        # imprint_lines caps whatever it renders; feed an oversized summary
        # through the cap loop by rendering a synthetic huge export instead.
        with tempfile.TemporaryDirectory() as tmp:
            text = "## Call\n" + "".join(
                "- **urn:imprint:call:%d** [valid 2026-08-0%dT00:00:00Z..] x\n"
                % (i, (i % 7) + 1) for i in range(5000))
            lines = self._lines(bd.load_imprint(self._write(tmp, text)))
        rendered = "\n".join(lines).encode("utf-8")
        self.assertLessEqual(len(rendered), bd.IMPRINT_CAP_BYTES)
        self.assertLessEqual(
            len("\n".join(huge[:2]).encode("utf-8")), bd.IMPRINT_CAP_BYTES,
            "sanity: cap leaves room for the header + one line")

    def _write(self, tmp, text):
        path = os.path.join(tmp, "imprint-store.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path


class LaunchdProbe(unittest.TestCase):
    """launchctl exit-code surfacing: canned output into the PURE parser.

    No test shells out to the real launchctl -- the parser is the contract, and
    the probe around it is exercised with an injected runner.
    """

    FAILING = (
        "PID\tStatus\tLabel\n"
        "-\t78\tcom.anthonyflores.fully-aware.daily-scan\n"
        "912\t0\tcom.anthonyflores.fully-aware.boot-pack\n"
        "-\t1\tcom.saga.watchdog\n"
        "-\t127\tcom.adobe.updater\n"
        "-\t-\tcom.anthonyflores.iris-client-work-board\n"
    )
    CLEAN = (
        "PID\tStatus\tLabel\n"
        "912\t0\tcom.anthonyflores.fully-aware.boot-pack\n"
        "-\t0\tcom.anthonyflores.fully-aware.daily-scan\n"
        "-\t-\tcom.saga.watchdog\n"
    )

    def test_failing_jobs_are_parsed_with_their_exit_codes(self):
        self.assertEqual(
            bd.parse_launchctl(self.FAILING),
            [("com.anthonyflores.fully-aware.daily-scan", 78),
             ("com.saga.watchdog", 1)])

    def test_clean_output_yields_nothing(self):
        self.assertEqual(bd.parse_launchctl(self.CLEAN), [])

    def test_foreign_labels_and_non_numeric_status_are_ignored(self):
        """A third-party updater's exit 127 is noise; '-' is 'never exited'."""
        labels = [lbl for lbl, _ in bd.parse_launchctl(self.FAILING)]
        self.assertNotIn("com.adobe.updater", labels)
        self.assertNotIn("com.anthonyflores.iris-client-work-board", labels)

    def test_malformed_output_is_survived(self):
        for text in ("", None, "garbage without tabs\n", "a\tb\n", "\t\t\t\n"):
            with self.subTest(text=text):
                self.assertEqual(bd.parse_launchctl(text), [])

    def test_warnings_render_into_the_digest_warnings_section(self):
        warnings = bd.launchctl_warnings(runner=lambda: self.FAILING)
        md = bd.build_digest(NOW, _pack(warnings=["pack said something"]),
                             extra_warnings=warnings)
        self.assertIn("WARNINGS (3):", md)
        self.assertIn("- WARNING: launchd job "
                      "com.anthonyflores.fully-aware.daily-scan last exit 78", md)
        self.assertIn("- WARNING: launchd job com.saga.watchdog last exit 1", md)

    def test_locally_probed_warnings_outlive_pack_warnings_under_the_cap(self):
        """The shed loop pops from the tail: a dead job is what a session most
        needs, so it leads the section.

        Same stable synthetic pointer as ``test_attention_is_shed_last``, so
        the priority does not change with the checkout's path length.
        """
        pack = _pack(warnings=["pack warning %d %s" % (i, "x" * 100)
                               for i in range(40)])
        pack_md = "/synthetic/BOOT-PACK.md"
        with unittest.mock.patch.object(bd, "PACK_MD", pack_md), \
                unittest.mock.patch.object(
                    bd, "PACK_POINTER", "Full pack: %s" % pack_md):
            md = bd.build_digest(NOW, pack, cap_tokens=120,
                                 extra_warnings=bd.launchctl_warnings(
                                     runner=lambda: self.FAILING))
        self.assertIn("com.anthonyflores.fully-aware.daily-scan last exit 78", md)
        self.assertIn("[truncated]", md)

    def test_clean_launchctl_adds_no_warnings_section(self):
        md = bd.build_digest(NOW, _pack(), extra_warnings=bd.launchctl_warnings(
            runner=lambda: self.CLEAN))
        self.assertNotIn("WARNINGS", md)

    def test_missing_launchctl_binary_is_a_clean_skip(self):
        """CI runs on Linux: no binary, no probe, no noise."""
        with unittest.mock.patch.object(bd.shutil, "which", return_value=None):
            self.assertEqual(bd.launchctl_warnings(), [])

    def test_probe_failure_degrades_to_silence(self):
        def boom():
            raise OSError("launchctl exploded")
        self.assertEqual(bd.launchctl_warnings(runner=boom), [])

    def test_probe_is_read_only(self):
        """D30: this generator reads launchd state and never acts on it."""
        with open(os.path.join(_HERE, "boot-digest.py"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn('["launchctl", "list"]', body)
        for verb in ("load", "unload", "bootstrap", "bootout", "kickstart",
                     "enable", "disable", "remove"):
            self.assertNotIn('"launchctl", "%s"' % verb, body)


class DegradeNotAbort(unittest.TestCase):
    """A missing/unusable pack writes NOTHING and exits 0."""

    def _run_main(self, tmp, pack_path):
        out = os.path.join(tmp, "BOOT-DIGEST.md")
        rc = bd.main(["--pack", pack_path, "--out", out,
                      "--daily-scan", os.path.join(tmp, "nope.md")])
        return rc, out

    def test_missing_pack_is_silent_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._run_main(tmp, os.path.join(tmp, "nope.json"))
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(out))

    def test_unparseable_pack_leaves_an_existing_digest_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "boot-pack.json")
            with open(broken, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            out = os.path.join(tmp, "BOOT-DIGEST.md")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("previous digest\n")
            rc = bd.main(["--pack", broken, "--out", out,
                          "--daily-scan", os.path.join(tmp, "nope.md")])
            self.assertEqual(rc, 0)
            with open(out, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "previous digest\n")

    def test_wrong_schema_pack_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_pack(tmp, _pack(schema="boot-pack/v2"))
            rc, out = self._run_main(tmp, path)
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(out))


class CliWrites(unittest.TestCase):
    def test_out_path_is_written_and_pack_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = _pack(surfaces=[_surface("synth-behind", behind=2)])
            path = _write_pack(tmp, pack)
            before = os.path.getmtime(path)
            out = os.path.join(tmp, "BOOT-DIGEST.md")
            rc = bd.main(["--pack", path, "--out", out,
                          "--daily-scan", os.path.join(tmp, "nope.md")])
            self.assertEqual(rc, 0)
            with open(out, "r", encoding="utf-8") as fh:
                self.assertIn("synth-behind", fh.read())
            self.assertEqual(os.path.getmtime(path), before)
            with open(path, "r", encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), pack)

    def test_write_leaves_no_tmp_file_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_pack(tmp, _pack())
            out = os.path.join(tmp, "BOOT-DIGEST.md")
            rc = bd.main(["--pack", path, "--out", out,
                          "--daily-scan", os.path.join(tmp, "nope.md")])
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(out + ".tmp"))
            self.assertEqual(sorted(os.listdir(tmp)),
                             ["BOOT-DIGEST.md", "boot-pack.json"])

    def test_write_is_atomic_via_replace_of_a_complete_tmp(self):
        """The SessionStart hook is a concurrent reader: never a partial file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_pack(tmp, _pack())
            out = os.path.join(tmp, "BOOT-DIGEST.md")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("PREVIOUS DIGEST\n")
            seen = {}
            real_replace = os.replace

            def spy(src, dst):
                with open(src, "r", encoding="utf-8") as fh:
                    seen["staged"] = fh.read()
                with open(dst, "r", encoding="utf-8") as fh:
                    seen["dest_before_swap"] = fh.read()
                seen["src"], seen["dst"] = src, dst
                return real_replace(src, dst)

            with unittest.mock.patch.object(bd.os, "replace", spy):
                rc = bd.main(["--pack", path, "--out", out,
                              "--daily-scan", os.path.join(tmp, "nope.md")])
            self.assertEqual(rc, 0)
            self.assertEqual(seen["src"], out + ".tmp")
            self.assertEqual(seen["dst"], out)
            # The old digest was still whole right up to the swap ...
            self.assertEqual(seen["dest_before_swap"], "PREVIOUS DIGEST\n")
            # ... and what got swapped in was the complete new digest.
            self.assertTrue(seen["staged"].rstrip("\n").endswith(bd.PACK_POINTER))
            with open(out, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), seen["staged"])

    def test_defaults_point_at_the_repo_state_dir(self):
        self.assertEqual(bd._default("state", "boot-pack.json"),
                         os.path.join(os.path.dirname(_HERE), "state",
                                      "boot-pack.json"))


class WrapperStep(unittest.TestCase):
    """morning-pack.sh generates the digest AFTER the pack, degrade-not-abort."""

    def _body(self):
        with open(_WRAPPER, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_digest_step_runs_after_the_assembler(self):
        body = self._body()
        self.assertIn("boot-digest.py", body)
        self.assertLess(body.rindex("assemble-boot-pack.py"),
                        body.index("boot-digest.py"))

    def test_digest_failure_does_not_abort_the_wrapper(self):
        body = self._body()
        self.assertNotRegex(body, r"set -e(?:[^a-zA-Z]|$)")
        self.assertIn("boot digest FAILED", body)

    def test_wrapper_exits_with_the_assembler_status(self):
        self.assertIn("exit ${asm_rc}", self._body())

    def test_digest_step_is_guarded_on_an_argument_free_run(self):
        body = self._body()
        self.assertIn("if [ $# -eq 0 ]; then", body)
        self.assertLess(body.index("if [ $# -eq 0 ]; then"),
                        body.index("boot-digest.py"))
        self.assertIn("morning-pack: args passed, skipping boot digest", body)


# --------------------------------------------------------------------------- #
# EXECUTING wrapper tests: morning-pack.sh is copied into a tempdir alongside
# SHIM tools, so REPO_ROOT resolves to the tempdir and nothing touches the real
# repo, the real state/ dir, or any other checkout. The shims are real Python
# (invoked through FULLY_AWARE_PYTHON), so the wrapper's actual dispatch and
# exit-status plumbing is exercised -- not a grep over its source.
# --------------------------------------------------------------------------- #
_SHIM_ASSEMBLER = '''#!/usr/bin/env python3
import os, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(root, "event-log"), "a", encoding="utf-8") as fh:
    fh.write("assembler\\n")
with open(os.path.join(root, "assembler-called"), "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("SHIM_ASSEMBLER_RC", "0")))
'''

_SHIM_DIGEST = '''#!/usr/bin/env python3
import os
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(root, "event-log"), "a", encoding="utf-8") as fh:
    fh.write("digest\\n")
state = os.path.join(root, "state")
os.makedirs(state, exist_ok=True)
with open(os.path.join(state, "BOOT-DIGEST.md"), "a", encoding="utf-8") as fh:
    fh.write("shim digest\\n")
'''

_SHIM_GENERATOR = '''#!/usr/bin/env python3
import os, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(root, "event-log"), "a", encoding="utf-8") as fh:
    fh.write("generator\\n")
sys.exit(0)
'''

_SHIM_VERIFIER = '''#!/usr/bin/env python3
import os, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(root, "event-log"), "a", encoding="utf-8") as fh:
    fh.write("verifier\\n")
sys.exit(int(os.environ.get("SHIM_VERIFIER_RC", "0")))
'''


class WrapperExecution(unittest.TestCase):
    """morning-pack.sh, actually run, against shimmed tools in a tempdir."""

    def _fixture(self, tmp):
        tools = os.path.join(tmp, "tools")
        configs = os.path.join(tools, "configs")
        os.makedirs(configs)
        with open(os.path.join(configs, "synthetic.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"schema": "surface-config/v1",
                       "environment": "synthetic"}, fh)
        shutil.copy2(_WRAPPER, os.path.join(tools, "morning-pack.sh"))
        for name, src in (("assemble-boot-pack.py", _SHIM_ASSEMBLER),
                          ("boot-digest.py", _SHIM_DIGEST),
                          ("generate-surface.py", _SHIM_GENERATOR),
                          ("verify-defects.py", _SHIM_VERIFIER)):
            path = os.path.join(tools, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return os.path.join(tools, "morning-pack.sh")

    def _run(self, tmp, args=(), assembler_rc=0, verifier_rc=0):
        wrapper = self._fixture(tmp)
        env = dict(os.environ)
        env["FULLY_AWARE_PYTHON"] = sys.executable
        env["SHIM_ASSEMBLER_RC"] = str(assembler_rc)
        env["SHIM_VERIFIER_RC"] = str(verifier_rc)
        proc = subprocess.run(["bash", wrapper] + list(args), env=env, cwd=tmp,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return proc

    def _digest(self, tmp):
        return os.path.join(tmp, "state", "BOOT-DIGEST.md")

    def _assembler_args(self, tmp):
        marker = os.path.join(tmp, "assembler-called")
        if not os.path.isfile(marker):
            return None
        with open(marker, "r", encoding="utf-8") as fh:
            return fh.read().splitlines()

    def _events(self, tmp):
        with open(os.path.join(tmp, "event-log"), "r", encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_argless_run_writes_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
            self.assertEqual(self._assembler_args(tmp), [""])
            self.assertTrue(os.path.isfile(self._digest(tmp)))
            self.assertEqual(self._events(tmp),
                             ["generator", "verifier", "assembler", "digest"])

    def test_preview_args_skip_the_digest_entirely(self):
        """--stdout / --out-json previews must not rewrite boot state."""
        for args in (["--stdout"], ["--out-json", "/dev/null"]):
            with self.subTest(args=args):
                with tempfile.TemporaryDirectory() as tmp:
                    proc = self._run(tmp, args)
                    out = proc.stdout.decode("utf-8")
                    self.assertEqual(proc.returncode, 0,
                                     proc.stderr.decode("utf-8"))
                    # forwarded to the assembler ...
                    self.assertEqual(self._assembler_args(tmp),
                                     [" ".join(args)])
                    # ... but no digest was generated.
                    self.assertFalse(os.path.exists(self._digest(tmp)))
                    self.assertIn("args passed, skipping boot digest", out)
                    self.assertNotIn("generating boot digest", out)
                    self.assertEqual(self._events(tmp),
                                     ["generator", "verifier", "assembler"])

    def test_failing_verifier_degrades_and_assembler_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(tmp, verifier_rc=7)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
            self.assertIn("defect verify FAILED (exit 7)",
                          proc.stderr.decode("utf-8"))
            self.assertEqual(self._events(tmp),
                             ["generator", "verifier", "assembler", "digest"])

    def test_failing_assembler_propagates_its_status_with_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(tmp, ["--stdout"], assembler_rc=3)
            self.assertEqual(proc.returncode, 3, proc.stderr.decode("utf-8"))
            self.assertFalse(os.path.exists(self._digest(tmp)))

    def test_failing_assembler_propagates_its_status_argless(self):
        """The digest step must not swallow or crash on the assembler's failure."""
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(tmp, assembler_rc=4)
            self.assertEqual(proc.returncode, 4, proc.stderr.decode("utf-8"))
            # degrade-not-abort: the digest still ran over whatever pack exists.
            self.assertTrue(os.path.isfile(self._digest(tmp)))

    def test_digest_is_written_once_per_argless_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            with open(self._digest(tmp), "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "shim digest\n")


class HookBody(unittest.TestCase):
    """session-digest-hook.sh: fresh -> cat, stale -> one line, absent -> silence."""

    def _run(self, digest_text=None, age_hours=0, brief_text=None):
        with tempfile.TemporaryDirectory() as tmp:
            digest = os.path.join(tmp, "BOOT-DIGEST.md")
            if digest_text is not None:
                with open(digest, "w", encoding="utf-8") as fh:
                    fh.write(digest_text)
                if age_hours:
                    old = os.path.getmtime(digest) - age_hours * 3600
                    os.utime(digest, (old, old))
            brief = os.path.join(tmp, "LATEST.md")
            if brief_text is not None:
                with open(brief, "w", encoding="utf-8") as fh:
                    fh.write(brief_text)
            # The shipped hook hardcodes absolute paths (asserted separately);
            # exercise its LOGIC against a fixture copy pointed at the tempdir.
            # BOTH paths are redirected -- a test that reads the real
            # state/daily-scan/LATEST.md reports this machine, not the hook.
            with open(_HOOK, "r", encoding="utf-8") as fh:
                body = fh.read()
            body = body.replace(
                '"/Users/anthonyflores/code/fully-aware/state/BOOT-DIGEST.md"',
                '"%s"' % digest).replace(
                '"/Users/anthonyflores/code/fully-aware/state/daily-scan/LATEST.md"',
                '"%s"' % brief)
            script = os.path.join(tmp, "hook.sh")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(body)
            proc = subprocess.run(["bash", script], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            return proc.returncode, proc.stdout.decode("utf-8")

    def test_fresh_digest_is_printed(self):
        rc, out = self._run("# FULLY AWARE -- BOOT DIGEST (advisory, not law)\n")
        self.assertEqual(rc, 0)
        self.assertIn("BOOT DIGEST", out)

    def test_stale_digest_prints_one_warning_line(self):
        rc, out = self._run("# stale content\n", age_hours=40)
        self.assertEqual(rc, 0)
        self.assertEqual(
            out.strip(),
            "fully-aware boot digest is stale (>36h) -- check the boot-pack "
            "LaunchAgent (com.anthonyflores.fully-aware.boot-pack)")
        self.assertNotIn("stale content", out)

    def test_stale_line_names_the_agent_that_actually_writes_the_digest(self):
        """The digest comes from the boot-pack agent -- not any daily-scan job."""
        label = "com.anthonyflores.fully-aware.boot-pack"
        self.assertTrue(os.path.isfile(
            os.path.join(os.path.dirname(_HERE), "launchd", label + ".plist")),
            "stale message must name a LaunchAgent this repo actually ships")
        with open(_HOOK, "r", encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn(label, body)
        self.assertNotIn("daily-scan LaunchAgent", body)

    def test_digest_output_is_bounded(self):
        """The hook consumes a file it does not control: head -c, never cat."""
        rc, out = self._run("x" * 20000 + "\n")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 12000)
        with open(_HOOK, "r", encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("head -c 12000", body)
        self.assertNotRegex(body, r"(?m)^\s*cat ")

    def test_bound_covers_both_generator_caps(self):
        """12000 >= core token cap (~2000 B) + IMPRINT_CAP_BYTES, with margin."""
        self.assertGreaterEqual(
            12000, bd.HARD_CAP_TOKENS * 4 + bd.IMPRINT_CAP_BYTES)

    def test_absent_digest_prints_nothing(self):
        rc, out = self._run(None)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_ships_the_absolute_digest_path(self):
        with open(_HOOK, "r", encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn(
            "/Users/anthonyflores/code/fully-aware/state/BOOT-DIGEST.md", body)
        self.assertNotRegex(body, r"set -e(?:[^a-zA-Z]|$)")

    def test_live_daily_brief_line_follows_the_digest(self):
        """05:45 digest vs 06:15 scan: quote the LIVE brief at consumption time.

        Its first line is the brief's own date stamp, so a scan that did not
        run today is visible in the session's opening context.
        """
        rc, out = self._run("# digest body\n",
                            brief_text="Daily brief -- 2026-08-08\n\nheadline\n")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(lines[-1], "Daily brief (live): Daily brief -- 2026-08-08")
        self.assertLess(lines.index("# digest body"), len(lines) - 1)
        self.assertNotIn("headline", out)

    def test_absent_live_brief_adds_no_line(self):
        rc, out = self._run("# digest body\n")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "# digest body\n")

    def test_empty_live_brief_adds_no_line(self):
        rc, out = self._run("# digest body\n", brief_text="\n")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "# digest body\n")

    def test_live_brief_line_is_bounded(self):
        rc, out = self._run("# digest body\n", brief_text="z" * 5000 + "\n")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()[-1]), len("Daily brief (live): ") + 200)

    def test_stale_digest_still_prints_only_its_one_line(self):
        """The stale branch exits before the digest is emitted; it stays one line."""
        rc, out = self._run("# stale content\n", age_hours=40,
                            brief_text="Daily brief -- 2026-08-08\n")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("stale (>36h)", out)

    def test_ships_the_absolute_daily_scan_path(self):
        with open(_HOOK, "r", encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn(
            "/Users/anthonyflores/code/fully-aware/state/daily-scan/LATEST.md",
            body)
        # Same portability discipline as the mtime read (PR #12): no GNU-only
        # flags, no process substitution, no `cat`.
        self.assertNotRegex(body, r"(?m)^\s*cat ")
        self.assertNotIn("head -1", body)  # POSIX spelling is head -n 1


def _git(*args, **kwargs):
    """Run git with a synthetic identity (fixture repos need no real config)."""
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "f@example.invalid",
                "GIT_COMMITTER_NAME": "fixture",
                "GIT_COMMITTER_EMAIL": "f@example.invalid"})
    return subprocess.run(("git",) + args, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **kwargs)


def _installer_fixture(root):
    """Copy the real installer + a stand-in hook body into <root>/tools/."""
    tools = os.path.join(root, "tools")
    os.makedirs(tools, exist_ok=True)
    shutil.copy2(_INSTALLER, os.path.join(tools, "install-digest-hook.sh"))
    with open(os.path.join(tools, "session-digest-hook.sh"), "w",
              encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\nexit 0\n")
    return os.path.join(tools, "install-digest-hook.sh")


class InstallerIdempotence(unittest.TestCase):
    """The installer runs against a THROWAWAY HOME -- never the real settings.

    It also runs from a COPY of itself in a tempdir, not from this checkout: the
    installer refuses to run inside a linked git worktree (see InstallerWorktree
    below), and these tests are about the settings.json edit, not that guard.
    """

    IMPRINT = {
        "matcher": "",
        "hooks": [{"type": "command",
                   "command": "/synthetic/imprint-local/session_start.py "
                              "# imprint-local-managed-hook"}],
    }

    def _install(self, home):
        env = dict(os.environ)
        env["HOME"] = home
        installer = os.path.join(home, "fixture-repo", "tools",
                                 "install-digest-hook.sh")
        if not os.path.isfile(installer):
            installer = _installer_fixture(os.path.join(home, "fixture-repo"))
        return subprocess.run(["bash", installer], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _settings(self, home):
        with open(os.path.join(home, ".claude", "settings.json"),
                  "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _seed(self, home):
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(os.path.join(home, ".claude", "settings.json"),
                  "w", encoding="utf-8") as fh:
            json.dump({"model": "synthetic",
                       "hooks": {"SessionStart": [dict(self.IMPRINT)]}},
                      fh, indent=2)

    def test_installs_once_then_no_ops(self):
        with tempfile.TemporaryDirectory() as home:
            self._seed(home)
            first = self._install(home)
            self.assertEqual(first.returncode, 0, first.stderr.decode("utf-8"))
            self.assertIn("installed", first.stdout.decode("utf-8"))
            after_first = self._settings(home)

            second = self._install(home)
            self.assertEqual(second.returncode, 0, second.stderr.decode("utf-8"))
            self.assertIn("already installed", second.stdout.decode("utf-8"))
            self.assertEqual(self._settings(home), after_first)

    def test_imprint_entries_are_left_alone(self):
        with tempfile.TemporaryDirectory() as home:
            self._seed(home)
            self._install(home)
            entries = self._settings(home)["hooks"]["SessionStart"]
            self.assertEqual(entries[0], self.IMPRINT)
            self.assertEqual(len(entries), 2)
            self.assertIn("session-digest-hook.sh",
                          entries[1]["hooks"][0]["command"])
            self.assertTrue(entries[1]["hooks"][0]["command"].startswith("bash /"))

    def test_backup_written_on_change_only(self):
        with tempfile.TemporaryDirectory() as home:
            self._seed(home)
            self._install(home)
            backups = [f for f in os.listdir(os.path.join(home, ".claude"))
                       if ".bak-" in f]
            self.assertEqual(len(backups), 1, backups)
            self._install(home)
            backups = [f for f in os.listdir(os.path.join(home, ".claude"))
                       if ".bak-" in f]
            self.assertEqual(len(backups), 1, backups)

    def test_refuses_to_create_a_missing_settings_file(self):
        with tempfile.TemporaryDirectory() as home:
            proc = self._install(home)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("refusing to create",
                          proc.stderr.decode("utf-8"))
            self.assertFalse(os.path.exists(
                os.path.join(home, ".claude", "settings.json")))

    def test_installer_targets_the_hook_body_beside_it(self):
        with open(_INSTALLER, "r", encoding="utf-8") as fh:
            self.assertIn('HOOK="${REPO_ROOT}/tools/session-digest-hook.sh"',
                          fh.read())


class InstallerWorktreeGuard(unittest.TestCase):
    """settings.json outlives a worktree: never register a path inside one.

    The command written into ~/.claude/settings.json is an absolute path to the
    hook body. Installed from a linked worktree, that path dies with the
    worktree and every later session start silently runs a missing script.
    """

    IMPRINT = InstallerIdempotence.IMPRINT

    def _seed_home(self, home):
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(os.path.join(home, ".claude", "settings.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"model": "synthetic",
                       "hooks": {"SessionStart": [dict(self.IMPRINT)]}},
                      fh, indent=2)

    def _run(self, installer, home):
        env = dict(os.environ)
        env["HOME"] = home
        return subprocess.run(["bash", installer], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _settings(self, home):
        with open(os.path.join(home, ".claude", "settings.json"),
                  "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_refuses_from_a_linked_worktree_and_allows_the_main_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "main")
            linked = os.path.join(tmp, "linked")
            home = os.path.join(tmp, "home")
            os.makedirs(main)
            self._seed_home(home)

            init = _git("init", "-q", main)
            if init.returncode != 0:
                self.skipTest("git init unavailable: %s"
                              % init.stderr.decode("utf-8"))
            with open(os.path.join(main, "README.md"), "w",
                      encoding="utf-8") as fh:
                fh.write("fixture\n")
            self.assertEqual(_git("add", "-A", cwd=main).returncode, 0)
            self.assertEqual(_git("commit", "-qm", "fixture", cwd=main).returncode,
                             0)
            wt = _git("worktree", "add", "-q", "-b", "fixture-branch", linked,
                      cwd=main)
            self.assertEqual(wt.returncode, 0, wt.stderr.decode("utf-8"))

            # From the LINKED worktree: refuse, touch nothing.
            before = self._settings(home)
            proc = self._run(_installer_fixture(linked), home)
            err = proc.stderr.decode("utf-8")
            self.assertEqual(proc.returncode, 1, proc.stdout.decode("utf-8"))
            self.assertIn("linked worktree", err)
            self.assertIn(linked, err)
            self.assertEqual(self._settings(home), before)
            self.assertEqual(
                [f for f in os.listdir(os.path.join(home, ".claude"))
                 if ".bak-" in f], [])

            # From the MAIN checkout of the same repo: install normally.
            ok = self._run(_installer_fixture(main), home)
            self.assertEqual(ok.returncode, 0, ok.stderr.decode("utf-8"))
            entries = self._settings(home)["hooks"]["SessionStart"]
            self.assertEqual(len(entries), 2)
            self.assertIn(os.path.join(main, "tools", "session-digest-hook.sh"),
                          entries[1]["hooks"][0]["command"])

    def test_this_checkout_is_refused_when_it_is_a_linked_worktree(self):
        """Run the SHIPPED installer where it lives -- the guard's real target."""
        repo_root = os.path.dirname(_HERE)
        git_dir = _git("-C", repo_root, "rev-parse", "--git-dir")
        common = _git("-C", repo_root, "rev-parse", "--git-common-dir")
        if git_dir.returncode != 0 or common.returncode != 0:
            self.skipTest("this checkout is not a git repo")
        if git_dir.stdout == common.stdout:
            self.skipTest("this checkout is a main clone, not a linked worktree")
        with tempfile.TemporaryDirectory() as home:
            self._seed_home(home)
            before = self._settings(home)
            proc = self._run(_INSTALLER, home)
            self.assertEqual(proc.returncode, 1, proc.stdout.decode("utf-8"))
            self.assertIn("linked worktree", proc.stderr.decode("utf-8"))
            self.assertEqual(self._settings(home), before)




# --------------------------------------------------------------------------- #
# DEFECTS: the register's count is the first line every session reads
# --------------------------------------------------------------------------- #
DEFECT_COUNTS = {
    # oldest_days is deliberately well clear of the gate threshold, so the
    # baseline line carries the trip point but no warning tail.
    "P0": {"open": 3, "oldest_days": 3},
    "P1": {"open": 9, "oldest_days": 12},
    "P2": {"open": 7, "oldest_days": 30},
    "fixed_since_last": 1, "provisional": 4, "deferred": 0, "error": 0,
    "accepted": 1,
    "open_by_owner": {"anthony": 4, "codex": 5, "session": 10},
}

EXPECTED_SUMMARY = ("DEFECTS -- P0: 3 (oldest 3d; gate blocks at 7d) · "
                    "P1: 9 · P2: 7 · "
                    "fixed since yesterday: 1 · yours today: 4 · "
                    "no real check yet: 4 · accepted: 1")


def _defect_record(item_id, days=6, symptom="a synthetic thing is broken"):
    return {"id": item_id, "severity": "P0", "owner": "anthony",
            "fix_scope": "decision", "status": "open", "days_open": days,
            "symptom": symptom, "fix_hint": "do the synthetic thing"}


def _defect_pack(tmp=None, counts=DEFECT_COUNTS, yours=("SYN-1",),
                 records=None, status_path="", **kw):
    """A pack whose sidecar carries sections.defects (+ an optional status file)."""
    pack = _pack(**kw)
    if records is not None and tmp is not None:
        status_path = os.path.join(tmp, "defects-status.json")
        with open(status_path, "w", encoding="utf-8") as fh:
            json.dump({"schema": "defect-status/v1", "items": records}, fh)
    pack["sections"]["defects"] = {
        "status_path": status_path,
        "generated_at": GENERATED,
        "counts": dict(counts) if counts else {},
        "yours_today": list(yours),
        "rendered_ids": list(yours),
    }
    return pack


class DefectLines(unittest.TestCase):
    def test_summary_is_the_first_line_after_the_title(self):
        md = bd.build_digest(NOW, _defect_pack())
        lines = md.splitlines()
        self.assertEqual(lines[0], bd.TITLE)
        self.assertEqual(lines[1], EXPECTED_SUMMARY)

    def test_pack_header_still_follows_the_defect_lines(self):
        md = bd.build_digest(NOW, _defect_pack())
        self.assertLess(md.index("DEFECTS --"), md.index("Pack generated:"))

    def test_up_to_three_yours_today_lines_follow(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = [_defect_record("SYN-%d" % i, days=i) for i in range(5)]
            pack = _defect_pack(tmp, yours=[r["id"] for r in records],
                                records=records)
            lines = bd.build_digest(NOW, pack).splitlines()
            bullets = [ln for ln in lines if ln.startswith("- SYN-")]
            self.assertEqual(len(bullets), bd.DEFECT_LINES)
            self.assertEqual(lines[2], "- SYN-0 — 0d — a synthetic thing is broken")

    def test_lines_are_compressed_to_the_digest_line_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = [_defect_record("SYN-1", symptom="y" * 400)]
            pack = _defect_pack(tmp, yours=["SYN-1"], records=records)
            md = bd.build_digest(NOW, pack)
            bullet = [ln for ln in md.splitlines() if ln.startswith("- SYN-1")][0]
            self.assertLessEqual(len(bullet), bd.MAX_LINE_CHARS)

    def test_ids_alone_when_the_status_file_is_gone(self):
        """The sidecar names the status file; losing it costs prose, not the count."""
        pack = _defect_pack(status_path="/nonexistent/defects-status.json")
        md = bd.build_digest(NOW, pack)
        self.assertIn(EXPECTED_SUMMARY, md)
        self.assertIn("- SYN-1 — yours today", md)

    def test_a_days_open_that_is_not_a_count_falls_back_to_the_id_line(self):
        """A junk days_open must cost the detail line, never the whole digest.

        The status file is read directly, so nothing upstream has type-checked
        it: a string would raise TypeError on the %d and kill every line the
        digest owes the session, and a bool would silently print as 1d/0d.
        """
        for junk in ("3", "", True, False, -1, -7, {"days": 3}, [3], 3.5, None):
            with self.subTest(days_open=junk):
                with tempfile.TemporaryDirectory() as tmp:
                    record = _defect_record("SYN-1", days=junk)
                    pack = _defect_pack(tmp, yours=["SYN-1"], records=[record])
                    lines = bd.defect_lines(NOW, pack)
                    self.assertEqual(lines[0], EXPECTED_SUMMARY)
                    self.assertEqual(lines[1], "- SYN-1 — yours today")

    def test_a_real_count_still_gets_the_detailed_line(self):
        for days in (0, 1, 12):
            with self.subTest(days_open=days):
                with tempfile.TemporaryDirectory() as tmp:
                    record = _defect_record("SYN-1", days=days)
                    pack = _defect_pack(tmp, yours=["SYN-1"], records=[record])
                    self.assertEqual(
                        bd.defect_lines(NOW, pack)[1],
                        "- SYN-1 — %dd — a synthetic thing is broken" % days)

    def test_older_packs_without_the_section_get_no_defect_lines(self):
        md = bd.build_digest(NOW, _pack())
        self.assertNotIn("DEFECTS", md)
        lines = md.splitlines()
        self.assertEqual(lines[1][:8], "NIGHTLY ")
        self.assertEqual(lines[3][:15], "Pack generated:")

    def test_section_present_but_countless_says_so(self):
        md = bd.build_digest(NOW, _defect_pack(counts={}))
        self.assertIn("DEFECTS -- no status yet (run tools/verify-defects.py)",
                      md)

    def test_defect_lines_are_never_shed_by_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = [_defect_record("SYN-%d" % i, days=i) for i in range(5)]
            fat = _defect_pack(
                tmp, yours=[record["id"] for record in records], records=records,
                warnings=["a degraded synthetic probe %d" % i for i in range(40)],
                open_items=["Open item %d. Trailing background." % i
                            for i in range(20)],
                surfaces=[_surface("synth-%02d" % i, behind=i + 1)
                          for i in range(30)])
            pack_md = "/synthetic/BOOT-PACK.md"
            with unittest.mock.patch.object(bd, "PACK_MD", pack_md), \
                    unittest.mock.patch.object(
                        bd, "PACK_POINTER", "Full pack: %s" % pack_md):
                md = bd.build_digest(NOW, fat, cap_tokens=120)
        lines = md.splitlines()
        self.assertEqual(lines[1], EXPECTED_SUMMARY)
        self.assertEqual([line.split(" ")[1] for line in lines
                          if line.startswith("- SYN-")],
                         ["SYN-0", "SYN-1", "SYN-2"])

    def test_summary_line_drops_the_oldest_note_when_no_p0_is_open(self):
        counts = dict(DEFECT_COUNTS, P0={"open": 0, "oldest_days": 0})
        md = bd.build_digest(NOW, _defect_pack(counts=counts))
        self.assertIn("DEFECTS -- P0: 0 · P1: 9 ·", md)
        self.assertNotIn("oldest", md)


class DefectStaleness(unittest.TestCase):
    """Old counts must never read as this morning's counts."""

    @staticmethod
    def _aged(hours=None, stamp=None, drop=False):
        pack = _defect_pack()
        if drop:
            del pack["sections"]["defects"]["generated_at"]
        elif stamp is not None:
            pack["sections"]["defects"]["generated_at"] = stamp
        else:
            pack["sections"]["defects"]["generated_at"] = (
                NOW - datetime.timedelta(hours=hours)).isoformat(timespec="seconds")
        return pack

    def test_a_fresh_status_carries_no_prefix(self):
        self.assertEqual(bd.defect_lines(NOW, self._aged(hours=3))[0],
                         EXPECTED_SUMMARY)

    def test_the_last_hour_inside_the_window_still_carries_its_own_age(self):
        """Not yet STALE, but far older than the pack: the age must show.

        This is the failure mode GATE-INSTALL-4 found -- a register step that
        crashed leaves a fresh-looking pack carrying day-old counts, and for the
        twelve hours before the 36h line the digest said nothing at all.
        """
        old = self._aged(stamp=(NOW - datetime.timedelta(
            seconds=bd.STALE_DEFECTS)).isoformat(timespec="seconds"))
        self.assertEqual(bd.defect_lines(NOW, old)[0],
                         "AS_OF(36h) " + EXPECTED_SUMMARY)

    def test_a_day_old_status_in_a_fresh_pack_carries_its_age(self):
        day_old = self._aged(hours=24)
        self.assertEqual(bd.defect_lines(NOW, day_old)[0],
                         "AS_OF(24h) " + EXPECTED_SUMMARY)

    def test_the_ordinary_seconds_of_lag_within_one_run_are_not_marked(self):
        """verify-defects runs seconds before the assembler; that is not lag."""
        for minutes in (0, 5, 59):
            with self.subTest(minutes=minutes):
                pack = self._aged(stamp=(NOW - datetime.timedelta(
                    hours=3, minutes=minutes)).isoformat(timespec="seconds"))
                self.assertEqual(bd.defect_lines(NOW, pack)[0], EXPECTED_SUMMARY)

    def test_a_status_past_thirty_six_hours_is_marked_stale_with_its_age(self):
        self.assertEqual(bd.defect_lines(NOW, self._aged(hours=40))[0],
                         "STALE(40h) " + EXPECTED_SUMMARY)
        self.assertEqual(bd.defect_lines(NOW, self._aged(hours=72))[0],
                         "STALE(72h) " + EXPECTED_SUMMARY)

    def test_an_unparseable_stamp_is_marked_rather_than_trusted(self):
        for junk in ("not a timestamp", "2026-13-99", ""):
            self.assertEqual(bd.defect_lines(NOW, self._aged(stamp=junk))[0],
                             "AS_OF-UNPARSEABLE " + EXPECTED_SUMMARY,
                             msg=junk)

    def test_a_sidecar_with_no_stamp_at_all_is_left_alone(self):
        """An older pack that never recorded the stamp must not read as stale."""
        self.assertEqual(bd.defect_lines(NOW, self._aged(drop=True))[0],
                         EXPECTED_SUMMARY)

    def test_the_mark_reaches_the_rendered_digest_not_just_the_line(self):
        md = bd.build_digest(NOW, self._aged(hours=48))
        self.assertEqual(md.splitlines()[1], "STALE(48h) " + EXPECTED_SUMMARY)

    def test_the_countless_line_is_never_prefixed(self):
        pack = _defect_pack(counts={})
        pack["sections"]["defects"]["generated_at"] = (
            NOW - datetime.timedelta(hours=99)).isoformat(timespec="seconds")
        self.assertEqual(bd.defect_lines(NOW, pack),
                         ["DEFECTS -- no status yet (run tools/verify-defects.py)"])


# --------------------------------------------------------------------------- #
# The gate's trip point, the register's own failures, and the night.
# (review 2026-08-28: EXIT-TEST-1, GATE-INSTALL-4, EXIT-TEST-4, LAUNCHD-ENV-3/5)
# --------------------------------------------------------------------------- #
class GateTripPoint(unittest.TestCase):
    """"oldest 6d" never said where the machine-wide block starts."""

    @staticmethod
    def _line(oldest, open_count=3):
        counts = dict(DEFECT_COUNTS,
                      P0={"open": open_count, "oldest_days": oldest})
        return bd.defects_summary_line(counts)

    def test_the_line_names_the_day_the_gate_blocks(self):
        self.assertIn("(oldest 3d; gate blocks at 7d)", self._line(3))

    def test_the_eve_of_the_threshold_says_so(self):
        line = self._line(6)
        self.assertIn("(oldest 6d; gate blocks at 7d)", line)
        self.assertTrue(line.endswith(" — gate arms tomorrow"), line)

    def test_two_days_out_gets_no_warning_tail(self):
        self.assertNotIn("gate arms tomorrow", self._line(5))
        self.assertNotIn("gate is blocking now", self._line(5))

    def test_at_or_past_the_threshold_the_line_says_it_is_blocking(self):
        for oldest in (7, 9, 40):
            with self.subTest(oldest=oldest):
                line = self._line(oldest)
                self.assertTrue(line.endswith(" — gate is blocking now"), line)
                self.assertNotIn("arms tomorrow", line)

    def test_no_open_p0_means_no_gate_talk_at_all(self):
        line = self._line(0, open_count=0)
        self.assertNotIn("gate", line)
        self.assertNotIn("oldest", line)

    def test_the_threshold_matches_the_hook_that_enforces_it(self):
        """The number is duplicated, so this test is the coupling."""
        spec = importlib.util.spec_from_file_location(
            "defect_gate", os.path.join(_HERE, "hooks", "defect_gate.py"))
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        self.assertEqual(bd.GATE_BLOCKS_AT_DAYS, gate.DEFAULT_DAYS)


class RegisterStepFailed(unittest.TestCase):
    """A register step that crashed must be a line, not a silent countdown."""

    def _pack_with_marker(self, tmp, marker_body, status_age=0, marker_age=0):
        status_path = os.path.join(tmp, "defects-status.json")
        with open(status_path, "w", encoding="utf-8") as fh:
            json.dump({"schema": "defect-status/v1", "items": []}, fh)
        marker = os.path.join(tmp, "defects-status.FAILED")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(marker_body)
        now = datetime.datetime.now().timestamp()
        os.utime(status_path, (now - status_age, now - status_age))
        os.utime(marker, (now - marker_age, now - marker_age))
        return _defect_pack(status_path=status_path), marker

    def test_a_marker_newer_than_the_status_leads_the_defect_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack, _ = self._pack_with_marker(
                tmp, "verify-defects FAILED exit=2 at=2026-08-28T05:45:12-0700\n",
                status_age=90000, marker_age=0)
            lines = bd.defect_lines(NOW, pack)
        self.assertEqual(
            lines[0],
            "DEFECTS -- register step FAILED at 2026-08-28T05:45:12-0700")
        self.assertIn(EXPECTED_SUMMARY, lines[1])

    def test_the_failed_line_reaches_the_rendered_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack, _ = self._pack_with_marker(
                tmp, "verify-defects FAILED exit=2 at=2026-08-28T05:45:12-0700\n",
                status_age=90000, marker_age=0)
            md = bd.build_digest(NOW, pack)
        self.assertEqual(md.splitlines()[1],
                         "DEFECTS -- register step FAILED at "
                         "2026-08-28T05:45:12-0700")

    def test_a_marker_older_than_the_status_is_a_failure_already_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack, _ = self._pack_with_marker(
                tmp, "verify-defects FAILED exit=2 at=2026-08-27T05:45:12-0700\n",
                status_age=0, marker_age=90000)
            lines = bd.defect_lines(NOW, pack)
        self.assertNotIn("FAILED", lines[0])

    def test_no_marker_at_all_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_path = os.path.join(tmp, "defects-status.json")
            with open(status_path, "w", encoding="utf-8") as fh:
                json.dump({"schema": "defect-status/v1", "items": []}, fh)
            lines = bd.defect_lines(NOW, _defect_pack(status_path=status_path))
        self.assertEqual(lines[0], EXPECTED_SUMMARY)

    def test_a_marker_with_no_timestamp_falls_back_to_its_own_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack, marker = self._pack_with_marker(
                tmp, "something went wrong\n", status_age=90000, marker_age=0)
            lines = bd.defect_lines(NOW, pack)
            expected = datetime.datetime.fromtimestamp(
                os.path.getmtime(marker)).isoformat(timespec="seconds")
        self.assertEqual(lines[0],
                         "DEFECTS -- register step FAILED at %s" % expected)


class NightlyLine(unittest.TestCase):
    """One line so a session can see what the 02:30 lane did."""

    def test_no_run_log_directory_means_the_lane_is_not_armed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bd.nightly_line(os.path.join(tmp, "nope")),
                             "NIGHTLY -- lane not armed")

    def test_an_empty_directory_means_no_run_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bd.nightly_line(tmp), "NIGHTLY -- no run recorded")

    def test_only_prompt_and_last_message_files_still_means_no_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("20260829-MAP-1.prompt.md", "20260829-MAP-1.last.md",
                         "20260829-MAP-1.codex.log", "attempts.json"):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write("x")
            self.assertEqual(bd.nightly_line(tmp), "NIGHTLY -- no run recorded")

    def test_the_newest_run_log_gives_the_id_and_the_outcome(self):
        for outcome in ("pr-opened", "pr-check-failed", "codex-failed",
                        "no-changes", "clone-failed"):
            with self.subTest(outcome=outcome):
                with tempfile.TemporaryDirectory() as tmp:
                    self._write_log(tmp, "20260829-MAP-1.md", "MAP-1", outcome)
                    self.assertEqual(bd.nightly_line(tmp),
                                     "NIGHTLY -- MAP-1: %s" % outcome)

    def test_the_newest_of_several_logs_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            older = self._write_log(tmp, "20260827-SAGA-1.md", "SAGA-1",
                                    "no-changes")
            self._write_log(tmp, "20260829-MAP-1.md", "MAP-1", "pr-opened")
            stale = datetime.datetime.now().timestamp() - 200000
            os.utime(older, (stale, stale))
            self.assertEqual(bd.nightly_line(tmp), "NIGHTLY -- MAP-1: pr-opened")

    def test_a_log_with_no_outcome_line_says_so_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "20260829-MAP-1.md"), "w",
                      encoding="utf-8") as fh:
                fh.write("# Nightly fix MAP-1 -- 2026-08-29\n\ntruncated\n")
            self.assertEqual(bd.nightly_line(tmp),
                             "NIGHTLY -- MAP-1: outcome not recorded")

    def test_the_id_falls_back_to_the_filename_when_the_header_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "20260829-MAP-1.md"), "w",
                      encoding="utf-8") as fh:
                fh.write("Outcome: pr-opened\n")
            self.assertEqual(bd.nightly_line(tmp), "NIGHTLY -- MAP-1: pr-opened")

    def test_the_line_reaches_the_digest_and_survives_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_log(tmp, "20260829-MAP-1.md", "MAP-1", "pr-opened")
            with unittest.mock.patch.object(bd, "NIGHTLY_DIR", tmp):
                md = bd.build_digest(NOW, _defect_pack(), cap_tokens=60)
        self.assertIn("NIGHTLY -- MAP-1: pr-opened", md)

    def test_a_pack_with_no_defect_section_still_shows_the_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(bd, "NIGHTLY_DIR",
                                            os.path.join(tmp, "nope")):
                md = bd.build_digest(NOW, _pack())
        self.assertIn("NIGHTLY -- lane not armed", md)

    @staticmethod
    def _write_log(directory, name, item_id, outcome):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Nightly fix %s -- 2026-08-29\n\nMode: live\n"
                     "Outcome: %s\n\n## What happened\n\n- did a thing\n"
                     % (item_id, outcome))
        return path


class WrapperEnvironmentAndMarker(unittest.TestCase):
    """One PATH contract across the lanes; a loud marker when the register dies."""

    def _body(self):
        with open(_WRAPPER, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_the_path_includes_the_local_bin_every_other_lane_has(self):
        self.assertIn(
            'export PATH="/opt/homebrew/bin:/usr/local/bin:${HOME}/.local/bin:${PATH}"',
            self._body())

    def test_a_failed_register_step_writes_a_marker_the_digest_can_see(self):
        body = self._body()
        self.assertIn("defects-status.FAILED", body)
        self.assertIn("verify-defects FAILED exit=%s at=%s", body)

    def test_a_successful_register_step_clears_the_marker(self):
        self.assertIn('rm -f "${DEFECTS_FAILED}"', self._body())

    def test_the_register_step_still_degrades_rather_than_aborting(self):
        body = self._body()
        self.assertNotRegex(body, r"set -e(?:[^a-zA-Z]|$)")
        self.assertIn("defect verify FAILED", body)


if __name__ == "__main__":
    unittest.main()
