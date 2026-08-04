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
with open(os.path.join(root, "assembler-called"), "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("SHIM_ASSEMBLER_RC", "0")))
'''

_SHIM_DIGEST = '''#!/usr/bin/env python3
import os
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
state = os.path.join(root, "state")
os.makedirs(state, exist_ok=True)
with open(os.path.join(state, "BOOT-DIGEST.md"), "a", encoding="utf-8") as fh:
    fh.write("shim digest\\n")
'''

_SHIM_GENERATOR = '''#!/usr/bin/env python3
import sys
sys.exit(0)
'''


class WrapperExecution(unittest.TestCase):
    """morning-pack.sh, actually run, against shimmed tools in a tempdir."""

    def _fixture(self, tmp):
        tools = os.path.join(tmp, "tools")
        os.makedirs(os.path.join(tools, "configs"))
        shutil.copy2(_WRAPPER, os.path.join(tools, "morning-pack.sh"))
        for name, src in (("assemble-boot-pack.py", _SHIM_ASSEMBLER),
                          ("boot-digest.py", _SHIM_DIGEST),
                          ("generate-surface.py", _SHIM_GENERATOR)):
            path = os.path.join(tools, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return os.path.join(tools, "morning-pack.sh")

    def _run(self, tmp, args=(), assembler_rc=0):
        wrapper = self._fixture(tmp)
        env = dict(os.environ)
        env["FULLY_AWARE_PYTHON"] = sys.executable
        env["SHIM_ASSEMBLER_RC"] = str(assembler_rc)
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

    def test_argless_run_writes_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
            self.assertEqual(self._assembler_args(tmp), [""])
            self.assertTrue(os.path.isfile(self._digest(tmp)))

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
        rc, out = self._run("x" * 10000 + "\n")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 4000)
        with open(_HOOK, "r", encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("head -c 4000", body)
        self.assertNotRegex(body, r"(?m)^\s*cat ")

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


if __name__ == "__main__":
    unittest.main()
