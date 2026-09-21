#!/usr/bin/env python3
"""Read-only aggregate evaluation for the frozen three-case planning study.

This module consumes the append-only receipts produced by
``planning_receipts.py``.  It never writes a case, changes a source, calls a
model, or claims that the natural cohort is complete.  The evaluator is an
explicit report request, not a registry or a scheduler.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import stat
import sys
from collections.abc import Mapping
from typing import Any, Iterable, Sequence

try:
    from . import planning_receipts as receipts
except ImportError:  # pragma: no cover - exercised by the unittest discover path
    import planning_receipts as receipts


INPUT_SCHEMA = "planning-evaluation-input/v1"
REPORT_SCHEMA = "planning-evaluation-report/v1"
EXPECTED_CASES = 3
MAX_INPUT_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
LABEL_FIELDS = (
    "human_top_three_ids",
    "usefulness",
    "correction_burden",
    "human_supervision_minutes",
    "hard_failures",
)
CHAIN_PREFIX = ("source", "baseline", "proposal", "label")
MISS_PREFIXES = {
    ("missed",),
    ("source", "missed"),
    ("source", "baseline", "missed"),
}


class EvaluationError(ValueError):
    """A bounded evaluator error whose code contains no source values."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code else "INVALID_INPUT"
        super().__init__(self.code)


def _fail(code: str) -> None:
    raise EvaluationError(code)


def _finite(value: Any) -> None:
    """Reject non-finite values and non-JSON containers without recursion."""
    pending = [value]
    visited = 0
    while pending:
        visited += 1
        if visited > MAX_INPUT_BYTES:
            _fail("INPUT_TOO_LARGE")
        current = pending.pop()
        if isinstance(current, float) and not math.isfinite(current):
            _fail("NONFINITE_INPUT")
        if isinstance(current, Mapping):
            if any(not isinstance(key, str) for key in current):
                _fail("INVALID_INPUT")
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
        elif current is not None and not isinstance(current, (str, bool, int, float)):
            _fail("INVALID_INPUT")


def _bounded_json(value: Any, limit: int) -> bytes:
    _finite(value)
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        _fail("INVALID_INPUT")
    if len(raw) > limit:
        _fail("INPUT_TOO_LARGE")
    return raw


def _fields(value: Any, required: Iterable[str]) -> None:
    required_set = set(required)
    if not isinstance(value, Mapping) or set(value) != required_set:
        _fail("INVALID_FIELDS")


def _hex(value: Any) -> str:
    if not isinstance(value, str) or not receipts.HEX.fullmatch(value):
        _fail("INVALID_HASH")
    return value


def _reference_value(value: Any) -> tuple[Path, str]:
    _fields(value, ("path", "sha256"))
    path_value = value["path"]
    if not isinstance(path_value, str) or not path_value or len(path_value) > 4096:
        _fail("INVALID_PATH")
    path = Path(path_value)
    try:
        if not path.is_absolute() or path.resolve() != path:
            _fail("INVALID_PATH")
    except (OSError, ValueError):
        _fail("INVALID_PATH")
    return path, _hex(value["sha256"])


def _reference(value: Any) -> tuple[Path, str]:
    path, digest = _reference_value(value)
    try:
        mode = os.lstat(path).st_mode
    except (OSError, ValueError):
        _fail("INVALID_PATH")
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        _fail("INVALID_PATH")
    return path, digest


def _stable_file(path: Path) -> tuple[bytes, str]:
    try:
        first = receipts.read(path, MAX_INPUT_BYTES)
        first_hash = receipts.sha(first)
        second = receipts.read(path, MAX_INPUT_BYTES)
    except (OSError, ValueError, TypeError, KeyError):
        _fail("EVIDENCE_UNREADABLE")
    if first != second:
        _fail("CHANGED_EVIDENCE")
    return first, first_hash


def _design_snapshot(reference: Mapping[str, Any]) -> tuple[Path, str, Mapping[str, Any]]:
    path, expected_hash = _reference(reference)
    raw, actual_hash = _stable_file(path)
    if actual_hash != expected_hash:
        _fail("DESIGN_HASH_MISMATCH")
    try:
        design = receipts.parse(raw)
    except (ValueError, TypeError, RecursionError):
        _fail("INVALID_DESIGN")
    if not isinstance(design, Mapping):
        _fail("INVALID_DESIGN")
    return path, actual_hash, design


