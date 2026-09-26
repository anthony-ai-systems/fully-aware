"""Read-only Fully Aware shared-briefing coordinator consumer.

The adapter joins two independent observations for one short-lived read:
the local Fully Aware situation brief and the host-pinned IRIS selected-reader
probe.  It does not write state, select a generation, or turn either source's
freshness into authority.

``generation_probe_entry`` is deliberately imported only when an observation
is requested.  The host runner supplies that module from its own pinned module
closure; this adapter never changes ``sys.path`` or imports a live repository.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import sys
import subprocess
from typing import Any, Mapping, Optional, Sequence

try:
    from . import situation_brief
except ImportError:  # Direct execution from tools/convergence.
    import situation_brief  # type: ignore


SCHEMA = "fully-aware-coordinator-consumer/v1"
BRIEF_SCHEMA = "situation-brief/v1"
PROBE_SCHEMA = "iris-selected-consumer-observation/v1"
INITIALIZATION_SCHEMA = "iris-selected-consumer-initialization/v1"
CONSUMER = "shared_briefing"
MAX_PRIVATE_CONFIG_BYTES = 128 * 1024
HEX64 = re.compile(r"^[0-9a-f]{64}$")
OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
SELECTION_FIELDS = {"schema", "revision", "operation_id", "mode", "manifest_sha256"}
SELECTION_SCHEMA = "iris-selection-reference/v1"
INITIALIZATION_STATES = {"no_selection", "selection_pending", "disabled"}
_MISSING = object()


class CoordinatorConsumerError(ValueError):
    """Closed, non-sensitive adapter validation failure."""


def _fail(code: str) -> None:
    raise CoordinatorConsumerError(code)


def _shape(value: Any, fields: set[str], code: str) -> None:
    if type(value) is not dict or set(value) != fields:
        _fail(code)


def _digest(value: Any, code: str = "digest_invalid") -> str:
    if type(value) is not str or HEX64.fullmatch(value) is None:
        _fail(code)
    return value


def _absolute_path(value: Any, code: str = "path_invalid") -> str:
    """Validate the lexical path contract; native readers validate the file."""
    if type(value) is not str or not value.startswith("/") or not value:
        _fail(code)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail(code)
    # Match the native private reader's physical-path boundary.  Do not
    # resolve the path here: the pinned helper owns race-resistant reads.
    parts = value.split("/")[1:]
    if any(part in {"", ".", ".."} for part in parts):
        _fail(code)
    return value


def _validate_configuration(configuration: Any) -> dict[str, Any]:
    _shape(configuration, {"schema", "probe_release", "consumer_config", "brief"},
           "configuration_shape_invalid")
    if configuration["schema"] != SCHEMA:
        _fail("configuration_schema_invalid")

    release = configuration["probe_release"]
    _shape(release, {"path", "sha256"}, "probe_release_shape_invalid")
    _absolute_path(release["path"], "probe_release_path_invalid")
    _digest(release["sha256"], "probe_release_digest_invalid")
    _absolute_path(configuration["consumer_config"], "consumer_config_path_invalid")

    brief = configuration["brief"]
    _shape(brief, {"boot_pack", "plans", "sweep_automation"}, "brief_config_shape_invalid")
    _absolute_path(brief["boot_pack"], "boot_pack_path_invalid")
    _absolute_path(brief["plans"], "plans_path_invalid")
    if brief["sweep_automation"] is not None:
        _absolute_path(brief["sweep_automation"], "sweep_automation_path_invalid")
    # Snapshot the closed scalar contract before building the brief.  The
    # caller's dictionary remains untouched, and a late caller mutation cannot
    # retarget the native probe after validation.
    return {
        "schema": SCHEMA,
        "probe_release": {
            "path": release["path"],
            "sha256": release["sha256"],
        },
        "consumer_config": configuration["consumer_config"],
        "brief": {
            "boot_pack": brief["boot_pack"],
            "plans": brief["plans"],
            "sweep_automation": brief["sweep_automation"],
        },
    }


def _validate_installation(installation: Any) -> dict[str, str]:
    _shape(installation, {"path", "sha256"}, "installation_shape_invalid")
    _absolute_path(installation["path"], "installation_path_invalid")
    _digest(installation["sha256"], "installation_digest_invalid")
    return {"path": installation["path"], "sha256": installation["sha256"]}


def _native_probe_entry() -> Any:
    """Load the host-supplied native helper without altering import paths."""
    try:
        return importlib.import_module("generation_probe_entry")
    except (ImportError, ModuleNotFoundError) as exc:
        raise CoordinatorConsumerError("native_probe_unavailable") from exc


def _read_private(helper: Any, path: str) -> bytes:
    try:
        raw = helper.read(path, MAX_PRIVATE_CONFIG_BYTES, private=True)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        raise CoordinatorConsumerError("private_configuration_unavailable") from exc
    if type(raw) is not bytes or not raw or len(raw) > MAX_PRIVATE_CONFIG_BYTES:
        _fail("private_configuration_invalid")
    return raw


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise CoordinatorConsumerError("selection_not_canonical") from exc


def _selection_reference(value: Any, *, mode: str) -> dict[str, Any]:
    """Validate the exact selected-generation reference and its scalar types."""
    _shape(value, SELECTION_FIELDS, "selection_shape_invalid")
    if value["schema"] != SELECTION_SCHEMA:
        _fail("selection_schema_invalid")
    revision = value["revision"]
    if type(revision) is not int or revision <= 0 or revision > 2_147_483_647:
        _fail("selection_revision_invalid")
    operation_id = value["operation_id"]
    if type(operation_id) is not str or OPERATION_ID.fullmatch(operation_id) is None:
        _fail("selection_operation_invalid")
    if value["mode"] != mode:
        _fail("selection_mode_invalid")
    manifest = value["manifest_sha256"]
    if mode == "active":
        if type(manifest) is not str or HEX64.fullmatch(manifest) is None:
            _fail("selection_manifest_invalid")
    elif manifest is not None:
        _fail("selection_manifest_invalid")
    # Return a fresh scalar-only projection for validation/canonicalization;
    # the native proof itself is never replaced with this value.
    return {
        "schema": value["schema"],
        "revision": revision,
        "operation_id": operation_id,
        "mode": value["mode"],
        "manifest_sha256": manifest,
    }


def _brief_selection(brief: Any) -> Mapping[str, Any]:
    if type(brief) is not dict or brief.get("schema") != BRIEF_SCHEMA:
        _fail("brief_schema_invalid")
    iris = brief.get("iris")
    if type(iris) is not dict:
        _fail("brief_iris_invalid")
    selection = iris.get("selection")
    if type(selection) is not dict:
        _fail("brief_selection_missing")
    return selection


def _exact_bool(value: Any, expected: bool, code: str) -> None:
    if type(value) is not bool or value is not expected:
        _fail(code)


def _require_active_brief(brief: Any) -> dict[str, Any]:
    selection = _brief_selection(brief)
    _exact_bool(selection.get("advertised"), True, "brief_selection_not_advertised")
    _exact_bool(selection.get("current"), True, "brief_selection_not_current")
    _exact_bool(selection.get("identity_consistent"), True,
                "brief_selection_identity_inconsistent")
    if selection.get("status") != "active":
        _fail("brief_selection_status_invalid")
    reference = selection.get("reference", _MISSING)
    if reference is _MISSING:
        _fail("brief_selection_reference_missing")
    return _selection_reference(reference, mode="active")


def _require_initializing_brief(brief: Any) -> None:
    """Require the actual brief's selected reader to be visibly non-active."""
    selection = _brief_selection(brief)
    _exact_bool(selection.get("advertised"), True, "brief_selection_not_advertised")
    _exact_bool(selection.get("current"), False, "brief_selection_current")
    _exact_bool(selection.get("identity_consistent"), False,
                "brief_selection_identity_consistent")
    if selection.get("status") != "unavailable":
        _fail("brief_selection_status_invalid")


