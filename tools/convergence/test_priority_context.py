"""Synthetic contract checks for the pure priority-context projection."""

import copy
import datetime as dt
import json
import sys
import unittest

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from priority_context import MAX_OUTPUT_CHARS, project_priority


NOW = dt.datetime(2026, 9, 20, 18, 0, tzinfo=dt.timezone.utc)
META = {
    "reference": "/priority.json",
    "sha256": "a" * 64,
    "observed_at": "2026-09-20T18:00:00Z",
    "http_status": 200,
}


def row(index=0, *, binding_status="unbound", url=None, long=False):
    text = "Reviewed source wording " + ("x" * 550 if long else "")
    return {
        "id": "row-%d" % index,
        "label": "Now · item %d" % index,
        "title": "Prepare the reviewed work item %d" % index,
        "owner": "Anthony",
        "reason": text,
        "mode": "anthony_judgment",
        "estimated_minutes": 45,
        "estimate_basis": "Trial estimate; actual duration remains unknown.",
        "url": url,
        "binding_status": binding_status,
    }


def payload(*, rows=None, checked="2026-09-20T17:59:00Z", local_date="2026-09-20"):
    return {
        "schema": "iris-priority-context/v1",
        "status": "current",
        "reason": "verified_evidence",
        "checked_at": checked,
        "plan": {
            "local_date": local_date,
            "timezone": "America/Los_Angeles",
            "prepared_at": "2026-09-20T17:30:00Z",
            "evidence_cutoff": "2026-09-20T17:00:00Z",
            "digest_cutoff": "2026-09-20T16:00:00Z",
            "digest_presentation": "recorded_unverified",
            "coverage": "partial",
            "coverage_note": "Communications coverage remains partial.",
            "capacity": "Protect the reviewed focus window; actual capacity is unknown.",
            "rows": rows if rows is not None else [row()],
        },
    }


