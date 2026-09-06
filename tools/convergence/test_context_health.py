import copy
import datetime as dt
import json
import unittest

from context_health import (
    CONTEXT_HEALTH_SCHEMA,
    DEGRADED,
    FRESH,
    INVALID,
    PARTIAL,
    STALE,
    UNKNOWN,
    build_health,
    render_health,
)


UTC = dt.timezone.utc
OBSERVED = dt.datetime(2026, 9, 6, 0, 30, tzinfo=UTC)


def imprint_fixture(*, status="healthy", reasons=None, metrics=None):
    default_metrics = {
        "compiler_count": 1,
        "spool_depth": 2,
        "spool_unacknowledged_count": 1,
        "quarantine_count": 0,
        "hook_failure_count": 0,
        "retrieval_omitted_count": 3,
        "stale_lock_count": 0,
        "abandoned_temp_count": 0,
        "verified_backup_count": 2,
        "invalid_backup_count": 0,
    }
    if metrics is not None:
        default_metrics.update(metrics)
    return {
        "health_schema_version": "1.0.0",
        "status": status,
        "degraded_reasons": list(reasons or []),
        "metrics": default_metrics,
        "private_path": "/private/fixture/imprint",
        "private_message": "fictional source content must not appear",
    }


def taste_fixture(*, generated_at=None, status="healthy", **overrides):
    timestamp = generated_at if isinstance(generated_at, str) else (generated_at or OBSERVED).isoformat()
    result = {
        "schema": "taste-distiller-health/v1",
        "generated_at": timestamp,
        "status": status,
        "errors": 0,
        "retry_exhausted": 0,
        "transcript_missing": 0,
        "queue_bad_records": 0,
        "batch_count": 4,
        "batch_errors": 0,
        "private_path": "/private/fixture/taste",
        "private_message": "fictional taste content must not appear",
    }
    result.update(overrides)
    return result


def decay_fixture(*, as_of="2026-09-06", freshness="fresh", **overrides):
    counts = {
        "total": 5,
        "reviewed": 2,
        "needs_update": 1,
        "deferred": 1,
        "pending": 1,
        "unchecked": 0,
    }
    result = {
        "configured": True,
        "present": True,
        "path": "/private/fixture/atlas",
        "file": "DECAY-2026-09-06.md",
        "source_file": "/private/fixture/atlas/DECAY-2026-09-06.md",
        "as_of": as_of,
        "cadence": "weekly (Monday)",
        "freshness": freshness,
        "freshness_threshold_seconds": 7 * 24 * 60 * 60,
        "state_counts": counts,
        "counts": dict(counts),
        "items": [{"id": "SECRET-DECAY-ID", "path": "/private/fixture/item"}],
        "summary": "SECRET decay prose must not appear",
    }
    result.update(overrides)
    return result


def boot_fixture(*, generated_at=None, decay=None, queue=None):
    if queue is None:
        queue = {
            "items": [
                {
                    "kind": "adjudication",
                    "source": "adjudication:atlas-v2",
                    "as_of": "2026-09-06",
                    "summary": "SECRET adjudication prose /private/fixture",
                },
                {
                    "kind": "decay",
                    "source": "decay:weekly",
                    "as_of": "2026-09-06",
                    "summary": "SECRET decay queue prose",
                },
            ]
        }
    return {
        "schema": "boot-pack/v1",
        "generated_at": (generated_at or OBSERVED).isoformat(),
        "sections": {
            "decay": decay if decay is not None else decay_fixture(),
            "decision_queue": queue,
        },
        "private_path": "/private/fixture/boot-pack",
    }


