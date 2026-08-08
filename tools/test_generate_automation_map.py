#!/usr/bin/env python3
"""Tests for generate-automation-map.py -- stdlib unittest.

All fixtures are SYNTHETIC: a throwaway HOME tree (fake LaunchAgents plists,
fake ~/.codex/automations, fake ~/.claude/settings.json + skills) and a
throwaway repo root with tools/configs/. The real HOME is NEVER read or
written; launchctl is stubbed via the runner injection point. The generator
filename has hyphens, so it is loaded via importlib. Run with:

    python3 -m unittest test_generate_automation_map -v
"""

import importlib.util
import json
import os
import plistlib
import shutil
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_gen():
    spec = importlib.util.spec_from_file_location(
        "automation_map", os.path.join(_HERE, "generate-automation-map.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


am = _load_gen()

LAUNCHCTL_OUT = (
    "PID\tStatus\tLabel\n"
    "123\t0\tcom.anthony.daemon\n"
    "-\t0\tcom.anthonyflores.fully-aware.boot-pack\n"
    "-\t1\tcom.anthony.broken\n")


def _fake_runner(argv):
    assert argv == ["launchctl", "list"]
    return LAUNCHCTL_OUT


def _failing_runner(argv):
    raise OSError("launchctl unavailable")


class Fixture(unittest.TestCase):
    """Builds a synthetic HOME + repo root once per test."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="am-home-")
        self.repo = tempfile.mkdtemp(prefix="am-repo-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

        la = os.path.join(self.home, "Library", "LaunchAgents")
        os.makedirs(la)
        with open(os.path.join(
                la, "com.anthonyflores.fully-aware.boot-pack.plist"), "wb") as fh:
            plistlib.dump({
                "Label": "com.anthonyflores.fully-aware.boot-pack",
                "ProgramArguments": ["/bin/sh", "morning-pack.sh"],
                "StartCalendarInterval": {"Hour": 5, "Minute": 45},
                "StandardOutPath": "/tmp/out.log"}, fh)
        with open(os.path.join(la, "com.anthony.broken.plist"), "wb") as fh:
            plistlib.dump({"Label": "com.anthony.broken",
                           "ProgramArguments": ["/bin/false"],
                           "StartCalendarInterval": [
                               {"Hour": 9, "Minute": 0},
                               {"Hour": 21, "Minute": 0}]}, fh)
        with open(os.path.join(la, "com.anthony.daemon.plist"), "wb") as fh:
            plistlib.dump({"Label": "com.anthony.daemon", "KeepAlive": True,
                           "ProgramArguments": ["/bin/cat"]}, fh)
        with open(os.path.join(la, "com.anthony.unloaded.plist"), "wb") as fh:
            plistlib.dump({"Label": "com.anthony.unloaded",
                           "ProgramArguments": ["/bin/true"],
                           "RunAtLoad": True}, fh)

        cx = os.path.join(self.home, ".codex", "automations",
                          "iris-client-work-pulse")
        os.makedirs(cx)
        with open(os.path.join(cx, "automation.toml"), "w") as fh:
            fh.write('name = "iris-client-work-pulse"\n'
                     'model = "gpt-5.6-sol"\n'
                     'status = "active"\n'
                     '[schedule]\n'
                     'rrule = "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"\n')

        cl = os.path.join(self.home, ".claude")
        os.makedirs(os.path.join(cl, "skills", "saga"))
        os.makedirs(os.path.join(cl, "skills", "session-log"))
        with open(os.path.join(cl, "settings.json"), "w") as fh:
            json.dump({"hooks": {
                "Stop": [{"hooks": [
                    {"type": "command",
                     "command": "python3 /x/agent_session_pulse.py"}]}],
                "PreToolUse": [{"matcher": "Task|Agent", "hooks": [
                    {"type": "command",
                     "command": "python3 /x/pin_guard.py"}]}],
            }}, fh)

        cfg = os.path.join(self.repo, "tools", "configs")
        os.makedirs(cfg)
        for env in ("atlas", "imprint", "ratification-backlog", "seed-manifest"):
            with open(os.path.join(cfg, env + ".json"), "w") as fh:
                json.dump({"environment": env, "repo": "~/code/" + env}, fh)

        # One artifact that exists (boot pack), the rest missing.
        state = os.path.join(self.home, "code", "fully-aware", "state")
        os.makedirs(state)
        with open(os.path.join(state, "BOOT-PACK.md"), "w") as fh:
            fh.write("# pack\n")

    def build(self, runner=_fake_runner):
        return am.build_map(self.home, self.repo, runner=runner)


class TestProbes(Fixture):

    def test_launchagent_states(self):
        nodes = {n["id"]: n for n in self.build()["nodes"]}
        boot = nodes["la:com.anthonyflores.fully-aware.boot-pack"]
        self.assertEqual(boot["status"], "ok")           # loaded, exit 0
        self.assertEqual(boot["subtitle"], "daily 05:45")
        self.assertEqual(nodes["la:com.anthony.broken"]["status"], "failing")
        self.assertEqual(nodes["la:com.anthony.broken"]["subtitle"],
                         "daily 09:00 + 21:00")
        daemon = nodes["la:com.anthony.daemon"]
        self.assertEqual(daemon["status"], "ok")          # running pid 123
        self.assertEqual(daemon["subtitle"], "keep-alive daemon")
        self.assertEqual(nodes["la:com.anthony.unloaded"]["status"], "unarmed")

    def test_launchctl_failure_degrades_not_raises(self):
        nodes = {n["id"]: n for n in self.build(_failing_runner)["nodes"]}
        boot = nodes["la:com.anthonyflores.fully-aware.boot-pack"]
        self.assertEqual(boot["status"], "unknown")
        self.assertIn("unavailable", boot["detail"]["status_note"])

    def test_codex_automation_parsed_and_gated(self):
        nodes = {n["id"]: n for n in self.build()["nodes"]}
        cx = nodes["cx:iris-client-work-pulse"]
        self.assertEqual(cx["detail"]["model"], "gpt-5.6-sol")
        self.assertIn("FREQ=DAILY", cx["subtitle"])
        self.assertFalse(cx["human_gated"])   # pulse is autonomous
        # HUMAN_GATED membership drives the flag:
        self.assertIn("cx:launchpad-autofeed", am.HUMAN_GATED)

    def test_hooks_grouped_by_script_with_matchers(self):
        nodes = {n["id"]: n for n in self.build()["nodes"]}
        pin = nodes["hook:pin_guard.py"]
        self.assertEqual(pin["subtitle"], "PreToolUse:Task|Agent")
        self.assertIn("hook:agent_session_pulse.py", nodes)

    def test_environments_skip_meta_configs(self):
        ids = {n["id"] for n in self.build()["nodes"]}
        self.assertIn("env:atlas", ids)
        self.assertIn("env:imprint", ids)
        self.assertNotIn("env:ratification-backlog", ids)
        self.assertNotIn("env:seed-manifest", ids)

    def test_artifact_existence_probed(self):
        nodes = {n["id"]: n for n in self.build()["nodes"]}
        self.assertEqual(nodes["art:boot-pack"]["status"], "ok")
        self.assertIn("size_bytes", nodes["art:boot-pack"]["detail"])
        missing = nodes["art:iris-events"]
        self.assertEqual(missing["status"], "failing")
        self.assertTrue(missing["degraded"])
        self.assertEqual(missing["reason"], "missing_path")

    def test_skills_summary_node(self):
        nodes = {n["id"]: n for n in self.build()["nodes"]}
        sk = nodes["skills:all"]
        self.assertEqual(sk["detail"]["skills"], ["saga", "session-log"])
        self.assertIn("2 installed", sk["title"])

    def test_third_party_agents_filtered_by_default(self):
        la = os.path.join(self.home, "Library", "LaunchAgents")
        with open(os.path.join(la, "com.adobe.GC.Scheduler.plist"), "wb") as fh:
            plistlib.dump({"Label": "com.adobe.GC.Scheduler",
                           "ProgramArguments": ["/bin/true"]}, fh)
        ids = {n["id"] for n in self.build()["nodes"]}
        self.assertNotIn("la:com.adobe.GC.Scheduler", ids)
        all_ids = {n["id"] for n in am.build_map(
            self.home, self.repo, runner=_fake_runner, all_agents=True)["nodes"]}
        self.assertIn("la:com.adobe.GC.Scheduler", all_ids)

    @unittest.skipUnless(shutil.which("plutil"), "plutil is macOS-only")
    def test_plist_with_double_dash_comment_falls_back_to_plutil(self):
        # launchd tolerates `--` inside XML comments; expat rejects it. The
        # real fully-aware plists hit exactly this.
        la = os.path.join(self.home, "Library", "LaunchAgents")
        path = os.path.join(la, "com.anthony.commented.plist")
        with open(path, "w") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
                     ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                     '<!-- regenerate surfaces -- then assemble -->\n'
                     '<plist version="1.0"><dict>'
                     '<key>Label</key><string>com.anthony.commented</string>'
                     '<key>ProgramArguments</key>'
                     '<array><string>/bin/true</string></array>'
                     '<key>StartInterval</key><integer>60</integer>'
                     '</dict></plist>\n')
        nodes = {n["id"]: n for n in self.build()["nodes"]}
        node = nodes["la:com.anthony.commented"]
        self.assertNotIn("degraded", node)
        self.assertEqual(node["subtitle"], "every 60s")

    def test_script_name_ignores_trailing_args(self):
        self.assertEqual(
            am._script_name("/x/node /y/review-artifacts-open.mjs collect"),
            "review-artifacts-open.mjs")
        self.assertEqual(am._script_name("python3 /x/pin_guard.py"),
                         "pin_guard.py")
        self.assertEqual(am._script_name("/usr/bin/somebinary"), "somebinary")
        self.assertEqual(am._script_name(""), "?")

    def test_missing_dirs_yield_degraded_placeholders(self):
        empty_home = tempfile.mkdtemp(prefix="am-empty-")
        self.addCleanup(shutil.rmtree, empty_home, ignore_errors=True)
        data = am.build_map(empty_home, self.repo, runner=_failing_runner)
        ids = {n["id"]: n for n in data["nodes"]}
        for placeholder in ("la:none", "cx:none", "hook:none"):
            self.assertIn(placeholder, ids)
            self.assertTrue(ids[placeholder]["degraded"])


class TestAssembly(Fixture):

    def test_edges_only_between_observed_nodes(self):
        data = self.build()
        ids = {n["id"] for n in data["nodes"]}
        for e in data["edges"]:
            self.assertIn(e["from"], ids)
            self.assertIn(e["to"], ids)
        # env:* fan-in expanded to the real envs:
        self.assertIn({"from": "env:atlas", "to": "art:surfaces"}, data["edges"])
        # dead endpoint drops the declared flow (no daily-scan agent in fixture):
        froms = {e["from"] for e in data["edges"]}
        self.assertNotIn("la:com.anthonyflores.fully-aware.daily-scan", froms)

    def test_deterministic_after_carving_generated_at(self):
        a, b = self.build(), self.build()
        a["generated_at"] = b["generated_at"] = "CARVED"
        # mtime of the fixture artifact is stable within a test run.
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))

    def test_schema_and_lanes(self):
        data = self.build()
        self.assertEqual(data["schema"], "automation-map/v1")
        self.assertEqual([l["key"] for l in data["lanes"]],
                         ["hooks", "launchd", "codex", "artifacts", "envs"])


class TestHtml(Fixture):

    def test_html_selfcontained_with_nodes_and_data(self):
        data = self.build()
        page = am.render_html(data)
        self.assertIn("<!DOCTYPE html>", page)
        self.assertIn("com.anthonyflores.fully-aware.boot-pack", page)
        self.assertIn("iris-client-work-pulse", page)
        self.assertNotIn("src=", page.split("<script")[0])  # no external assets
        embedded = page.split("id='map-data'>")[1].split("</script>")[0]
        self.assertEqual(json.loads(embedded.replace("<\\/", "</"))["schema"],
                         "automation-map/v1")

    def test_html_escapes_titles(self):
        # A hostile title must be escaped in the card MARKUP. It may appear
        # verbatim inside the application/json data block (inert there; the
        # panel renders via textContent and </ is escaped for the block).
        data = self.build()
        data["nodes"][0]["title"] = "<img src=x onerror=alert(1)>"
        page = am.render_html(data)
        markup = page.split("id='map-data'>")[0]
        self.assertNotIn("<img src=x", markup)
        self.assertIn("&lt;img src=x", markup)


if __name__ == "__main__":
    unittest.main()