def _case_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096:
        _fail("INVALID_CASE_PATH")
    path = Path(value)
    try:
        resolved = path.resolve()
        mode = os.lstat(path).st_mode
        owner = os.lstat(path).st_uid
    except (OSError, ValueError):
        _fail("INVALID_CASE_PATH")
    if not path.is_absolute() or resolved != path or stat.S_ISLNK(mode):
        _fail("INVALID_CASE_PATH")
    if not stat.S_ISDIR(mode) or owner != os.getuid() or mode & 0o077:
        _fail("INVALID_CASE_PATH")
    return path


def _lock_is_present(case: Path) -> bool:
    lock = case / ".lock"
    try:
        mode = os.lstat(lock).st_mode
        owner = os.lstat(lock).st_uid
    except OSError:
        return False
    return (
        stat.S_ISREG(mode)
        and not stat.S_ISLNK(mode)
        and owner == os.getuid()
        and not mode & 0o077
    )


def _lock_exists(case: Path) -> bool:
    try:
        os.lstat(case / ".lock")
    except OSError:
        return False
    return True


def _receipt_paths(case: Path) -> list[Path]:
    paths = []
    for path in case.glob("[0-9]*-*.json"):
        paths.append(path)
        if len(paths) > 16:
            _fail("TOO_MANY_RECEIPTS")
    return sorted(paths, key=lambda item: item.name)


def _receipt_fingerprint(case: Path) -> tuple[tuple[str, str], ...]:
    result = []
    for path in _receipt_paths(case):
        raw, digest = _stable_file(path)
        del raw
        result.append((path.name, digest))
    return tuple(result)


def _load_case(case: Path) -> tuple[list[dict[str, Any]], str | None, dict[str, str]]:
    """Load one case without changing its persisted state."""
    paths = _receipt_paths(case)
    if not paths and not _lock_exists(case):
        # An empty private folder is equivalent to an unrecorded slot.  Do not
        # call receipts.locked here because it would create .lock as a side effect.
        return [], None, {}
    if not _lock_is_present(case):
        _fail("INVALID_CASE_EVIDENCE")
    try:
        # ``load`` validates the full persisted chain.  Fingerprints before
        # and after it make a concurrent append or replacement observable,
        # without taking a writer lock or creating any lock state.
        before = _receipt_fingerprint(case)
        chain, tail = receipts.load(case)
        after = _receipt_fingerprint(case)
        if before != after:
            _fail("CHANGED_EVIDENCE")
        hashes = {
            row["kind"] if row["kind"] != "label" else "label": digest
            for (name, digest), row in zip(after, chain)
            if name == f'{row["sequence"]:04d}-{row["kind"]}.json'
        }
    except EvaluationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        _fail("INVALID_CASE_EVIDENCE")
    return chain, tail, hashes


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except (OSError, ValueError):
        return False


def _validate_input(payload: Any) -> tuple[Path, str, Mapping[str, Any], list[str | None]]:
    _bounded_json(payload, MAX_INPUT_BYTES)
    _fields(payload, ("schema", "design_ref", "case_dirs"))
    if payload["schema"] != INPUT_SCHEMA:
        _fail("INVALID_SCHEMA")
    design_path, design_hash, design = _design_snapshot(payload["design_ref"])
    case_dirs = payload["case_dirs"]
    if not isinstance(case_dirs, list) or len(case_dirs) != EXPECTED_CASES:
        _fail("EXPECTED_THREE_CASES")
    resolved_seen: set[Path] = set()
    normalized: list[str | None] = []
    for entry in case_dirs:
        if entry is None:
            normalized.append(None)
            continue
        path = _case_path(entry)
        if path in resolved_seen:
            _fail("DUPLICATE_CASE_PATH")
        resolved_seen.add(path)
        normalized.append(str(path))
    return design_path, design_hash, design, normalized


def _receipt_hashes_for_report(hashes: Mapping[str, str]) -> dict[str, str]:
    # Fixed keys keep report shape stable and never expose receipt filenames.
    return {key: hashes[key] for key in ("source", "baseline", "proposal", "label", "missed") if key in hashes}


