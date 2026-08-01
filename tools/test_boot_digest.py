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
import subprocess
import tempfile
import unittest

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
        self.assertIn("- synth-behind: 8 behind origin/main", md)
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
        self.assertIn("- synth-both: 1 behind origin/main, 1 decision-queue item(s)",
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
        """Open items go first, warnings next, attention only as a last resort."""
        md = bd.build_digest(NOW, self._fat_pack(), cap_tokens=120)
        self.assertIn("- synth-00: 1 behind origin/main", md)

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

    def test_absent_brief_adds_no_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "LATEST.md")
            self.assertIsNone(bd.load_daily_brief(missing))
            md = bd.build_digest(NOW, _pack(),
                                 daily_brief=bd.load_daily_brief(missing),
                                 daily_brief_path=missing)
            self.assertNotIn("Daily brief:", md)


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


class HookBody(unittest.TestCase):
    """session-digest-hook.sh: fresh -> cat, stale -> one line, absent -> silence."""

    def _run(self, digest_text=None, age_hours=0):
        with tempfile.TemporaryDirectory() as tmp:
            digest = os.path.join(tmp, "BOOT-DIGEST.md")
            if digest_text is not None:
                with open(digest, "w", encoding="utf-8") as fh:
                    fh.write(digest_text)
                if age_hours:
                    old = os.path.getmtime(digest) - age_hours * 3600
                    os.utime(digest, (old, old))
            # The shipped hook hardcodes an absolute path (asserted separately);
            # exercise its LOGIC against a fixture copy pointed at the tempdir.
            with open(_HOOK, "r", encoding="utf-8") as fh:
                body = fh.read()
            body = body.replace(
                '"/Users/anthonyflores/code/fully-aware/state/BOOT-DIGEST.md"',
                '"%s"' % digest)
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
            "fully-aware boot digest is stale (>36h) -- check the daily-scan "
            "LaunchAgent")
        self.assertNotIn("stale content", out)

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


class InstallerIdempotence(unittest.TestCase):
    """The installer runs against a THROWAWAY HOME -- never the real settings."""

    IMPRINT = {
        "matcher": "",
        "hooks": [{"type": "command",
                   "command": "/synthetic/imprint-local/session_start.py "
                              "# imprint-local-managed-hook"}],
    }

    def _install(self, home):
        env = dict(os.environ)
        env["HOME"] = home
        return subprocess.run(["bash", _INSTALLER], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _settings(self, home):
        with open(os.path.join(home, ".claude", "settings.json"),
                  "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _seed(self, home):
        os.makedirs(os.path.join(home, ".claude"))
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


if __name__ == "__main__":
    unittest.main()