def _validate_probe_common(proof: Any, *, initialize: bool, installation: Mapping[str, Any]) -> None:
    if type(proof) is not dict:
        _fail("native_proof_invalid")
    expected_schema = INITIALIZATION_SCHEMA if initialize else PROBE_SCHEMA
    if proof.get("schema") != expected_schema:
        _fail("native_proof_schema_invalid")
    if proof.get("consumer") != CONSUMER:
        _fail("native_proof_consumer_invalid")
    if proof.get("installation_sha256") != installation["sha256"]:
        _fail("native_proof_installation_mismatch")
    _exact_bool(proof.get("control_admission"), False, "native_proof_control_admission")
    _exact_bool(proof.get("source_acknowledgement"), False,
                "native_proof_source_acknowledgement")


def _validate_active_probe(proof: Any, brief_reference: Mapping[str, Any],
                           installation: Mapping[str, Any]) -> None:
    _validate_probe_common(proof, initialize=False, installation=installation)
    proof_reference = proof.get("selection", _MISSING)
    if proof_reference is _MISSING:
        _fail("native_proof_selection_missing")
    normalized = _selection_reference(proof_reference, mode="active")
    if _canonical(dict(brief_reference)) != _canonical(normalized):
        _fail("selection_mismatch")


def _validate_initialization_probe(proof: Any, installation: Mapping[str, Any]) -> None:
    _validate_probe_common(proof, initialize=True, installation=installation)
    if proof.get("authority") != "initialized_read_observation_only":
        _fail("native_initialization_authority_invalid")
    state = proof.get("state")
    if type(state) is not str or state not in INITIALIZATION_STATES:
        _fail("native_initialization_state_invalid")
    selection = proof.get("selection", _MISSING)
    if selection is _MISSING:
        _fail("native_initialization_selection_missing")
    if state in {"no_selection", "selection_pending"}:
        if selection is not None:
            _fail("native_initialization_selection_invalid")
    else:
        _selection_reference(selection, mode="disabled")


