import datetime
import json
import os
import tempfile
import unittest
from test_boot_digest import bd, _pack, NOW


class TasteHealth(unittest.TestCase):
    def test_retry_exhausted_empty_batch_visible_in_digest(self):
        health = {"schema": "taste-distiller-health/v1", "generated_at": NOW.isoformat(),
                  "status": "degraded", "errors": 4, "retry_exhausted": 4,
                  "transcript_missing": 0, "queue_bad_records": 0, "batch_count": 0, "batch_errors": 0}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "health.json")
            with open(path, "w") as fh: json.dump(health, fh)
            line = bd.taste_health_line(path, NOW)
            md = bd.build_digest(NOW, _pack(), taste_health=line)
            self.assertIn("TASTE: DEGRADED; errors 4 (4 retry-exhausted)", md)
            self.assertIn("STALE", bd.taste_health_line(path, NOW + datetime.timedelta(hours=1)))
            health.update(status="healthy", errors=0, retry_exhausted=0)
            with open(path, "w") as fh: json.dump(health, fh)
            self.assertIn("TASTE: healthy", bd.taste_health_line(path, NOW))
            with open(path, "w") as fh: fh.write("bad")
            self.assertIn("DEGRADED", bd.taste_health_line(path, NOW))
        self.assertIn("DEGRADED", bd.taste_health_line(path, NOW))
        self.assertIsNone(bd.taste_health_line(None, NOW))


if __name__ == "__main__":
    unittest.main()
