import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sweep_route as route

NOW = dt.datetime(2026, 9, 23, 16, tzinfo=dt.timezone.utc)


def config(status="ACTIVE", owner=route.OWNER, schedule=route.SCHEDULE):
    return (f'version = 1\nid = "{route.AUTOMATION}"\nkind = "heartbeat"\n'
            f'status = "{status}"\ntarget_thread_id = "{owner}"\nrrule = "{schedule}"\n'
            'prompt = "SECRET_PRIVATE_PROMPT"\nname = "SECRET_PRIVATE_NAME"\n')


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "automation.toml"
        self.path.write_text(config())

    def read(self):
        return route.read_route(self.path, now=NOW)

    def test_active_config_does_not_prove_execution_or_expose_content(self):
        before = self.path.read_bytes()
        value = self.read()
        self.assertEqual(value["configured_status"], "active")
        self.assertTrue(value["expected_owner"])
        self.assertTrue(value["expected_schedule"])
        self.assertFalse(value["execution_verified"])
        self.assertEqual(value["authority"], "none")
        self.assertNotIn("SECRET", json.dumps(value))
        self.assertNotIn(str(self.path), json.dumps(value))
        self.assertEqual(self.path.read_bytes(), before)

    def test_paused_is_distinct_from_missing_and_active(self):
        self.path.write_text(config("PAUSED"))
        self.assertEqual(self.read()["configured_status"], "paused")
        self.path.unlink()
        value = self.read()
        self.assertEqual(value["configured_status"], "unknown")
        self.assertEqual(value["reason"], "local_configuration_missing")

    def test_owner_or_schedule_changes_are_visible_without_echoing_them(self):
        self.path.write_text(config(owner="PRIVATE_CHANGED_OWNER", schedule="PRIVATE_SCHEDULE"))
        value = self.read()
        self.assertFalse(value["expected_owner"])
        self.assertFalse(value["expected_schedule"])
        self.assertNotIn("PRIVATE", json.dumps(value))

    def test_wrong_identity_invalid_toml_and_unknown_status_fail_closed(self):
        for text in (config("OTHER"), config().replace('version = 1', 'version = true'),
                     config().replace(route.AUTOMATION, 'other'), 'invalid toml',
                     config().replace('kind = "heartbeat"', 'kind = "cron"')):
            with self.subTest(text=text):
                self.path.write_text(text)
                self.assertEqual(self.read()["availability"], "unavailable")

    def test_symlink_fifo_directory_and_oversized_input_do_not_block(self):
        self.path.unlink()
        self.path.symlink_to(self.path.parent / 'missing')
        self.assertEqual(self.read()["availability"], "unavailable")
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(self.read()["availability"], "unavailable")
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(self.read()["availability"], "unavailable")
        self.path.rmdir()
        self.path.write_bytes(b'x' * (route.MAX_BYTES + 1))
        self.assertEqual(self.read()["availability"], "unavailable")

    def test_replaced_file_and_permission_error_are_unknown(self):
        original = Path.lstat
        with mock.patch.object(Path, "lstat", side_effect=PermissionError):
            self.assertEqual(self.read()["availability"], "unavailable")
        other = self.path.parent / 'other.toml'
        other.write_text(config())
        with mock.patch.object(Path, "lstat", return_value=original(other)):
            self.assertEqual(self.read()["availability"], "unavailable")

    def test_unconfigured_and_invalid_unicode_are_safe(self):
        self.assertEqual(route.read_route(None, now=NOW)["reason"], "not_configured")
        self.path.write_bytes(b'\xff')
        self.assertEqual(self.read()["availability"], "unavailable")


if __name__ == "__main__":
    unittest.main()