class PriorityContextTests(unittest.TestCase):
    def project(self, value=None, now=NOW, metadata=None, rows=None):
        if rows is not None:
            value = payload(rows=rows)
        return project_priority(value if value is not None else payload(), now, metadata or META)

    def test_fresh_projection_is_flat_and_preserves_separate_binding_state(self):
        result = self.project(rows=[row(binding_status="unbound")])
        self.assertEqual(result["status"], "current")
        self.assertTrue(result["current"])
        self.assertEqual(result["availability"], "available")
        self.assertEqual(result["rows"][0]["binding_status"], "unbound")
        self.assertEqual(result["omitted_rows"], 0)

        self.assertNotIn("authority", result)
        self.assertNotIn("dispatch", json.dumps(result).lower())

    def test_partial_rows_are_bounded_and_omission_count_is_exact(self):
        result = self.project(rows=[row(i) for i in range(8)])
        self.assertEqual([item["id"] for item in result["rows"]], ["row-%d" % i for i in range(7)])
        self.assertEqual(result["omitted_rows"], 1)

    def test_midnight_and_old_receipts_are_stale_without_rows(self):
        midnight = self.project(now=dt.datetime(2026, 9, 21, 7, 0, tzinfo=dt.timezone.utc))
        self.assertEqual((midnight["status"], midnight["reason"], midnight["rows"]), ("stale", "plan_expired", []))
        old = self.project(payload(checked="2026-09-20T17:54:00Z"))
        self.assertEqual((old["status"], old["reason"], old["rows"]), ("stale", "plan_expired", []))

    def test_endpoint_stale_and_unavailable_are_row_free(self):
        stale = payload()
        stale.update(status="stale", reason="plan_expired", plan=None)
        self.assertEqual(self.project(stale)["status"], "stale")
        unavailable = payload()
        unavailable.update(status="unavailable", reason="evidence_unavailable", plan=None)
        result = self.project(unavailable)
        self.assertEqual((result["availability"], result["status"], result["rows"]), ("unavailable", "unavailable", []))

    def test_future_and_invalid_shapes_fail_closed_with_fixed_reason(self):
        future = self.project(payload(checked="2026-09-20T18:01:00Z"))
        self.assertEqual((future["status"], future["reason"], future["rows"]), ("unavailable", "evidence_unavailable", []))
        prepared_after_check = payload()
        prepared_after_check["plan"]["prepared_at"] = "2026-09-20T18:00:30Z"
        result = self.project(prepared_after_check)
        self.assertEqual((result["status"], result["reason"], result["rows"]), ("unavailable", "evidence_unavailable", []))
        for mutation in (
            lambda value: value.update(extra="ignored"),
            lambda value: value["plan"].update(coverage="unknown"),
            lambda value: value["plan"]["rows"].append(copy.deepcopy(value["plan"]["rows"][0])),
            lambda value: value["plan"]["rows"][0].update(binding_status="dispatch"),
            lambda value: value["plan"]["rows"][0].update(estimated_minutes=True),
            lambda value: value["plan"]["rows"][0].update(id="bad id"),
        ):
            candidate = payload()
            mutation(candidate)
            result = self.project(candidate)
            self.assertEqual((result["status"], result["reason"], result["rows"]), ("unavailable", "evidence_unavailable", []))

    def test_safe_links_are_retained_and_malformed_links_refuse_source(self):
        notion = "https://www.notion.so/Work-0123456789abcdef0123456789abcdef"
        google = "https://docs.google.com/document/d/abcdefghijkl/edit"
        result = self.project(rows=[row(url=notion), row(1, url=google)])
        self.assertEqual(result["rows"][0]["url"], notion)
        self.assertEqual(result["rows"][1]["url"], google)
        for bad in (
            "javascript:alert(1)",
            "https://docs.google.com/document/d/abcdefghijkl/edit?x=1",
            "https://docs.google.com:443/document/d/abcdefghijkl/edit",
            "HTTPS://docs.google.com/document/d/abcdefghijkl/edit",
            "https://example.com/a",
        ):
            candidate = payload(rows=[row(url=bad)])
            result = self.project(candidate)
            self.assertEqual((result["status"], result["reason"], result["rows"]), ("unavailable", "evidence_unavailable", []))

    def test_text_is_plain_and_explicitly_excerpted(self):
        candidate = payload(rows=[row(long=True)])
        candidate["plan"]["coverage_note"] = "C" * 600
        candidate["plan"]["capacity"] = "K" * 1000
        candidate["plan"]["rows"][0]["title"] = "<script>alert(1)</script>" + "T" * 130
        result = self.project(candidate)
        serialized = json.dumps(result)
        self.assertIn("<script>", serialized)
        self.assertNotIn("&lt;script&gt;", serialized)
        self.assertIn("[excerpt]", serialized)
        self.assertLessEqual(len(result["coverage_note"]), 160)
        self.assertLessEqual(len(result["capacity"]), 160)
        self.assertLessEqual(len(result["rows"][0]["title"]), 120)
        candidate["plan"]["rows"][0]["title"] = "Q&A"
        self.assertEqual(self.project(candidate)["rows"][0]["title"], "Q&A")

    def test_maximal_valid_projection_stays_bounded_without_losing_ids_or_times(self):
        rows = []
        for index in range(4):
            item = row(index, long=True, url="https://docs.google.com/document/d/abcdefghijkl/edit")
            item["id"] = ("r%d-" % index) + "x" * 96
            item["label"] = "L" * 160
            item["title"] = "T" * 160
            item["owner"] = "O" * 160
            item["estimate_basis"] = "B" * 300
            rows.append(item)
        result = self.project(rows=rows)
        self.assertLessEqual(len(json.dumps(result)), MAX_OUTPUT_CHARS)
        self.assertEqual([item["id"] for item in result["rows"]], [item["id"] for item in rows])
        self.assertEqual(result["prepared_at"], "2026-09-20T17:30:00Z")
        self.assertEqual(result["omitted_rows"], 0)

        downgraded = project_priority(payload(rows=rows), NOW, META, work_current=False)
        self.assertLessEqual(len(json.dumps(downgraded)), MAX_OUTPUT_CHARS)
        # This source's rows are unbound, so there is no invented downgrade note.
        self.assertNotIn("binding_note", downgraded)
        for item in rows:
            item["binding_status"] = "current"
        downgraded = project_priority(payload(rows=rows), NOW, META, work_current=False)
        self.assertLessEqual(len(json.dumps(downgraded)), MAX_OUTPUT_CHARS)
        self.assertEqual({item["binding_status"] for item in downgraded["rows"]}, {"unavailable"})
        self.assertIn("surrounding board", downgraded["binding_note"])

    def test_metadata_is_fixed_and_arbitrary_errors_are_not_echoed(self):
        result = self.project(metadata={"reference": "https://evil.example", "sha256": "secret", "observed_at": "bad"})
        self.assertEqual(result["reference"], "/priority.json")
        self.assertIsNone(result["sha256"])
        self.assertNotIn("evil.example", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(result["reason"], "evidence_unavailable")


if __name__ == "__main__":
    unittest.main()
