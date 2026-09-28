"""An unfinished observation is neither a completed sweep nor a missed deadline."""
import datetime as dt
import hashlib
import json
import tempfile
import unittest

import initiative_health as health
from test_initiative_health import Fixture, NOW, OWNER, iso


class PendingAttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(self.tmp.name)
        self.f.automations([{"id": health.IRIS_AUTOMATION, "status": "ACTIVE",
                            "next_run_at": iso(NOW + dt.timedelta(hours=1))}])
        self.last = NOW - dt.timedelta(hours=1)
        self.f.sweep(self.last)

    def unfinished(self, age):
        start = NOW - dt.timedelta(seconds=age)
        run = self.f.path("sweep_runs_dir") / (start.strftime("%Y%m%dT%H%M%SZ") + "-pending")
        run.mkdir(parents=True, exist_ok=True)
        return run, start

    def test_pending_window_never_implies_running_or_new_success(self):
        run, start = self.unfinished(300)
        report = self.f.report()
        self.assertEqual(report["state"], "operating")
        self.assertEqual(report["missed_attempts"], [])
        self.assertEqual(report["drivers"][0]["last_success_at"], iso(self.last))
        self.assertEqual(report["pending_attempt"], {
            "run_id": run.name, "status": "awaiting_outcome", "trigger_at": iso(start),
            "deadline_at": iso(start + dt.timedelta(seconds=900)),
            "time_basis": "run_directory_timestamp",
            "running_verified": False, "completion": "unverified"})
        self.assertIn("Awaiting outcome:", health.render_markdown(report))
        self.assertNotIn("Missed attempt:", health.render_markdown(report))

    def test_original_deadline_is_not_extended_by_a_read(self):
        run, _ = self.unfinished(899)
        snapshot = health.collect(self.f.config, NOW)
        self.assertIsNotNone(health.assess(snapshot, NOW)["pending_attempt"])
        for later in [NOW + dt.timedelta(seconds=1), NOW + dt.timedelta(hours=2)]:
            with self.subTest(later=later):
                report = health.assess(snapshot, later)
                self.assertIsNone(report["pending_attempt"])
                self.assertEqual(report["reason"], "missed_attempts")
                self.assertEqual(report["missed_attempts"][0]["run_id"], run.name)

    def test_early_terminal_receipt_is_still_missed(self):
        run, start = self.unfinished(120)
        for status in ["missed_before_start", "unknown"]:
            with self.subTest(status=status):
                raw = self.f.write(run / "attempt.json", {
                    "schema": health.ATTEMPT_SCHEMA, "run_id": run.name,
                    "automation_id": health.IRIS_AUTOMATION, "owner_thread_id": OWNER,
                    "trigger_at": iso(start), "closed_at": iso(NOW), "status": status})
                path = self.f.path("iris_next_session")
                context = json.loads(path.read_bytes())
                context["latest_sweep_attempt"] = {"path": str(run / "attempt.json"),
                                                     "sha256": hashlib.sha256(raw).hexdigest()}
                self.f.write(path, context)
                report = self.f.report()
                self.assertIsNone(report["pending_attempt"])
                self.assertEqual(report["reason"], "missed_attempts")
                self.assertEqual(report["missed_attempts"][0]["status"], status)

    def test_failed_or_unreadable_outcome_is_never_pending(self):
        run, _ = self.unfinished(120)
        for body, status in [({"schema": health.OUTCOME_SCHEMA, "run_id": run.name,
                              "ended_at": iso(NOW), "status": "failed"}, "failed"),
                             (b'{"unfinished":', "outcome_unavailable")]:
            with self.subTest(status=status):
                self.f.write(run / "outcome.json", body)
                report = self.f.report()
                self.assertIsNone(report["pending_attempt"])
                self.assertEqual(report["reason"], "missed_attempts")
                self.assertEqual(report["missed_attempts"][0]["status"], status)

    def test_future_or_explicitly_closed_missing_run_is_not_pending(self):
        run, _ = self.unfinished(-60)
        report = self.f.report()
        self.assertIsNone(report["pending_attempt"])
        self.assertEqual(report["reason"], "missed_attempts")
        run.rmdir()
        run, _ = self.unfinished(60)
        self.f.write(run / "late-closure.json", {})
        self.assertIsNone(self.f.report()["pending_attempt"])

    def test_pending_run_does_not_cure_stale_or_absent_success(self):
        run, _ = self.unfinished(120)
        for path in self.f.path("sweep_runs_dir").glob("*/outcome.json"):
            path.unlink()
        self.f.write(self.f.path("iris_next_session"), {"schema": "next-session/v2"})
        report = self.f.report()
        self.assertEqual(report["state"], "degraded")
        self.assertEqual(report["reason"], "last_success_stale")
        self.assertIsNone(report["drivers"][0]["last_success_at"])
        self.assertEqual(report["pending_attempt"]["run_id"], run.name)

    def test_successful_close_removes_pending_without_inventing_acceptance(self):
        run, _ = self.unfinished(120)
        self.f.write(run / "outcome.json", {"schema": health.OUTCOME_SCHEMA,
            "run_id": run.name, "ended_at": iso(NOW), "status": "complete"})
        report = self.f.report()
        self.assertEqual(report["state"], "operating")
        self.assertIsNone(report["pending_attempt"])
        self.assertEqual(report["drivers"][0]["last_success_at"], iso(NOW))
        self.assertIn("not proof of accepted work", " ".join(report["limits"]))

    def test_diagnostic_window_matches_existing_pass_budget(self):
        from sweep_clock import TOTAL
        self.assertEqual(health.SWEEP_PENDING_SECONDS, TOTAL)


if __name__ == "__main__":
    unittest.main()
