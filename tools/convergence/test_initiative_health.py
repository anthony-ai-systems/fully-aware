import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import initiative_health as health

NOW = dt.datetime(2026, 9, 25, 8, tzinfo=dt.timezone.utc)
OWNER = "01a08366-bd65-72a3-b7a8-ae0e5ab5bb20"
WORK = "work-79d5d0c44486336a47076655"


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


class Fixture:
    """A synthetic home with every input the module reads; nothing live."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config = health.default_config(self.root)
        self.config["hold_receipts"] = [str(self.root / "hold.json")]

    def path(self, key):
        return Path(self.config[key])

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        path.write_bytes(data)
        return data

    def automations(self, rows, columns=("id", "status", "kind", "rrule", "last_run_at", "next_run_at")):
        db = self.path("codex_dev_db")
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE automations (%s)" % ", ".join(columns))
        for row in rows:
            con.execute("INSERT INTO automations (%s) VALUES (%s)" % (", ".join(row), ", ".join("?" * len(row))),
                        tuple(row.values()))
        con.commit(); con.close()
        self.path("codex_automations_dir").mkdir(parents=True, exist_ok=True)

    def config_file(self, automation_id, status="ACTIVE"):
        target = self.path("codex_automations_dir") / automation_id / "automation.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('version = 1\nid = "%s"\nkind = "heartbeat"\nstatus = "%s"\n'
                          'rrule = "FREQ=DAILY;BYHOUR=9,13,17"\ntarget_thread_id = "%s"\n'
                          'prompt = "SECRET_PRIVATE_PROMPT"\n' % (automation_id, status, OWNER))

    def thread(self, archived):
        db = self.path("codex_state_db")
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE threads (id TEXT, archived INTEGER, archived_at INTEGER, title TEXT)")
        con.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (OWNER, 1 if archived else 0, 1790179692, "SECRET"))
        con.commit(); con.close()

    def hold(self):
        self.write(self.root / "hold.json", {
            "schema": "iris-platform-block-hold/v1", "recorded_at": "2026-09-23T16:06:55.793Z",
            "automation_id": health.IRIS_AUTOMATION, "target_thread_id": OWNER,
            "before_status": "ACTIVE", "after_status": "PAUSED",
            "resume_condition": "A supported platform recovery is confirmed for the original task."})

    def sweep(self, ended, attempt_status=None, attempt_trigger=None):
        runs = self.path("sweep_runs_dir")
        run_id = ended.strftime("%Y%m%dT%H%M%SZ") + "-abcd1234"
        outcome = runs / run_id / "outcome.json"
        self.write(outcome, {"schema": "iris-sweep-outcome/v1", "run_id": run_id,
                             "trigger_at": iso(ended - dt.timedelta(minutes=12)),
                             "started_at": iso(ended - dt.timedelta(minutes=11)),
                             "ended_at": iso(ended), "status": "complete"})
        context = {"schema": "next-session/v2", "latest_sweep": {"outcome": "sweep-runs/%s/outcome.json" % run_id}}
        if attempt_status:
            late_id = attempt_trigger.strftime("%Y%m%dT%H%M%SZ") + "-late-ffff0000"
            raw = self.write(runs / late_id / "attempt.json", {
                "schema": "iris-sweep-attempt/v1", "automation_id": health.IRIS_AUTOMATION,
                "owner_thread_id": OWNER, "run_id": late_id, "trigger_at": iso(attempt_trigger),
                "closed_at": iso(attempt_trigger + dt.timedelta(minutes=100)),
                "recorded_at": iso(attempt_trigger + dt.timedelta(minutes=101)), "status": attempt_status})
            context["latest_sweep_attempt"] = {"path": str(runs / late_id / "attempt.json"),
                                               "sha256": hashlib.sha256(raw).hexdigest()}
        self.write(self.path("iris_next_session"), context)

    def docket(self, events):
        self.write(self.path("docket"), {"schema": "iris-initiative-docket/v1", "generation": len(events),
                                         "events": events})

    def report(self):
        return health.assess(health.collect(self.config, NOW), NOW)


def source(verified_at, terminal=False):
    return {"work_item_id": WORK, "work_revision": "a" * 64, "verified_at": verified_at,
            "reference": {"kind": "notion_read", "id": "n1"}, "terminal": terminal}


def answered_docket(next_check):
    key = health.action_key(WORK, "dependency_decision")
    return key, [
        {"sequence": 1, "at": "2026-09-20T16:09:00Z", "command": "propose",
         "input": {"event_id": "e1", "action_class": "dependency_decision", "source": source("2026-09-20T16:08:00Z"),
                   "packet": {"question": "PRIVATE QUESTION"}, "next_check_at": next_check}},
        {"sequence": 2, "at": "2026-09-20T16:10:00Z", "command": "present",
         "input": {"event_id": "e2", "action_key": key, "proposal_revision": "b" * 64, "attempt_id": "a1",
                   "outcome": "attempted", "reference": None, "source": source("2026-09-20T16:09:30Z")}},
        {"sequence": 3, "at": "2026-09-21T00:13:51Z", "command": "respond",
         "input": {"event_id": "e3", "action_key": key, "proposal_revision": "b" * 64, "disposition": "answered",
                   "option_id": "keep_htl", "response_ref": {"thread_id": OWNER, "message_id": "m"},
                   "source": source("2026-09-21T00:13:00Z")}},
    ]


class InitiativeHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(self.tmp.name)

    def stopped(self):
        # Scheduler store readable but without the IRIS row; config dir has no IRIS entry.
        self.f.automations([{"id": health.RADAR_AUTOMATION, "status": "ACTIVE", "kind": "heartbeat",
                             "rrule": "FREQ=DAILY", "last_run_at": 1790269329000, "next_run_at": 1790352000000}])
        self.f.sweep(NOW - dt.timedelta(hours=60), "missed_before_start", NOW - dt.timedelta(hours=56))
        self.f.hold(); self.f.thread(archived=True)

    def test_stopped_loop_is_loud_not_a_quiet_day(self):
        self.stopped()
        report = self.f.report()
        self.assertEqual(report["schema"], "fa-initiative-health/v1")
        self.assertEqual(report["state"], "stopped")
        self.assertFalse(report["idle"]["legitimate"])
        self.assertEqual(report["idle"]["next_wake"], {"kind": "none", "at": None})
        self.assertIn("Anthony must decide how IRIS gets a heartbeat", report["required_decision"])
        self.assertIs(report["hold"]["resume_condition_satisfiable"], False)
        self.assertEqual(report["hold"]["reason"], "hold_target_archived")
        self.assertIn("hold_automation_absent", report["hold"]["reasons"])
        iris = report["drivers"][0]
        self.assertEqual((iris["driver"], iris["present"], iris["status"]), ("iris_sweep_heartbeat", False, "ABSENT"))
        radar = report["drivers"][2]
        self.assertTrue(radar["present"]); self.assertEqual(radar["next_run_at"], "2026-09-25T16:00:00Z")
        self.assertEqual(report["missed_attempts"][0]["status"], "missed_before_start")
        self.assertTrue(report["limits"])

    def test_stopped_with_no_automation_dir_at_all(self):
        self.stopped()
        self.f.path("codex_automations_dir").rmdir()
        report = self.f.report()
        self.assertEqual(report["state"], "stopped")
        self.assertIn("config_dir:missing", report["drivers"][0]["evidence"])

    def test_operating(self):
        next_run = NOW + dt.timedelta(hours=1)
        self.f.automations([{"id": health.IRIS_AUTOMATION, "status": "ACTIVE", "kind": "heartbeat",
                             "rrule": "FREQ=DAILY", "last_run_at": iso(NOW - dt.timedelta(hours=3)),
                             "next_run_at": iso(next_run)}])
        self.f.config_file(health.IRIS_AUTOMATION)
        self.f.sweep(NOW - dt.timedelta(hours=3))
        report = self.f.report()
        self.assertEqual(report["state"], "operating")
        self.assertTrue(report["idle"]["legitimate"])
        self.assertEqual(report["idle"]["next_wake"], {"kind": "iris_sweep_heartbeat", "at": iso(next_run)})
        self.assertIsNone(report["required_decision"])
        self.assertEqual(report["hold"]["declared"], False)
        self.assertNotIn("SECRET", json.dumps(report))

    def test_degraded_when_driver_present_but_success_stale(self):
        self.f.automations([{"id": health.IRIS_AUTOMATION, "status": "ACTIVE", "kind": "heartbeat",
                             "rrule": "FREQ=DAILY", "last_run_at": None, "next_run_at": None}])
        self.f.sweep(NOW - dt.timedelta(hours=40))
        report = self.f.report()
        self.assertEqual((report["state"], report["reason"]), ("degraded", "last_success_stale"))
        self.assertFalse(report["idle"]["legitimate"])
        self.assertIsNone(report["required_decision"])

    def test_degraded_on_missed_attempt_after_recent_success(self):
        self.f.automations([{"id": health.IRIS_AUTOMATION, "status": "ACTIVE", "kind": "heartbeat",
                             "rrule": "FREQ=DAILY", "last_run_at": None, "next_run_at": None}])
        self.f.sweep(NOW - dt.timedelta(hours=10), "missed_before_start", NOW - dt.timedelta(hours=5))
        self.assertEqual(self.f.report()["reason"], "missed_attempts")

    def test_paused_driver_is_degraded(self):
        self.f.automations([{"id": health.IRIS_AUTOMATION, "status": "PAUSED", "kind": "heartbeat",
                             "rrule": "FREQ=DAILY", "last_run_at": None, "next_run_at": None}])
        self.f.sweep(NOW - dt.timedelta(hours=2))
        self.assertEqual(self.f.report()["reason"], "driver_not_active")

    def test_config_file_without_scheduler_row_is_not_a_driver(self):
        # Readable store with only Radar's row; IRIS has an automation.toml but no row.
        self.f.automations([{"id": health.RADAR_AUTOMATION, "status": "ACTIVE", "kind": "heartbeat",
                             "rrule": "FREQ=DAILY", "last_run_at": None, "next_run_at": None}])
        self.f.config_file(health.IRIS_AUTOMATION)
        self.f.sweep(NOW - dt.timedelta(hours=2))
        report = self.f.report()
        self.assertEqual((report["state"], report["reason"]), ("degraded", "scheduler_row_missing"))
        iris = report["drivers"][0]
        self.assertEqual((iris["present"], iris["presence"], iris["status"]),
                         (False, "present_in_config_only", "present_in_config_only"))
        self.assertFalse(report["idle"]["legitimate"])
        self.assertIn("scheduler has no row", report["required_decision"])
        self.assertEqual(self.run_cli("--check")[0], 1)

    def test_paused_driver_with_future_next_run_is_not_legitimate_idle(self):
        self.f.automations([{"id": health.IRIS_AUTOMATION, "status": "PAUSED", "kind": "heartbeat",
                             "rrule": "FREQ=DAILY", "last_run_at": None,
                             "next_run_at": iso(NOW + dt.timedelta(hours=1))}])
        self.f.sweep(NOW - dt.timedelta(hours=2))
        report = self.f.report()
        self.assertEqual((report["state"], report["reason"]), ("degraded", "driver_not_active"))
        self.assertFalse(report["idle"]["legitimate"])
        self.assertEqual(report["idle"]["next_wake"], {"kind": "none", "at": None})

    def test_null_scheduler_status_is_unknown_status_whatever_the_config_says(self):
        self.f.automations([{"id": health.IRIS_AUTOMATION, "status": None, "kind": "heartbeat",
                             "rrule": "FREQ=DAILY", "last_run_at": None,
                             "next_run_at": iso(NOW + dt.timedelta(hours=1))}])
        self.f.config_file(health.IRIS_AUTOMATION, status="ACTIVE")
        self.f.sweep(NOW - dt.timedelta(hours=2))
        report = self.f.report()
        self.assertEqual((report["state"], report["reason"]), ("degraded", "driver_status_unknown"))
        iris = report["drivers"][0]
        self.assertEqual((iris["present"], iris["presence"], iris["status"]), (True, "scheduler_row", "unknown_status"))
        self.assertFalse(report["idle"]["legitimate"])
        self.assertEqual(report["idle"]["next_wake"], {"kind": "none", "at": None})
        self.assertEqual(self.run_cli("--check")[0], 1)

    def test_status_column_missing_is_unknown_status(self):
        self.f.automations([{"id": health.IRIS_AUTOMATION, "kind": "heartbeat", "rrule": "FREQ=DAILY",
                             "next_run_at": iso(NOW + dt.timedelta(hours=1))}],
                           columns=("id", "kind", "rrule", "next_run_at"))
        self.f.config_file(health.IRIS_AUTOMATION, status="ACTIVE")
        self.f.sweep(NOW - dt.timedelta(hours=2))
        report = self.f.report()
        self.assertEqual((report["state"], report["drivers"][0]["status"]), ("degraded", "unknown_status"))
        self.assertFalse(report["idle"]["legitimate"])

    def test_scheduler_status_wins_over_config_status(self):
        self.f.automations([{"id": health.IRIS_AUTOMATION, "status": "PAUSED", "kind": "heartbeat",
                             "rrule": "FREQ=DAILY", "last_run_at": None,
                             "next_run_at": iso(NOW + dt.timedelta(hours=1))}])
        self.f.config_file(health.IRIS_AUTOMATION, status="ACTIVE")
        self.f.sweep(NOW - dt.timedelta(hours=2))
        report = self.f.report()
        self.assertEqual((report["reason"], report["drivers"][0]["status"]), ("driver_not_active", "PAUSED"))
        self.assertFalse(report["idle"]["legitimate"])

    def test_never_succeeded_says_since_unknown(self):
        self.f.automations([{"id": health.RADAR_AUTOMATION, "status": "ACTIVE", "kind": "heartbeat",
                             "rrule": "FREQ=DAILY", "last_run_at": None, "next_run_at": None}])
        self.f.path("sweep_runs_dir").mkdir(parents=True)
        report = self.f.report()
        self.assertEqual(report["state"], "stopped")
        self.assertIsNone(report["since"])
        first = health.render_markdown(report).splitlines()[0]
        self.assertTrue(first.startswith("Initiative: STOPPED since unknown (no successful sweep observed) — "), first)
        self.assertNotIn(iso(NOW), first)

    def test_unreadable_sqlite_is_unknown_not_operating(self):
        self.f.config_file(health.IRIS_AUTOMATION)
        self.f.sweep(NOW - dt.timedelta(hours=1))
        self.f.write(self.f.path("codex_dev_db"), b"this is not a sqlite database" * 20)
        report = self.f.report()
        self.assertEqual(report["state"], "unknown")
        self.assertEqual(report["reason"], "scheduler_store_unavailable")
        self.assertFalse(report["idle"]["legitimate"])
        self.assertIn("scheduler_db:unreadable", report["drivers"][0]["evidence"])

    def test_sweep_pointer_shapes(self):
        runs = self.f.path("sweep_runs_dir")
        self.f.sweep(NOW - dt.timedelta(hours=2))
        run_id = next(runs.iterdir()).name
        absolute = str(runs / run_id / "outcome.json")
        for pointer in (absolute, {"path": absolute}, {"outcome": {"path": absolute}}, run_id,
                        "sweep-runs/%s/outcome.json" % run_id):
            path, _ = health.resolve_pointer(pointer, str(self.f.path("iris_next_session")), str(runs), "outcome.json")
            self.assertEqual(path, Path(absolute), pointer)
        self.assertEqual(health.resolve_pointer({"other": 1}, "x", str(runs), "outcome.json"), (None, None))

    def test_missing_everything_is_unknown(self):
        report = self.f.report()
        self.assertEqual(report["state"], "unknown")
        self.assertEqual(report["docket"]["availability"], "unavailable")
        self.assertEqual(report["worker"], {"availability": "unavailable", "reason": "missing"})

    def test_answered_without_follow_through_listed_with_age_and_overdue_recheck(self):
        self.stopped()
        key, events = answered_docket("2026-09-21T17:40:00Z")
        self.f.docket(events)
        report = self.f.report()
        self.assertEqual(len(report["follow_through_gaps"]), 1)
        gap = report["follow_through_gaps"][0]
        self.assertEqual((gap["action_key"], gap["work_item_id"]), (key, WORK))
        self.assertEqual(gap["age_hours"], round((NOW - dt.datetime(2026, 9, 21, 0, 13, 51, tzinfo=dt.timezone.utc))
                                                 .total_seconds() / 3600, 1))
        self.assertTrue(gap["recheck_overdue"])
        self.assertEqual(report["overdue_rechecks"], 1)
        self.assertEqual(report["open_requests"], 0)
        self.assertEqual(report["docket"]["label"], "event-level summary (not IRIS replay)")
        self.assertNotIn("PRIVATE QUESTION", json.dumps(report))

    def test_follow_through_closes_gap_and_future_recheck_not_overdue(self):
        self.stopped()
        key, events = answered_docket("2026-10-10T00:00:00Z")
        self.f.docket(events)
        self.assertEqual(self.f.report()["overdue_rechecks"], 0)
        events.append({"sequence": 4, "at": "2026-09-22T00:00:00Z", "command": "follow-through",
                       "input": {"event_id": "e4", "action_key": key, "proposal_revision": "b" * 64,
                                 "reference": {"kind": "artifact", "id": "x"}, "outcome": "recorded",
                                 "source": source("2026-09-21T23:59:00Z")}})
        self.f.docket(events)
        self.assertEqual(self.f.report()["follow_through_gaps"], [])

    def test_unanswered_proposal_counts_as_open(self):
        self.stopped()
        _, events = answered_docket("2026-10-10T00:00:00Z")
        self.f.docket(events[:2])
        report = self.f.report()
        self.assertEqual((report["open_requests"], report["follow_through_gaps"]), (1, []))

    def test_automation_table_schema_drift_tolerated(self):
        self.f.automations([{"id": health.IRIS_AUTOMATION, "status": "active", "surprise": "x",
                             "nextRunAtMs": 1790352000000}], columns=("id", "status", "surprise", "nextRunAtMs"))
        self.f.sweep(NOW - dt.timedelta(hours=1))
        report = self.f.report()
        iris = report["drivers"][0]
        self.assertEqual((iris["present"], iris["status"]), (True, "ACTIVE"))
        self.assertIsNone(iris["next_run_at"])
        self.assertEqual(report["state"], "operating")
        self.assertFalse(report["idle"]["legitimate"])

    def test_automation_table_without_id_column_is_unknown(self):
        self.f.automations([{"name": "x"}], columns=("name",))
        self.f.sweep(NOW - dt.timedelta(hours=1))
        report = self.f.report()
        self.assertEqual(report["state"], "unknown")
        self.assertIn("scheduler_db:schema_drift_no_id", report["drivers"][0]["evidence"])

    def test_worker_counts_and_lane_share(self):
        self.stopped()
        base = self.f.path("local_worker_dir")
        for index, (lane, state, days) in enumerate([("iris", "accepted", 1), ("iris", "failed", 2),
                                                     ("fully-aware-convergence", "accepted", 3), ("iris", "accepted", 9)]):
            started = iso(NOW - dt.timedelta(days=days))
            self.f.write(base / ("run-%d" % index) / "record.json",
                         {"schema": "clayton-local-receipt/v1", "state": state, "started_at": started, "updated_at": started})
            self.f.write(base / ("run-%d" % index) / "request.json", {"lane": lane})
        worker = self.f.report()["worker"]
        self.assertEqual((worker["runs_7d"], worker["accepted_7d"]), (3, 2))
        self.assertEqual(worker["lane_counts_7d"], {"fully-aware-convergence": 1, "iris": 2})
        self.assertEqual(worker["newest_run_at"], iso(NOW - dt.timedelta(days=1)))

    def test_markdown_first_line_and_length(self):
        self.stopped()
        _, events = answered_docket("2026-09-21T17:40:00Z")
        self.f.docket(events)
        text = health.render_markdown(self.f.report())
        lines = text.splitlines()
        self.assertRegex(lines[0], r"^Initiative: STOPPED since \S+ — .+")
        self.assertTrue(10 <= len(lines) <= 20, len(lines))
        self.assertIn("Next wake: none", text)
        self.assertIn("| iris_sweep_heartbeat | no | ABSENT |", text)

    def run_cli(self, *args):
        config = Path(self.tmp.name) / "config.json"
        config.write_text(json.dumps(self.f.config))
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = health.main(["--config", str(config), "--now", iso(NOW)] + list(args))
        return code, out.getvalue()

    def test_check_exit_codes(self):
        self.assertEqual(self.run_cli("--check")[0], 3)
        self.stopped()
        code, out = self.run_cli("--check", "--markdown")
        self.assertEqual(code, 1); self.assertTrue(out.startswith("Initiative: STOPPED since "))
        self.assertEqual(self.run_cli("--markdown")[0], 0)

    def test_check_operating_and_degraded(self):
        self.f.automations([{"id": health.IRIS_AUTOMATION, "status": "ACTIVE", "kind": "heartbeat", "rrule": "x",
                             "last_run_at": None, "next_run_at": iso(NOW + dt.timedelta(hours=1))}])
        self.f.sweep(NOW - dt.timedelta(hours=2))
        code, out = self.run_cli("--check", "--json")
        self.assertEqual((code, json.loads(out)["state"]), (0, "operating"))
        shutil.rmtree(self.f.path("sweep_runs_dir"))
        self.f.sweep(NOW - dt.timedelta(hours=30))
        self.assertEqual(self.run_cli("--check")[0], 1)

    def test_hard_error_exits_two(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("[]")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(health.main(["--config", str(bad), "--check"]), 2)
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(health.main(["--set", "not_a_key=x"]), 2)

    def test_collect_is_read_only(self):
        self.stopped()
        _, events = answered_docket("2026-09-21T17:40:00Z")
        self.f.docket(events)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(self.tmp.name).rglob("*") if p.is_file()}
        self.f.report()
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(self.tmp.name).rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