class ContextHealthTests(unittest.TestCase):
    def test_missing_decay_with_partial_adjudication_remains_partial(self):
        boot = boot_fixture(queue={"items": [
            {"kind": "adjudication", "source": "adjudication:atlas-v2", "as_of": "2026-09-06"},
            {"kind": "adjudication", "source": "adjudication:atlas-v2", "as_of": "invalid"}]})
        boot["sections"].pop("decay")
        atlas = build_health(None, None, boot, OBSERVED, OBSERVED)["components"]["atlas"]
        self.assertEqual(atlas["availability"], PARTIAL)
        self.assertEqual(atlas["coverage"], "partial")
        self.assertIsNotNone(atlas["last_verified_at"])

    def test_cli_distinguishes_corrupt_from_missing_snapshots(self):
        import contextlib, io, tempfile
        from pathlib import Path
        from context_health import main
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp).resolve() / "bad.json"
            bad.write_text("{ malformed")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["--imprint-health", str(bad), "--taste-health", str(bad.parent / "absent"),
                      "--captured-at", OBSERVED.isoformat(), "--format", "json"])
            components = json.loads(output.getvalue())["components"]
            self.assertEqual(components["imprint"]["availability"], INVALID)
            self.assertEqual(components["taste"]["availability"], "unavailable")
            self.assertEqual(components["atlas"]["availability"], "unavailable")

    def test_healthy_closed_shape_and_distinct_capture(self):
        imprint = imprint_fixture()
        taste = taste_fixture()
        boot = boot_fixture()
        originals = copy.deepcopy((imprint, taste, boot))

        report = build_health(imprint, taste, boot, OBSERVED, OBSERVED + dt.timedelta(seconds=30))

        self.assertEqual(report["schema"], CONTEXT_HEALTH_SCHEMA)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(
            set(report),
            {"schema", "observed_at", "captured_at", "status", "reasons", "components"},
        )
        self.assertEqual(set(report["components"]), {"imprint", "taste", "atlas"})
        for component in report["components"].values():
            self.assertEqual(set(component), {
                "availability", "coverage", "freshness", "last_verified_at",
                "status", "reasons", "counts",
            })
            self.assertEqual(component["status"], "healthy")
        self.assertEqual(
            report["components"]["imprint"]["last_verified_at"],
            "2026-09-06T00:30:30Z",
        )
        self.assertEqual(
            report["components"]["taste"]["last_verified_at"],
            "2026-09-06T00:30:00Z",
        )
        self.assertEqual((imprint, taste, boot), originals)

    def test_taste_old_and_future_are_stale(self):
        old = build_health(
            None,
            taste_fixture(generated_at=OBSERVED - dt.timedelta(minutes=46)),
            None,
            OBSERVED,
            OBSERVED,
        )
        self.assertEqual(old["components"]["taste"]["freshness"], STALE)
        self.assertEqual(old["components"]["taste"]["status"], DEGRADED)
        self.assertIn("taste_stale", old["components"]["taste"]["reasons"])

        future = build_health(
            None,
            taste_fixture(generated_at=OBSERVED + dt.timedelta(minutes=6)),
            None,
            OBSERVED,
            OBSERVED,
        )
        self.assertEqual(future["components"]["taste"]["freshness"], STALE)
        self.assertEqual(future["components"]["taste"]["status"], DEGRADED)
        self.assertIn("taste_future", future["components"]["taste"]["reasons"])

    def test_taste_naive_missing_and_malformed_counters(self):
        naive = build_health(
            None,
            taste_fixture(generated_at="2026-09-06T00:30:00"),
            None,
            OBSERVED,
            OBSERVED,
        )["components"]["taste"]
        self.assertEqual(naive["availability"], INVALID)
        self.assertEqual(naive["status"], UNKNOWN)
        self.assertEqual(naive["freshness"], UNKNOWN)
        self.assertIn("taste_timestamp_naive", naive["reasons"])

        missing = taste_fixture()
        del missing["generated_at"]
        missing_report = build_health(None, missing, None, OBSERVED, OBSERVED)
        self.assertIn("taste_timestamp_missing", missing_report["components"]["taste"]["reasons"])
        self.assertEqual(missing_report["components"]["taste"]["status"], UNKNOWN)

        malformed = build_health(
            None,
            taste_fixture(errors=True, batch_errors="1"),
            None,
            OBSERVED,
            OBSERVED,
        )["components"]["taste"]
        self.assertEqual(malformed["availability"], PARTIAL)
        self.assertEqual(malformed["status"], UNKNOWN)
        self.assertIn("taste_count_invalid", malformed["reasons"])
        self.assertNotIn("errors", malformed["counts"])

        relation = build_health(
            None,
            taste_fixture(retry_exhausted=2),
            None,
            OBSERVED,
            OBSERVED,
        )["components"]["taste"]
        self.assertIn("taste_count_relation_invalid", relation["reasons"])
        self.assertEqual(relation["status"], UNKNOWN)

    def test_imprint_allowlist_degraded_and_capture_age(self):
        source = imprint_fixture(
            status="healthy",
            reasons=["hook_failures_present", "SECRET /private/fixture"],
            metrics={"hook_failure_count": True, "SECRET_COUNT": 9},
        )
        component = build_health(source, None, None, OBSERVED, OBSERVED)["components"]["imprint"]
        self.assertEqual(component["availability"], PARTIAL)
        self.assertEqual(component["status"], DEGRADED)
        self.assertIn("hook_failures_present", component["reasons"])
        self.assertIn("imprint_reason_unknown", component["reasons"])
        self.assertIn("imprint_metric_invalid", component["reasons"])
        self.assertNotIn("hook_failure_count", component["counts"])
        encoded = json.dumps(component)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("/private", encoded)

        old = build_health(
            imprint_fixture(), None, None, OBSERVED,
            OBSERVED - dt.timedelta(hours=37),
        )["components"]["imprint"]
        self.assertEqual(old["freshness"], STALE)
        self.assertIn("imprint_capture_stale", old["reasons"])

        future_report = build_health(
            imprint_fixture(), None, None, OBSERVED,
            OBSERVED + dt.timedelta(minutes=6),
        )
        future = future_report["components"]["imprint"]
        self.assertEqual(future["freshness"], "future")
        self.assertIn("imprint_capture_future", future["reasons"])
        self.assertIn("capture_timestamp_future", future_report["reasons"])

    def test_naive_projection_arguments_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "observed_at_must_be_timezone_aware"):
            build_health(None, None, None, dt.datetime(2026, 9, 6), OBSERVED)
        with self.assertRaisesRegex(ValueError, "captured_at_must_be_timezone_aware"):
            build_health(None, None, None, OBSERVED, dt.datetime(2026, 9, 6))

    def test_atlas_partial_missing_decay_and_no_false_zero(self):
        boot = boot_fixture(decay=None)
        del boot["sections"]["decay"]
        atlas = build_health(None, None, boot, OBSERVED, OBSERVED)["components"]["atlas"]
        self.assertEqual(atlas["availability"], PARTIAL)
        self.assertEqual(atlas["status"], DEGRADED)
        self.assertIn("atlas_decay_unavailable", atlas["reasons"])
        self.assertIn("adjudication", atlas["counts"])
        self.assertNotIn("decay", atlas["counts"])

    def test_atlas_old_decay_and_adjudication(self):
        old_queue = {
            "items": [{
                "kind": "adjudication",
                "source": "adjudication:atlas-v2",
                "as_of": "2026-09-04T00:00:00Z",
                "summary": "SECRET old queue prose",
            }]
        }
        old_boot = boot_fixture(
            decay=decay_fixture(as_of="2026-08-28"),
            queue=old_queue,
        )
        atlas = build_health(None, None, old_boot, OBSERVED, OBSERVED)["components"]["atlas"]
        self.assertEqual(atlas["status"], DEGRADED)
        self.assertEqual(atlas["counts"]["decay"]["total"], 5)
        self.assertIn("atlas_decay_stale", atlas["reasons"])
        self.assertIn("atlas_adjudication_stale", atlas["reasons"])
        self.assertNotIn("SECRET", json.dumps(atlas))

    def test_atlas_last_verified_uses_oldest_source_and_daily_date_precision(self):
        old_queue = {
            "items": [{
                "kind": "adjudication",
                "source": "adjudication:atlas-v2",
                "as_of": "2026-09-04",
                "summary": "SECRET old adjudication prose",
            }]
        }
        old_atlas = build_health(
            None,
            None,
            boot_fixture(queue=old_queue),
            OBSERVED,
            OBSERVED,
        )["components"]["atlas"]
        self.assertEqual(old_atlas["freshness"], STALE)
        self.assertIn("atlas_adjudication_stale", old_atlas["reasons"])
        self.assertEqual(old_atlas["last_verified_at"], "2026-09-04T00:00:00Z")

        # Date-only current-day sources remain fresh through the daily window,
        # while the reported verification time retains day-boundary precision.
        late_observed = OBSERVED + dt.timedelta(hours=35)
        current_atlas = build_health(
            None,
            None,
            boot_fixture(),
            late_observed,
            late_observed,
        )["components"]["atlas"]
        self.assertEqual(current_atlas["freshness"], FRESH)
        self.assertEqual(current_atlas["status"], "healthy")
        self.assertEqual(current_atlas["last_verified_at"], "2026-09-06T00:00:00Z")

    def test_atlas_unknown_adjudication_does_not_imply_no_work(self):
        queue = {"items": [{
            "kind": "adjudication",
            "source": "other-source",
            "as_of": "2026-09-06",
            "summary": "SECRET unrelated content",
        }]}
        atlas = build_health(
            None, None, boot_fixture(queue=queue), OBSERVED, OBSERVED,
        )["components"]["atlas"]
        self.assertEqual(atlas["availability"], PARTIAL)
        self.assertEqual(atlas["status"], DEGRADED)
        self.assertIn("atlas_adjudication_unavailable", atlas["reasons"])
        self.assertNotIn("adjudication", atlas["counts"])

    def test_render_health_stays_bounded(self):
        report = build_health(
            imprint_fixture(), taste_fixture(), boot_fixture(), OBSERVED, OBSERVED,
        )
        rendered = render_health(report)
        self.assertIn("# Context health", rendered)
        self.assertIn("- schema: context-health/v1", rendered)
        self.assertIn("compiler_count: 1", rendered)
        self.assertNotIn("private", rendered.lower())

        malicious = copy.deepcopy(report)
        malicious["reasons"] = ["<script>SECRET</script>", "taste_stale"]
        malicious["components"]["imprint"]["reasons"] = ["/private/SECRET", "imprint_capture_stale"]
        malicious["components"]["imprint"]["counts"] = {
            "private_path": "/private/SECRET",
            "compiler_count": 3,
        }
        sanitized = render_health(malicious)
        self.assertNotIn("SECRET", sanitized)
        self.assertNotIn("private_path", sanitized)
        self.assertIn("compiler_count: 3", sanitized)

        invalid = render_health({"schema": "wrong", "message": "SECRET"})
        self.assertIn("status: unknown", invalid)
        self.assertIn("projection_invalid", invalid)
        self.assertNotIn("SECRET", invalid)


if __name__ == "__main__":
    unittest.main()
