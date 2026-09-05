"""Offline recovery contracts: pinned executable, persistent health, no implicit replay."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import taste_distiller as td
import prepare_launch_agent as prepare


class Recovery(unittest.TestCase):
    def test_pinned_executable_under_launchd_path_receives_machine_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake-claude"
            fake.write_text('#!/bin/sh\n[ "$IMPRINT_CAPTURE_ORIGIN" = automation ] || exit 4\nprintf "[]"\n')
            fake.chmod(0o700)
            with patch.dict(os.environ, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "MACROSEAT_CLAUDE_BIN": str(fake)}):
                self.assertEqual(td.call_model("synthetic"), "[]")
                prepared = prepare.render(str(fake))
            self.assertEqual(prepared["EnvironmentVariables"]["MACROSEAT_CLAUDE_BIN"], str(fake))
            self.assertNotIn("/bin/bash", prepared["ProgramArguments"])
            with patch.dict(os.environ, {"MACROSEAT_CLAUDE_BIN": str(fake) + ".missing"}):
                with self.assertRaises(RuntimeError):
                    td._claude_bin()

    def test_all_error_then_empty_batch_stays_degraded(self):
        with tempfile.TemporaryDirectory() as root:
            Path(td.queue_path(root)).write_text(json.dumps({"session_id": "synthetic"}) + "\n")
            with patch.object(td, "process_session", return_value={"status": "error"}):
                for _ in range(td.RETRY_LIMIT):
                    self.assertEqual(td._drain(root), 1)
                self.assertEqual(td._drain(root), 0)
            health = json.loads(Path(root, "distill-health.json").read_text())
            self.assertEqual(health["status"], "degraded")
            self.assertEqual((health["errors"], health["retry_exhausted"], health["batch_count"]), (1, 1, 0))
            self.assertNotIn("synthetic", json.dumps(health))
            self.assertEqual(Path(root, "distill-health.json").stat().st_mode & 0o777, 0o600)

    def test_success_is_healthy_and_manifest_excludes_success_and_missing(self):
        with tempfile.TemporaryDirectory() as root:
            Path(td.queue_path(root)).write_text(json.dumps({"session_id": "ok"}) + "\n")
            with patch.object(td, "process_session", return_value={"status": "distilled"}):
                self.assertEqual(td._drain(root), 0)
            self.assertEqual(json.loads(Path(root, "distill-health.json").read_text())["status"], "healthy")
        ledger = {str(i): {"status": "error"} for i in range(14)}
        ledger.update(ok={"status": "distilled"}, gone={"status": "transcript_missing"})
        entries = [{"session_id": key} for key in ledger]
        manifest = td.failed_replay_manifest(ledger, entries)
        self.assertEqual(list(map(len, manifest["batches"])), [6, 6, 2])
        self.assertNotIn("ok", sum(manifest["batches"], []))
        self.assertNotIn("gone", sum(manifest["batches"], []))
        self.assertFalse(manifest["execution_authorized"])

    def test_prepare_replay_does_not_mutate_ledger_or_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "operator", "macroseat"); root.mkdir(parents=True)
            cfg = Path(tmp, "config.json"); cfg.write_text(json.dumps({"data_root": tmp, "operator_slug": "operator"}))
            ledger = Path(td.ledger_path(str(root))); ledger.write_text(json.dumps({"a": {"status": "error", "attempts": 3}}))
            queue = Path(td.queue_path(str(root))); queue.write_text('{"session_id":"a"}\n')
            before = (ledger.read_bytes(), queue.read_bytes(), ledger.stat().st_mtime_ns, queue.stat().st_mtime_ns)
            output = Path(tmp, "replay.json")
            with patch.dict(os.environ, {"IMPRINT_CONFIG": str(cfg)}), patch.object(td, "process_session") as process:
                self.assertEqual(td.main(["--prepare-failed-replay", str(output)]), 0)
                process.assert_not_called()
            self.assertEqual(before, (ledger.read_bytes(), queue.read_bytes(), ledger.stat().st_mtime_ns, queue.stat().st_mtime_ns))
            self.assertEqual(json.loads(output.read_text())["batches"], [["a"]])
            self.assertFalse(Path(root, "worker.lock").exists())
            ledger.write_text("broken")
            with self.assertRaises(ValueError):
                td.load_ledger(str(ledger), strict=True)
            self.assertFalse(Path(str(ledger) + ".corrupt").exists())


if __name__ == "__main__":
    unittest.main()