def _run_probe(configuration: Mapping[str, Any], *, initialize: bool,
               installation: Mapping[str, Any], helper: Any) -> Any:
    release = configuration["probe_release"]
    # Read the private release and consumer config with the native helper's
    # race-resistant reader before invoking its child.  The helper.run call
    # independently rechecks the release/installation and the child rechecks
    # the consumer config; these reads are not saved proof.
    release_raw = _read_private(helper, release["path"])
    if hashlib.sha256(release_raw).hexdigest() != release["sha256"]:
        _fail("probe_release_digest_mismatch")
    _read_private(helper, configuration["consumer_config"])
    try:
        return helper.run(
            release_path=release["path"],
            expected_release_sha256=release["sha256"],
            installation=installation["path"],
            installation_sha256=installation["sha256"],
            consumer_config=configuration["consumer_config"],
            consumer=CONSUMER,
            initialize=initialize,
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            OverflowError, RecursionError, RuntimeError, subprocess.SubprocessError) as exc:
        raise CoordinatorConsumerError("native_probe_unavailable") from exc


def _read_current_validated(configuration: Mapping[str, Any], *, initialize: bool,
                            installation: Mapping[str, Any]) -> dict[str, Any]:
    # The brief is deliberately built before any native probe call.  A generic
    # brief fallback or malformed result cannot be papered over by native proof.
    brief_configuration = configuration["brief"]
    try:
        brief = situation_brief.build_brief(
            brief_configuration["boot_pack"],
            brief_configuration["plans"],
            sweep_automation=brief_configuration["sweep_automation"],
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            OverflowError, RecursionError, RuntimeError) as exc:
        raise CoordinatorConsumerError("brief_unavailable") from exc

    brief_reference: Optional[dict[str, Any]] = None
    if initialize:
        _require_initializing_brief(brief)
    else:
        brief_reference = _require_active_brief(brief)

    helper = _native_probe_entry()
    proof = _run_probe(configuration, initialize=initialize,
                        installation=installation, helper=helper)
    if initialize:
        _validate_initialization_probe(proof, installation)
    else:
        assert brief_reference is not None
        _validate_active_probe(proof, brief_reference, installation)
    # Keep the native object exactly as returned.  In particular, do not add
    # brief fields to it, normalize timestamps, or accept a saved report.
    return {"brief": brief, "proof": proof}


def read_current(configuration: dict, *, initialize: bool,
                installation: dict) -> dict[str, Any]:
    """Build the actual shared brief and return it beside native proof."""
    if type(initialize) is not bool:
        _fail("initialize_invalid")
    closed_configuration = _validate_configuration(configuration)
    closed_installation = _validate_installation(installation)
    return _read_current_validated(closed_configuration, initialize=initialize,
                                   installation=closed_installation)


def coordinator_observe(configuration: dict, *, initialize: bool,
                        installation: dict) -> Any:
    """Host-runner protocol: return only unchanged native proof."""
    return read_current(configuration, initialize=initialize,
                        installation=installation)["proof"]


def _json_configuration(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[Any, Any]]) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for key, value in items:
            if key in result:
                _fail("configuration_duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                CoordinatorConsumerError("configuration_nonfinite")),
        )
    except UnicodeDecodeError as exc:
        raise CoordinatorConsumerError("configuration_utf8_invalid") from exc
    except json.JSONDecodeError as exc:
        raise CoordinatorConsumerError("configuration_json_invalid") from exc
    if type(value) is not dict:
        _fail("configuration_shape_invalid")
    return value


def _load_cli_configuration(path: str, expected_sha256: str, helper: Any) -> dict[str, Any]:
    _absolute_path(path, "configuration_path_invalid")
    _digest(expected_sha256, "configuration_digest_invalid")
    raw = _read_private(helper, path)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        _fail("configuration_digest_mismatch")
    return _json_configuration(raw)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", required=True)
    parser.add_argument("--configuration-sha256", required=True)
    parser.add_argument("--installation", required=True)
    parser.add_argument("--installation-sha256", required=True)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args(argv)
    try:
        helper = _native_probe_entry()
        configuration = _load_cli_configuration(
            args.configuration, args.configuration_sha256, helper
        )
        installation = {"path": args.installation, "sha256": args.installation_sha256}
        result = read_current(configuration, initialize=args.initialize,
                              installation=installation)
        if args.format == "markdown":
            output = situation_brief.render_markdown(result["brief"])
        else:
            output = json.dumps(result, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False)
        sys.stdout.write(output)
        sys.stdout.write("\n")
        return 0
    except (CoordinatorConsumerError, OSError, ValueError, TypeError, KeyError,
            AttributeError, OverflowError, RecursionError, RuntimeError):
        # Keep CLI failures closed and free of private paths/tracebacks.
        sys.stderr.write("fully-aware coordinator consumer unavailable\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
