"""Interface tests for the bounded shared-briefing coordinator consumer.

These tests use the real ``situation_brief.build_brief`` through a spy and a
synthetic loopback response set.  The native helper is a small module-shaped
stand-in: no retained child process, IRIS service, or live installation is
claimed by this suite.  Native-child coverage remains owned by the IRIS probe
tests.
"""

import copy
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import situation_brief as brief
import coordinator_consumer as consumer


NOW = dt.datetime(2026, 9, 20, 18, 0, tzinfo=dt.timezone.utc)
NOW_TEXT = "2026-09-20T18:00:00Z"
RELEASE_BYTES = b"pinned release fixture"
RELEASE_SHA = hashlib.sha256(RELEASE_BYTES).hexdigest()
INSTALLATION_SHA = "b" * 64
SELECTION = {
    "schema": "iris-selection-reference/v1",
    "revision": 1,
    "operation_id": "a" * 32,
    "mode": "active",
    "manifest_sha256": "f" * 64,
}


def _write_json(root: Path, name: str, value: object) -> str:
    path = root / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def _configuration(root: Path) -> dict:
    return {
        "schema": consumer.SCHEMA,
        "probe_release": {"path": "/private/probe-release.json", "sha256": RELEASE_SHA},
        "consumer_config": "/private/shared-briefing.json",
        "brief": {
            "boot_pack": _write_json(
                root, "boot-pack.json",
                {"schema": "boot-pack/v1", "generated_at": NOW_TEXT, "warnings": [],
                 "open_items": [], "sections": {"decision_queue": {"items": []}}},
            ),
            "plans": _write_json(
                root, "plans.json",
                {"generated": NOW_TEXT, "lanes": [
                    {"name": "fully-aware-convergence", "step": "review", "health": "active",
                     "updated": NOW_TEXT, "waiting_on_anthony": [], "blocked": []},
                    {"name": "iris", "step": "serve", "health": "active", "updated": NOW_TEXT},
                    {"name": "autonomous-operators", "step": "test", "health": "active",
                     "updated": NOW_TEXT},
                ]},
            ),
            "sweep_automation": None,
        },
    }


def _endpoints(*, active: bool) -> object:
    """Reuse the maintained situation-brief fixture shapes, with fixed clocks."""
    # Importing the existing fixture avoids creating a second, subtly different
    # IRIS payload contract in this adapter test.
    import test_situation_brief as source

    values = {
        "/data/board.json": source.board(),
        "/healthz": source.health(),
        "/focus.json": source.focus(),
        "/local-agent.json": source.local_agent(),
        "/priority.json": source.priority_payload(),
    }
    if active:
        for path in ("/data/board.json", "/healthz", "/focus.json", "/priority.json"):
            values[path]["selection"] = copy.deepcopy(SELECTION)
    else:
        # A native no-selector initialization advertises an unavailable
        # selected state through the priority endpoint.  Other endpoints are
        # legacy-shaped, so situation_brief must still expose the join failure.
        values["/priority.json"]["selection"] = None
    return source.Endpoints(values)


class FakeNativeProbe(types.ModuleType):
    """Module-shaped native helper; no child/live proof is implied."""

    def __init__(self, release_path: str):
        super().__init__("generation_probe_entry")
        self.release_path = release_path
        self.calls = []
        self.proof = None

    def read(self, path, limit, *, private=False):
        self.calls.append(("read", path, limit, private))
        if path == self.release_path:
            return RELEASE_BYTES
        return b"private consumer configuration"

    def run(self, **kwargs):
        self.calls.append(("run", kwargs))
        return self.proof


class CoordinatorConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fa-coordinator-consumer-")
        self.root = Path(self.temp.name).resolve()
        self.configuration = _configuration(self.root)
        self.installation = {"path": "/private/installation.json", "sha256": INSTALLATION_SHA}
        self.helper = FakeNativeProbe(self.configuration["probe_release"]["path"])
        self.previous_helper = sys.modules.get("generation_probe_entry")
        sys.modules["generation_probe_entry"] = self.helper
        self.addCleanup(self._restore_helper)
        self.addCleanup(self.temp.cleanup)

    def _restore_helper(self):
        if self.previous_helper is None:
            sys.modules.pop("generation_probe_entry", None)
        else:
            sys.modules["generation_probe_entry"] = self.previous_helper

    def _active_proof(self):
        return {
            "schema": consumer.PROBE_SCHEMA,
            "consumer": consumer.CONSUMER,
            "authority": "selected_read_observation_only",
            "installation_sha256": INSTALLATION_SHA,
            "selection": copy.deepcopy(SELECTION),
            "control_admission": False,
            "source_acknowledgement": False,
            "source_freshness_checked": False,
        }

    def _initialization_proof(self, state="no_selection"):
        return {
            "schema": consumer.INITIALIZATION_SCHEMA,
            "consumer": consumer.CONSUMER,
            "authority": "initialized_read_observation_only",
            "installation_sha256": INSTALLATION_SHA,
            "state": state,
            "selection": None if state != "disabled" else {
                "schema": "iris-selection-reference/v1", "revision": 1,
                "operation_id": "a" * 32, "mode": "disabled", "manifest_sha256": None,
            },
            "control_admission": False,
            "source_acknowledgement": False,
        }

    def _call(self, *, initialize=False, active=True):
        endpoints = _endpoints(active=active)
        self.helper.proof = self._active_proof() if active else self._initialization_proof()
        with mock.patch.object(brief, "_now", side_effect=lambda value=None: NOW), \
                mock.patch.object(brief, "fetch_endpoint", side_effect=endpoints), \
                mock.patch.object(brief, "build_brief", wraps=brief.build_brief) as build:
            result = consumer.read_current(self.configuration, initialize=initialize,
                                            installation=self.installation)
        return result, build

    def test_active_path_calls_real_build_and_preserves_native_proof(self):
        configuration_before = copy.deepcopy(self.configuration)
        installation_before = copy.deepcopy(self.installation)
        result, build = self._call()
        self.assertEqual(build.call_count, 1)
        build.assert_called_once_with(
            self.configuration["brief"]["boot_pack"],
            self.configuration["brief"]["plans"],
            sweep_automation=None,
        )
        self.assertIs(result["proof"], self.helper.proof)
        self.assertIs(result["proof"]["selection"], self.helper.proof["selection"])
        selected = result["brief"]["iris"]["selection"]
        self.assertIs(selected["advertised"], True)
        self.assertIs(selected["current"], True)
        self.assertIs(selected["identity_consistent"], True)
        self.assertEqual(selected["status"], "active")
        self.assertEqual(selected["reference"], result["proof"]["selection"])
        self.assertEqual(self.helper.calls[-1][0], "run")
        self.assertEqual(self.helper.calls[-1][1]["consumer"], "shared_briefing")
        self.assertEqual(self.configuration, configuration_before)
        self.assertEqual(self.installation, installation_before)

    def test_initialization_requires_actual_non_active_brief_and_precise_native_state(self):
        result, build = self._call(initialize=True, active=False)
        self.assertEqual(build.call_count, 1)
        self.assertIs(result["proof"], self.helper.proof)
        selected = result["brief"]["iris"]["selection"]
        self.assertIs(selected["advertised"], True)
        self.assertIs(selected["current"], False)
        self.assertIs(selected["identity_consistent"], False)
        self.assertEqual(selected["status"], "unavailable")
        self.assertEqual(result["proof"]["state"], "no_selection")

    def test_coordinator_protocol_returns_only_the_same_native_proof(self):
        self.helper.proof = self._active_proof()
        endpoints = _endpoints(active=True)
        with mock.patch.object(brief, "_now", side_effect=lambda value=None: NOW), \
                mock.patch.object(brief, "fetch_endpoint", side_effect=endpoints):
            proof = consumer.coordinator_observe(
                self.configuration, initialize=False, installation=self.installation)
        self.assertIs(proof, self.helper.proof)
        self.assertNotIn("brief", proof)

    def test_rejects_missing_or_extra_configuration_fields(self):
        for mutation in (
            lambda value: value.pop("brief"),
            lambda value: value.__setitem__("unexpected", True),
            lambda value: value["brief"].__setitem__("unexpected", None),
        ):
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(self.configuration)
                mutation(candidate)
                with self.assertRaises(consumer.CoordinatorConsumerError):
                    consumer.read_current(candidate, initialize=False,
                                          installation=self.installation)

    def test_rejects_false_typed_identity_and_mismatched_proof_selection(self):
        endpoints = _endpoints(active=True)
        self.helper.proof = self._active_proof()
        self.helper.proof["selection"]["revision"] = True
        with mock.patch.object(brief, "_now", side_effect=lambda value=None: NOW), \
                mock.patch.object(brief, "fetch_endpoint", side_effect=endpoints):
            with self.assertRaises(consumer.CoordinatorConsumerError):
                consumer.read_current(self.configuration, initialize=False,
                                      installation=self.installation)

        self.helper.proof = self._active_proof()
        self.helper.proof["selection"]["operation_id"] = "b" * 32
        endpoints = _endpoints(active=True)
        with mock.patch.object(brief, "_now", side_effect=lambda value=None: NOW), \
                mock.patch.object(brief, "fetch_endpoint", side_effect=endpoints):
            with self.assertRaises(consumer.CoordinatorConsumerError):
                consumer.read_current(self.configuration, initialize=False,
                                      installation=self.installation)

    def test_generic_brief_fallback_cannot_substitute_for_probe_proof(self):
        self.helper.proof = self._active_proof()
        with mock.patch.object(brief, "build_brief", return_value=brief._fallback(NOW)):
            with self.assertRaises(consumer.CoordinatorConsumerError):
                consumer.read_current(self.configuration, initialize=False,
                                      installation=self.installation)
        self.assertFalse(any(call[0] == "run" for call in self.helper.calls))

    def test_release_hash_mismatch_and_input_mutation_are_rejected_without_mutation(self):
        candidate = copy.deepcopy(self.configuration)
        original = copy.deepcopy(candidate)
        candidate["probe_release"]["sha256"] = "c" * 64
        with mock.patch.object(brief, "build_brief", return_value={
                "schema": consumer.BRIEF_SCHEMA,
                "iris": {"selection": {"advertised": True, "current": True,
                                        "identity_consistent": True, "status": "active",
                                        "reference": copy.deepcopy(SELECTION)}}}):
            with self.assertRaises(consumer.CoordinatorConsumerError):
                consumer.read_current(candidate, initialize=False,
                                      installation=self.installation)
        self.assertEqual(candidate, {**original, "probe_release": {
            "path": original["probe_release"]["path"], "sha256": "c" * 64}})
        self.assertFalse(any(call[0] == "run" for call in self.helper.calls))

    def test_initialization_rejects_generic_native_fallback(self):
        self.helper.proof = {"schema": "iris-selected-consumer-unavailable/v1",
                             "reason": "selected_consumer_unavailable"}
        endpoints = _endpoints(active=False)
        with mock.patch.object(brief, "_now", side_effect=lambda value=None: NOW), \
                mock.patch.object(brief, "fetch_endpoint", side_effect=endpoints):
            with self.assertRaises(consumer.CoordinatorConsumerError):
                consumer.read_current(self.configuration, initialize=True,
                                      installation=self.installation)

    def test_native_timeout_is_closed_without_exposing_child_arguments(self):
        endpoints = _endpoints(active=True)
        with mock.patch.object(brief, "_now", side_effect=lambda value=None: NOW), \
                mock.patch.object(brief, "fetch_endpoint", side_effect=endpoints), \
                mock.patch.object(self.helper, "run", side_effect=subprocess.TimeoutExpired(
                    ["private-child-arguments"], 5)):
            with self.assertRaisesRegex(consumer.CoordinatorConsumerError,
                                        "^native_probe_unavailable$"):
                consumer.read_current(self.configuration, initialize=False,
                                      installation=self.installation)

    def test_cli_reads_private_configuration_with_native_reader(self):
        config_path = self.root / "coordinator.json"
        raw = json.dumps(self.configuration, sort_keys=True).encode("utf-8")
        config_path.write_bytes(raw)
        original_read = self.helper.read

        def read(path, limit, *, private=False):
            if path == str(config_path):
                self.helper.calls.append(("read-cli", path, limit, private))
                return raw
            return original_read(path, limit, private=private)

        self.helper.read = read
        self.helper.proof = self._active_proof()
        endpoints = _endpoints(active=True)
        stdout = io.StringIO()
        with mock.patch.object(brief, "_now", side_effect=lambda value=None: NOW), \
                mock.patch.object(brief, "fetch_endpoint", side_effect=endpoints), \
                mock.patch("sys.stdout", stdout):
            status = consumer.main([
                "--configuration", str(config_path),
                "--configuration-sha256", hashlib.sha256(raw).hexdigest(),
                "--installation", self.installation["path"],
                "--installation-sha256", self.installation["sha256"],
                "--format", "json",
            ])
        self.assertEqual(status, 0)
        encoded = json.loads(stdout.getvalue())
        self.assertEqual(encoded["proof"], self.helper.proof)
        self.assertTrue(any(call[0] == "read-cli" and call[3] is True
                            for call in self.helper.calls))


if __name__ == "__main__":
    unittest.main()