def _source_metadata(
    chain: list[dict[str, Any]],
    case: Path,
    design_path: Path,
    design_hash: str,
) -> tuple[str, str]:
    source = chain[0]
    data = source.get("data")
    if not isinstance(data, Mapping):
        _fail("INVALID_CASE_EVIDENCE")
    frozen_design = data.get("frozen_design")
    source_ref = data.get("source_ref")
    try:
        frozen_path, frozen_hash = _reference_value(frozen_design)
        _source_path, source_hash = _reference_value(source_ref)
    except EvaluationError:
        raise
    if not _same_path(frozen_path, design_path) or frozen_hash != design_hash:
        _fail("DESIGN_MISMATCH")
    # p.load has already checked the pinned artifact.  Reading it here gives
    # the evaluator a second stable snapshot for duplicate-source detection.
    artifact = case / "source-artifact.json"
    raw, artifact_hash = _stable_file(artifact)
    if artifact_hash != source_hash:
        _fail("CHANGED_EVIDENCE")
    try:
        parsed = receipts.parse(raw)
    except (ValueError, TypeError, RecursionError):
        _fail("INVALID_CASE_EVIDENCE")
    if not isinstance(parsed, Mapping):
        _fail("INVALID_CASE_EVIDENCE")
    encounter_id = data.get("encounter_id")
    if not isinstance(encounter_id, str) or not encounter_id.strip() or len(encounter_id) > 160:
        _fail("INVALID_CASE_EVIDENCE")
    return encounter_id, source_hash


def _validate_chain(
    chain: list[dict[str, Any]],
    case: Path,
    design_path: Path,
    design_hash: str,
) -> tuple[str, str | None, str | None]:
    if not chain:
        return "unknown", None, None
    kinds = tuple(row.get("kind") for row in chain)
    if kinds in MISS_PREFIXES:
        if kinds[0] == "source":
            encounter_id, source_hash = _source_metadata(chain, case, design_path, design_hash)
            return "missed", encounter_id, source_hash
        return "missed", None, None
    if not kinds or kinds[0] != "source":
        _fail("WRONG_CHAIN")
    if len(kinds) <= 3:
        if tuple(kinds) != CHAIN_PREFIX[: len(kinds)]:
            _fail("WRONG_CHAIN")
    elif tuple(kinds[:3]) != CHAIN_PREFIX[:3] or any(kind != "label" for kind in kinds[3:]):
        _fail("WRONG_CHAIN")
    encounter_id, source_hash = _source_metadata(chain, case, design_path, design_hash)
    if len(kinds) < 4:
        return "unlabelled", encounter_id, source_hash
    last = chain[-1]
    if last["kind"] != "label":
        return "unlabelled", encounter_id, source_hash
    label = last.get("data")
    if not isinstance(label, Mapping) or label.get("answer_reference") is None:
        return "unlabelled", encounter_id, source_hash
    if all(label.get(field) is not None for field in LABEL_FIELDS):
        return "complete", encounter_id, source_hash
    return "unlabelled", encounter_id, source_hash


def _summary(values: Sequence[int | float]) -> dict[str, Any]:
    if not values:
        return {"known_count": 0, "total": None, "mean": None}
    total = sum(values)
    return {"known_count": len(values), "total": total, "mean": total / len(values)}


