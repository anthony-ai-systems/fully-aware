#!/usr/bin/env python3
"""Tests for generate-automation-map.py -- stdlib unittest.

All fixtures are SYNTHETIC: a throwaway HOME tree (fake LaunchAgents plists,
fake ~/.codex/automations, fake ~/.claude/settings.json + skills) and a
throwaway repo root with tools/configs/. The real HOME is NEVER read or
written; launchctl is stubbed via the runner injection point. The generator
filename has hyphens, so it is loaded via importlib. Run with:

    python3 -m unittest test_generate_automation_map -v
"""

import datetime
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
    "-\t1\tcom.anthony.broken\n"
    "234\t0\tcom.saga.mission-control\n"
    "-\t0\tcom.apple.dock.extra\n"               # OS furniture: never counted
    "-\t0\tapplication.com.apple.Safari.1.2\n"   # GUI-app service job: ditto
    "789\t0\tcom.anthony.granola-ingest\n"       # prefixed but plist-less
    "456\t0\tcom.google.keystone.agent\n")       # loaded, off-map -> named

NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.timezone.utc)


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
        with open(os.path.join(la, "com.saga.mission-control.plist"), "wb") as fh:
            plistlib.dump({"Label": "com.saga.mission-control",
                           "ProgramArguments": ["/bin/cat"],
                           "KeepAlive": True}, fh)

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
                     "command": "python3 /x/agent_session_pulse.py"},
                    {"type": "command",
                     "command": "/venv/bin/python /imprint/hooks/stop_capture.py"}]}],
                "UserPromptSubmit": [{"hooks": [
                    {"type": "command",
                     "command": "/venv/bin/python "
                                "/imprint/hooks/user_prompt_submit.py"}]}],
                "PreToolUse": [{"matcher": "Task|Agent", "hooks": [
                    {"type": "command",
                     "command": "python3 /x/pin_guard.py"}]}],
            }}, fh)

        cfg = os.path.join(self.repo, "tools", "configs")
        os.makedirs(cfg)
        # surface-config/v1 spells the repo key "repo_path"...
        with open(os.path.join(cfg, "atlas.json"), "w") as fh:
            json.dump({"schema": "surface-config/v1", "environment": "atlas",
                       "repo_path": "/Users/x/code/atlas"}, fh)
        # ...legacy spellings and a config with neither still degrade sanely.
        with open(os.path.join(cfg, "imprint.json"), "w") as fh:
            json.dump({"environment": "imprint", "repo": "~/code/imprint"}, fh)
        with open(os.path.join(cfg, "keyless.json"), "w") as fh:
            json.dump({"environment": "keyless"}, fh)
        for meta in ("ratification-backlog", "seed-manifest"):
            with open(os.path.join(cfg, meta + ".json"), "w") as fh:
                json.dump({"environment": meta}, fh)

        # One artifact that exists (boot pack), the rest missing.
        state = os.path.join(self.home, "code", "fully-aware", "state")
        os.makedirs(state)
        with open(os.path.join(state, "BOOT-PACK.md"), "w") as fh:
            fh.write("# pack\n")

    def build(self, runner=_fake_runner, now=NOW):
        # A frozen clock: generated_at now travels inside pid claims too.
        return am.build_map(self.home, self.repo, runner=runner, now=now)


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

    def test_running_pid_claim_is_timestamped(self):
        # A bare "running (pid N)" goes stale the moment the daemon restarts;
        # the claim must carry the clock it was true at.
        nodes = {n["id"]: n for n in self.build()["nodes"]}
        self.assertEqual(nodes["la:com.anthony.daemon"]["detail"]["status_note"],
                         "running as of 2026-08-08T12:00:00+00:00 (pid 123)")

    def test_saga_prefix_is_shown(self):
        nodes = {n["id"]: n for n in self.build()["nodes"]}
        self.assertEqual(nodes["la:com.saga.mission-control"]["status"], "ok")

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

    def test_environment_subtitle_reads_repo_path_key(self):
        nodes = {n["id"]: n for n in self.build()["nodes"]}
        # surface-config/v1 key wins...
        self.assertEqual(nodes["env:atlas"]["subtitle"], "/Users/x/code/atlas")
        # ...legacy "repo" still resolves...
        self.assertEqual(nodes["env:imprint"]["subtitle"], "~/code/imprint")
        # ...and only a config with no path at all falls back to the label.
        self.assertEqual(nodes["env:keyless"]["subtitle"], "surface config")

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

    def test_launchd_exclusions_are_counted_and_named(self):
        exc = self.build()["launchd_excluded"]
        # Off-map because of the prefix filter (keystone) or because nothing in
        # ~/Library/LaunchAgents backs it (granola-ingest) -- both must be named.
        # Apple and GUI-app service jobs are furniture, not hidden automation.
        self.assertEqual(exc["labels"], ["com.anthony.granola-ingest",
                                         "com.google.keystone.agent"])
        self.assertEqual(exc["count"], 2)
        self.assertNotIn("com.apple", json.dumps(exc))
        self.assertNotIn("application.", json.dumps(exc))

    def test_all_launchagents_does_not_report_rendered_jobs_as_excluded(self):
        la = os.path.join(self.home, "Library", "LaunchAgents")
        with open(os.path.join(la, "com.google.keystone.agent.plist"), "wb") as fh:
            plistlib.dump({"Label": "com.google.keystone.agent",
                           "ProgramArguments": ["/bin/true"]}, fh)
        data = am.build_map(self.home, self.repo, runner=_fake_runner,
                            now=NOW, all_agents=True)
        self.assertIn("la:com.google.keystone.agent",
                      {n["id"] for n in data["nodes"]})
        # keystone is on the map now; granola-ingest still has no plist at all.
        self.assertEqual(data["launchd_excluded"],
                         {"count": 1, "labels": ["com.anthony.granola-ingest"]})

    def test_launchctl_failure_degrades_the_exclusion_field(self):
        exc = self.build(_failing_runner)["launchd_excluded"]
        self.assertTrue(exc["degraded"])
        self.assertIn("launchctl_failed", exc["reason"])
        self.assertEqual(exc["count"], 0)

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

    def test_imprint_topology_follows_the_real_data_flow(self):
        data = self.build()
        edges = {(e["from"], e["to"]) for e in data["edges"]}
        boot = "la:com.anthonyflores.fully-aware.boot-pack"
        # hooks write the live db; the 05:45 agent exports it; digest reads it.
        for edge in (("hook:stop_capture.py", "art:imprint-db"),
                     ("hook:user_prompt_submit.py", "art:imprint-db"),
                     ("art:imprint-db", boot),
                     (boot, "art:imprint-store"),
                     ("art:imprint-store", "art:boot-digest")):
            self.assertIn(edge, edges)
        # the hooks never touch the export, and the export never feeds the agent
        self.assertNotIn(("hook:stop_capture.py", "art:imprint-store"), edges)
        self.assertNotIn(("hook:user_prompt_submit.py", "art:imprint-store"), edges)
        self.assertNotIn(("art:imprint-store", boot), edges)

    def test_imprint_db_node_probes_the_live_store(self):
        nodes = {n["id"]: n for n in self.build()["nodes"]}
        db = nodes["art:imprint-db"]
        self.assertEqual(db["source"],
                         os.path.join(self.home, ".local", "share", "imprint",
                                      "anthony", "imprint.db"))
        self.assertEqual(db["status"], "failing")     # absent in the fixture
        self.assertEqual(db["reason"], "missing_path")

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

    def test_html_footnotes_the_launchd_exclusions(self):
        markup = am.render_html(self.build()).split("id='map-data'>")[0]
        self.assertIn("2 loaded jobs not shown", markup)
        self.assertIn("com.google.keystone.agent", markup)   # title attribute

    def test_html_footnote_states_when_nothing_is_hidden(self):
        data = self.build()
        data["launchd_excluded"] = {"count": 0, "labels": []}
        markup = am.render_html(data).split("id='map-data'>")[0]
        self.assertIn("every loaded job outside Apple", markup)

    def test_html_footnote_reports_a_degraded_probe(self):
        markup = am.render_html(self.build(_failing_runner)
                                ).split("id='map-data'>")[0]
        self.assertIn("launchd exclusions unknown", markup)

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
