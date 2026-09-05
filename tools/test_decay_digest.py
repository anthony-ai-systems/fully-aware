"""Producer-format fixture survives pack and digest projection with action counts."""
import datetime
import os
import unittest
from test_assemble_boot_pack import ab, _manifest, _backlog
from test_boot_digest import bd, _pack


class DecayDigest(unittest.TestCase):
    def test_fixture_appears_with_weekly_states_and_stale_retention(self):
        now = datetime.datetime(2026, 9, 5, 10, tzinfo=datetime.timezone.utc)
        directory = os.path.join(os.path.dirname(__file__), "fixtures", "decay")
        decay = ab.load_decay(directory, now)
        self.assertTrue(decay["present"])
        pack = _pack(generated_at=now.isoformat())
        pack["sections"]["decay"] = decay
        digest = bd.build_digest(now, pack)
        self.assertIn("DECAY:", digest)
        for label in ("pending", "needs update", "deferred", "reviewed", "weekly", "2026-08-31"):
            self.assertIn(label, digest)
        self.assertNotIn("STALE", digest)
        pack["sections"]["decay"] = ab.load_decay(directory, now + datetime.timedelta(days=9))
        self.assertIn("STALE (work retained)", bd.build_digest(now, pack))
        self.assertEqual(decay["state_counts"], pack["sections"]["decay"]["state_counts"])

    def test_missing_feed_is_unknown(self):
        pack = _pack()
        pack["sections"]["decay"] = ab.load_decay("/synthetic/missing")
        self.assertIn("pending work unknown", bd.decay_line(pack))


if __name__ == "__main__":
    unittest.main()
