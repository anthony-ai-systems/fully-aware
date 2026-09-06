import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

import capture_context as c


UTC = dt.timezone.utc
CAPTURED = dt.datetime(2026, 9, 6, 15, 0, tzinfo=UTC)


def policy(**overrides):
    value = {
        "schema": "fully-aware-context-policy/v1",
        "client_id": "client-a",
        "project_id": "project-a",
        "imprint_domain": "domain-one",
        "imprint_record_ids": [],
        "atlas_claim_ids": [],
    }
    value.update(overrides)
    return value


def record(
    record_id,
    *,
    provenance_status="ratified",
    authority_tier="ratified_knowledge",
    ontology_partition="judgment",
    domain_id="domain-one",
):
    return types.SimpleNamespace(
        record_id=record_id,
        provenance_status=provenance_status,
        authority_tier=authority_tier,
        ontology_partition=ontology_partition,
        domain_id=domain_id,
        text="fictional judgment text for testing",
        evidence_ids=["evidence:one"],
        valid_from="2026-09-01",
    )


@contextlib.contextmanager
def fake_imprint_modules(records):
    package = types.ModuleType("imprint")
    package.__path__ = []
    config = types.ModuleType("imprint.config")
    config.load_config = lambda: {"fixture": True}
    config.resolved_operator_root = lambda _config: Path("/tmp/fictional-imprint-root")
    store = types.ModuleType("imprint.store")

    class ImprintStore:
        def __init__(self, path):
            self.path = path

    store.ImprintStore = ImprintStore
    retrieve = types.ModuleType("imprint.retrieve")
    retrieve.__path__ = []
    source = types.ModuleType("imprint.retrieve.store_source")

    class StoreRetrievalSource:
        def __init__(self, _store):
            pass

        def retrieval_candidates(self, snapshot_id):
            self.snapshot_id = snapshot_id
            return list(records)

    source.StoreRetrievalSource = StoreRetrievalSource
    engine = types.ModuleType("imprint.retrieve.engine")
    engine.eligible = lambda item, domain: domain is None or item.domain_id == domain
    modules = {
        "imprint": package,
        "imprint.config": config,
        "imprint.store": store,
        "imprint.retrieve": retrieve,
        "imprint.retrieve.store_source": source,
        "imprint.retrieve.engine": engine,
    }
    with mock.patch.dict(sys.modules, modules):
        yield


def atlas_module(directory, responses):
    path = Path(directory) / "fictional_atlas_provider.py"
    path.write_text(
        "_RESPONSES = " + repr(responses) + "\n"
        "def handle_check_claim(request):\n"
        "    return _RESPONSES[request['claim_id']]\n",
        encoding="utf-8",
    )
    return path


def local_tempdir():
    """Use a real-parent temp directory; capture_context rejects symlink parents."""
    return tempfile.TemporaryDirectory(dir=str(Path.cwd()))


def atlas_claim(claim_id):
    return {
        "claim_id": claim_id,
        "claim_type": "fictional-state",
        "subject": "fixture-subject",
        "state": "fixture-state",
        "source_path": "/private/fixture/source.md",
        "source_commit": "a" * 40,
        "line": 7,
        "as_of": "2026-09-06",
        "extracted_at": "2026-09-06T14:00:00Z",
    }


class ValidatePolicyTests(unittest.TestCase):
    def test_policy_is_closed_and_duplicate_or_invalid_selections_reject(self):
        self.assertEqual(c.validate_policy(policy()), policy())
        cases = [
            ({"extra": True}, "context-policy-fields"),
            ({"client_id": "Client-A"}, "context-policy-identity"),
            ({"project_id": ""}, "context-policy-identity"),
            ({"imprint_record_ids": ["judgment:one", "judgment:one"]}, "context-selection-invalid"),
            ({"atlas_claim_ids": ["claim with spaces"]}, "context-selection-invalid"),
            ({"imprint_domain": 7}, "context-domain-invalid"),
            ({"imprint_domain": "x" * 161}, "context-domain-invalid"),
        ]
        for changes, error in cases:
            with self.subTest(changes=changes):
                candidate = policy()
                if "extra" in changes:
                    candidate["extra"] = changes["extra"]
                else:
                    candidate.update(changes)
                with self.assertRaisesRegex(ValueError, error):
                    c.validate_policy(candidate)

    def test_missing_required_field_and_wrong_schema_reject(self):
        missing = policy()
        del missing["project_id"]
        with self.assertRaisesRegex(ValueError, "context-policy-fields"):
            c.validate_policy(missing)
        wrong = policy(schema="other/v1")
        with self.assertRaisesRegex(ValueError, "context-policy-schema"):
            c.validate_policy(wrong)


class CaptureImprintTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            record("judgment:good"),
            record("judgment:captured", provenance_status="captured"),
            record("judgment:model", authority_tier="inferred_candidate"),
            record("judgment:cross-domain", domain_id="domain-two"),
            record("judgment:other-partition", ontology_partition="policy"),
        ]

    def test_selection_keeps_only_ratified_eligible_judgment_partition(self):
        with fake_imprint_modules(self.records):
            result = c.capture_imprint(policy(imprint_record_ids=["judgment:good"]))
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["eligible_ratified_count"], 1)
        self.assertEqual([item["record_id"] for item in result["judgments"]], ["judgment:good"])
        self.assertEqual(result["judgments"][0]["authority"], "operator-ratified")
        self.assertFalse(result["historical_snapshot"])
        self.assertNotIn("captured", json.dumps(result))
        self.assertNotIn("inferred", json.dumps(result))
        self.assertNotIn("domain-two", json.dumps(result))
        self.assertNotIn("other-partition", json.dumps(result))

    def test_captured_model_inferred_cross_domain_and_missing_selection_reject(self):
        with fake_imprint_modules(self.records):
            for selected in (
                "judgment:captured",
                "judgment:model",
                "judgment:cross-domain",
                "judgment:other-partition",
                "judgment:missing",
            ):
                with self.subTest(selected=selected):
                    with self.assertRaisesRegex(ValueError, "selected-judgment-not-ratified-or-eligible"):
                        c.capture_imprint(policy(imprint_record_ids=[selected]))

    def test_duplicate_eligible_record_is_not_silently_selected(self):
        duplicate = self.records + [copy.copy(self.records[0])]
        with fake_imprint_modules(duplicate):
            with self.assertRaisesRegex(ValueError, "selected-judgment-not-ratified-or-eligible"):
                c.capture_imprint(policy(imprint_record_ids=["judgment:good"]))


class CaptureAtlasTests(unittest.TestCase):
    def test_verdicts_preserve_mechanical_boundary_and_unknown_is_unavailable(self):
        responses = {
            "claim-current": {
                "status": "ok", "found": True, "verdict": "current",
                "claim": atlas_claim("claim-current"),
            },
            "claim-strategic": {
                "status": "ok", "found": True, "verdict": "not-mechanically-verifiable",
                "claim": atlas_claim("claim-strategic"),
            },
            "claim-unknown": {
                "status": "ok", "found": True, "verdict": "model-inferred",
                "claim": atlas_claim("claim-unknown"),
            },
            "claim-missing": {"status": "ok", "found": False},
        }
        with local_tempdir() as temp:
            provider = atlas_module(temp, responses)
            result = c.capture_atlas(
                policy(atlas_claim_ids=list(responses)),
                provider,
            )
        self.assertEqual(result["status"], "checked")
        self.assertEqual([item["claim_id"] for item in result["claims"]], list(responses))
        by_id = {item["claim_id"]: item for item in result["claims"]}
        self.assertEqual(by_id["claim-current"]["status"], "current")
        self.assertEqual(by_id["claim-current"]["authority"], "mechanically-verified")
        self.assertEqual(by_id["claim-strategic"]["status"], "not-mechanically-verifiable")
        self.assertEqual(by_id["claim-strategic"]["authority"], "unverified-context")
        self.assertEqual(by_id["claim-strategic"]["routed_to"], "STRATEGIC_REVIEW")
        self.assertEqual(by_id["claim-unknown"]["status"], "unavailable")
        self.assertNotEqual(by_id["claim-unknown"]["authority"], "mechanically-verified")
        self.assertEqual(by_id["claim-missing"]["status"], "unavailable")
        self.assertEqual(by_id["claim-missing"]["authority"], "none")

    def test_response_claim_id_mismatch_fails_closed(self):
        responses = {
            "claim-requested": {
                "status": "ok", "found": True, "verdict": "current",
                "claim": atlas_claim("claim-other"),
            }
        }
        with local_tempdir() as temp:
            provider = atlas_module(temp, responses)
            with self.assertRaisesRegex(ValueError, "atlas-claim-mismatch"):
                c.capture_atlas(policy(atlas_claim_ids=["claim-requested"]), provider)