def _slot(
    ordinal: int,
    case: Path | None,
    design_path: Path,
    design_hash: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if case is None:
        return (
            {
                "ordinal": ordinal,
                "state": "unknown",
                "receipt_count": 0,
                "tail_sha256": None,
                "evidence_hashes": {},
                "design_binding_verified": False,
            },
            {"state": "unknown", "label": None},
        )
    chain, tail, hashes = _load_case(case)
    state, encounter_id, source_hash = _validate_chain(chain, case, design_path, design_hash)
    if encounter_id is None and source_hash is None and state == "unknown":
        return (
            {
                "ordinal": ordinal,
                "state": "unknown",
                "receipt_count": 0,
                "tail_sha256": None,
                "evidence_hashes": {},
                "design_binding_verified": False,
            },
            {"state": "unknown", "label": None},
        )
    safe_hashes = _receipt_hashes_for_report(hashes)
    if source_hash is not None:
        safe_hashes["source_snapshot_sha256"] = source_hash
    result = {
        "ordinal": ordinal,
        "state": state,
        "receipt_count": len(chain),
        "tail_sha256": tail,
        "evidence_hashes": safe_hashes,
        "design_binding_verified": source_hash is not None,
    }
    if state == "missed":
        return result, {
            "state": state,
            "label": None,
            "encounter_id": encounter_id,
            "source_hash": source_hash,
        }
    if len(chain) < 3 or chain[2].get("kind") != "proposal":
        return result, {
            "state": state,
            "label": None,
            "encounter_id": encounter_id,
            "source_hash": source_hash,
        }
    baseline = chain[1].get("derived", {})
    proposal = chain[2].get("data", {})
    if not isinstance(baseline, Mapping) or not isinstance(proposal, Mapping):
        _fail("INVALID_CASE_EVIDENCE")
    baseline_ids = baseline.get("ordered_ids")
    proposal_ids = proposal.get("ordered_ids")
    if not isinstance(baseline_ids, list) or not isinstance(proposal_ids, list):
        _fail("INVALID_CASE_EVIDENCE")
    label_rows = [row for row in chain if row.get("kind") == "label"]
    label = label_rows[-1].get("data") if label_rows else None
    return result, {
        "state": state,
        "label": label if isinstance(label, Mapping) else None,
        "baseline_ids": baseline_ids,
        "proposal_ids": proposal_ids,
        "encounter_id": encounter_id,
        "source_hash": source_hash,
    }


def _report_slot_state(slot: Mapping[str, Any]) -> str:
    return str(slot.get("state"))


def evaluate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate exactly three explicit receipt slots without writing state."""
    design_path, design_hash, _design, case_strings = _validate_input(payload)
    slots: list[dict[str, Any]] = []
    internals: list[dict[str, Any]] = []
    encounter_ids: set[str] = set()
    source_hashes: set[str] = set()
    for ordinal, value in enumerate(case_strings, 1):
        case = None if value is None else Path(value)
        result, internal = _slot(ordinal, case, design_path, design_hash)
        encounter_id = internal.pop("encounter_id", None)
        source_hash = internal.pop("source_hash", None)
        # _slot intentionally keeps its public result free of source IDs and
        # paths; duplicate checks happen against private values from its chain.
        if encounter_id is not None:
            if encounter_id in encounter_ids:
                _fail("DUPLICATE_ENCOUNTER")
            encounter_ids.add(encounter_id)
        if source_hash is not None:
            if source_hash in source_hashes:
                _fail("DUPLICATE_SNAPSHOT")
            source_hashes.add(source_hash)
        slots.append(result)
        internals.append(internal)

    del encounter_ids, source_hashes

    # Re-read the external frozen design after all case evidence.  A changed
    # design must never be silently combined with the earlier snapshot.
    _raw_design, final_design_hash = _stable_file(design_path)
    del _raw_design
    if final_design_hash != design_hash:
        _fail("CHANGED_EVIDENCE")

    agreement_baseline = 0
    agreement_proposal = 0
    paired = 0
    useful = 0
    not_useful = 0
    unknown_usefulness = 0
    known_hard_failures = 0
    unknown_hard_failures = 0
    unknown_label_count = 0
    cases_with_hard_failures = 0
    correction_values: list[int | float] = []
    supervision_values: list[int | float] = []

    for internal in internals:
        label = internal.get("label")
        if not isinstance(label, Mapping) or label.get("answer_reference") is None:
            unknown_label_count += 1
            unknown_hard_failures += 1
            unknown_usefulness += 1
            continue
        human = label.get("human_top_three_ids")
        if human is not None:
            paired += 1
            agreement_baseline += len(set(human) & set(internal.get("baseline_ids", [])))
            agreement_proposal += len(set(human) & set(internal.get("proposal_ids", [])))
        usefulness = label.get("usefulness")
        if usefulness == "useful":
            useful += 1
        elif usefulness == "not_useful":
            not_useful += 1
        else:
            unknown_usefulness += 1
        hard_failures = label.get("hard_failures")
        if hard_failures is None:
            unknown_hard_failures += 1
        else:
            known_hard_failures += 1
            if hard_failures:
                cases_with_hard_failures += 1
        correction = label.get("correction_burden")
        if correction is not None:
            correction_values.append(correction)
        supervision = label.get("human_supervision_minutes")
        if supervision is not None:
            supervision_values.append(supervision)

    counts = {
        "expected3": EXPECTED_CASES,
        "recorded": sum(_report_slot_state(slot) != "unknown" for slot in slots),
        "missed": sum(_report_slot_state(slot) == "missed" for slot in slots),
        "unlabelled": sum(_report_slot_state(slot) == "unlabelled" for slot in slots),
        "complete": sum(_report_slot_state(slot) == "complete" for slot in slots),
        "unknown": sum(_report_slot_state(slot) == "unknown" for slot in slots),
    }
    aggregate_delta = agreement_proposal - agreement_baseline if paired else None
    hard_failures = {
        "known_count": known_hard_failures,
        "unknown_count": unknown_hard_failures,
        "unknown_label_count": unknown_label_count,
        "cases_with_failures": cases_with_hard_failures,
    }
    reasons: list[str] = []
    if counts["complete"] != EXPECTED_CASES:
        reasons.append("INCOMPLETE_LABELS")
    if counts["missed"] or counts["unknown"]:
        reasons.append("MISSING_OR_MISSED_SLOT")
    if paired != EXPECTED_CASES:
        reasons.append("MISSING_PAIRED_AGREEMENT")
    if hard_failures["unknown_count"]:
        reasons.append("HARD_FAILURE_UNKNOWN")
    if hard_failures["cases_with_failures"]:
        reasons.append("HARD_FAILURE_PRESENT")
    if useful != EXPECTED_CASES:
        reasons.append("NOT_ALL_USEFUL")
    if aggregate_delta is None or aggregate_delta <= 0:
        reasons.append("NO_STRICT_AGGREGATE_IMPROVEMENT")
    eligible = not reasons
    report = {
        "schema": REPORT_SCHEMA,
        "status": "evaluated",
        "design_sha256": design_hash,
        "expected3": counts["expected3"],
        "recorded": counts["recorded"],
        "missed": counts["missed"],
        "unlabelled": counts["unlabelled"],
        "complete": counts["complete"],
        "unknown": counts["unknown"],
        "counts": counts,
        "slots": slots,
        "agreement": {
            "baseline": agreement_baseline,
            "proposal": agreement_proposal,
            "paired_denominator": paired,
            "aggregate_delta": aggregate_delta,
            "baseline_mean": agreement_baseline / paired if paired else None,
            "proposal_mean": agreement_proposal / paired if paired else None,
        },
        "usefulness": {
            "useful": useful,
            "not_useful": not_useful,
            "unknown": unknown_usefulness,
        },
        "hard_failures": hard_failures,
        "hard_failure_known_count": known_hard_failures,
        "unknown_label_count": unknown_label_count,
        "correction_burden": _summary(correction_values),
        "human_supervision_minutes": _summary(supervision_values),
        "qualification": {
            "eligible_for_owner_review": eligible,
            "reason_codes": reasons,
            "automatic_win": False,
            "rollout_authorized": False,
            "cohort_completeness_verified": False,
            "real_model_superiority_claim": False,
        },
        "caveats": [
            "baseline_is_deadline_only",
            "evaluation_is_read_only",
            "no_live_rank_change",
            "owner_must_verify_natural_cohort_and_labels",
        ],
    }
    _bounded_json(report, MAX_OUTPUT_BYTES)
    return report


def _read_cli_input(path_value: str) -> Any:
    try:
        path = Path(path_value)
        if not path.is_absolute():
            path = Path.cwd() / path
        raw = receipts.read(path, MAX_INPUT_BYTES)
        return receipts.parse(raw)
    except (OSError, ValueError, TypeError, RecursionError):
        _fail("INVALID_INPUT_FILE")


class _NoStderrArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        _fail("INVALID_ARGUMENTS")


def _parse_cli(argv: Sequence[str]) -> argparse.Namespace:
    parser = _NoStderrArgumentParser(add_help=False)
    parser.add_argument("--input", required=True)
    try:
        return parser.parse_args(list(argv))
    except SystemExit as exc:
        _fail("INVALID_ARGUMENTS")
        raise AssertionError from exc


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parse_cli(sys.argv[1:] if argv is None else argv)
        report = evaluate(_read_cli_input(args.input))
        output = json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        sys.stdout.write(output + "\n")
        return 0
    except EvaluationError as exc:
        sys.stdout.write(json.dumps({"status": "refused", "error_code": exc.code}, separators=(",", ":")) + "\n")
        return 2
    except Exception:
        sys.stdout.write('{"status":"refused","error_code":"INVALID_INPUT"}\n')
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["EvaluationError", "evaluate", "main"]