class CapturePacketTests(unittest.TestCase):
    def test_empty_successful_adapters_are_distinct_from_unavailable(self):
        empty_imprint = {
            "status": "no-ratified-judgments-selected",
            "eligible_ratified_count": 0,
            "judgments": [],
            "observation_sha256": "fixture-imprint",
            "historical_snapshot": False,
        }
        empty_atlas = {
            "status": "no-claims-selected",
            "claims": [],
            "observation_sha256": "fixture-atlas",
            "module_sha256": "fixture-module",
        }
        result = c.capture(
            policy(),
            "/tmp/fictional-atlas.py",
            CAPTURED,
            imprint_reader=lambda _policy: empty_imprint,
            atlas_reader=lambda _policy, _path: empty_atlas,
        )
        self.assertEqual(result["imprint"]["status"], "no-ratified-judgments-selected")
        self.assertEqual(result["atlas"]["status"], "no-claims-selected")
        self.assertEqual(
            result["limitations"],
            ["no-ratified-judgments-in-packet", "no-atlas-claims-in-packet"],
        )

        unavailable = c.capture(
            policy(),
            "/tmp/fictional-atlas.py",
            CAPTURED,
            imprint_reader=mock.Mock(side_effect=OSError("fixture failure")),
            atlas_reader=mock.Mock(side_effect=ImportError("fixture failure")),
        )
        self.assertEqual(unavailable["imprint"]["status"], "unavailable")
        self.assertEqual(unavailable["atlas"]["status"], "unavailable")
        self.assertIn("imprint-retrieval-unavailable", unavailable["limitations"])
        self.assertIn("atlas-check-unavailable", unavailable["limitations"])
        self.assertIn("no-ratified-judgments-in-packet", unavailable["limitations"])
        self.assertIn("no-atlas-claims-in-packet", unavailable["limitations"])
        self.assertFalse(unavailable["imprint"]["judgments"])
        self.assertFalse(unavailable["atlas"]["claims"])

    def test_packet_scope_expiry_and_no_action_authority(self):
        packet = c.capture(
            policy(client_id="client-z", project_id="project-z"),
            "/tmp/fictional-atlas.py",
            CAPTURED,
            imprint_reader=lambda _policy: {
                "status": "selected", "eligible_ratified_count": 1,
                "judgments": [{"record_id": "judgment:one"}],
                "observation_sha256": "fixture-imprint",
                "historical_snapshot": False,
            },
            atlas_reader=lambda _policy, _path: {
                "status": "checked",
                "claims": [{"claim_id": "claim:one", "status": "current"}],
                "observation_sha256": "fixture-atlas",
                "module_sha256": "fixture-module",
            },
        )
        self.assertEqual(packet["client_id"], "client-z")
        self.assertEqual(packet["project_id"], "project-z")
        self.assertEqual(packet["permitted_effect"], "local-draft")
        self.assertFalse(packet["grants_action_authority"])
        self.assertEqual(packet["captured_at"], "2026-09-06T15:00:00+00:00")
        self.assertEqual(packet["expires_at"], "2026-09-07T15:00:00+00:00")

    def test_naive_capture_time_rejects_before_readers(self):
        imprint_reader = mock.Mock()
        atlas_reader = mock.Mock()
        with self.assertRaisesRegex(ValueError, "context-time-invalid"):
            c.capture(
                policy(), "/tmp/fictional-atlas.py", dt.datetime(2026, 9, 6),
                imprint_reader=imprint_reader, atlas_reader=atlas_reader,
            )
        imprint_reader.assert_not_called()
        atlas_reader.assert_not_called()


class CliPublicationTests(unittest.TestCase):
    def test_directory_atlas_module_is_bounded_unavailable(self):
        with local_tempdir() as temp:
            packet = c.capture(policy(), str(Path(temp).resolve()), CAPTURED,
                imprint_reader=lambda _policy: {"judgments": []})
            self.assertEqual(packet["atlas"]["status"], "unavailable")
            self.assertIn("atlas-check-unavailable", packet["limitations"])

    def test_cli_summary_is_bounded_and_publication_is_exclusive(self):
        packet = {
            "schema": c.SCHEMA,
            "client_id": "client-a",
            "project_id": "project-a",
            "captured_at": CAPTURED.isoformat(),
            "expires_at": (CAPTURED + dt.timedelta(hours=24)).isoformat(),
            "policy_sha256": "a" * 64,
            "permitted_effect": "local-draft",
            "grants_action_authority": False,
            "imprint": {"judgments": [{"text": "SECRET fixture text"}]},
            "atlas": {"claims": []},
            "limitations": [],
        }
        with local_tempdir() as temp:
            root = Path(temp)
            policy_path = root / "policy.json"
            atlas_path = root / "atlas.py"
            output_path = root / "out" / "context.json"
            policy_path.write_text(json.dumps(policy()), encoding="utf-8")
            atlas_path.write_text("# fictional provider\n", encoding="utf-8")
            argv = [
                "capture_context.py", "--policy", str(policy_path),
                "--atlas-module", str(atlas_path), "--out", str(output_path),
            ]
            stdout = io.StringIO()
            with mock.patch.object(c, "capture", return_value=packet), mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
                c.main()
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["schema"], c.SCHEMA)
            self.assertEqual(summary["judgment_count"], 1)
            self.assertEqual(summary["claim_count"], 0)
            self.assertNotIn("SECRET", stdout.getvalue())
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), packet)
            self.assertEqual(stat.S_IMODE(output_path.stat().st_mode), 0o600)
            before = output_path.read_bytes()
            with mock.patch.object(c, "capture", return_value=packet), mock.patch.object(sys, "argv", argv):
                with self.assertRaises(FileExistsError):
                    c.main()
            self.assertEqual(output_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
