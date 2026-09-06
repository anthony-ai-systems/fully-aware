#!/usr/bin/env python3
"""Build a bounded, local-only operator view from three JSON snapshots.

The inputs are already-produced artifacts.  This module deliberately does not
discover paths, run producers, call subprocesses, or infer authority from a
reported status.  The same pure view model feeds both JSON and Markdown output.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


WORK_VIEW_SCHEMA = "work-view/v1"
BOOT_PACK_SCHEMA = "boot-pack/v1"
MAX_INPUT_BYTES = 1024 * 1024
CLAYTON_MAX_AGE_SECONDS = 600
SNAPSHOT_MAX_AGE_SECONDS = 129600
MAX_CLAYTON_TEXT = 200
MAX_NOTICE_COUNT = 20
MAX_NOTICE_TEXT = 240
MAX_WARNING_COUNT = 10
MAX_WARNING_TEXT = 240
MAX_LANE_COUNT = 100
MAX_LANE_LIST_COUNT = 10
UTC = _datetime.timezone.utc
SOURCE_NAMES = ("clayton", "boot_pack", "plans")
PROFILE_NAMES = ("a-lane.json", "b-lane.json")
KNOWN_HEALTH = {"active", "waiting", "stalled", "complete"}
KNOWN_GATE_STATES = {"RUNNING", "IDLE", "HALTED"}
HALTED_REPORTS = {"HALTED", "HALTED (fleet-kill present)"}
HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_BIDI = {
    "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
    "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069",
}

# These are stable, non-sensitive issue identifiers.  Loader issues are also
# used in observations, so arbitrary exception text can never cross the API.
READ_ISSUES = {
    "unconfigured", "missing", "symlink", "not_regular", "oversize",
    "read_error", "utf8", "malformed_json", "duplicate_key",
    "nonfinite_number", "not_object",
}


class _IssueCollector:
    def __init__(self) -> None:
        self.items: List[str] = []

    def add(self, code: str) -> None:
        if code not in self.items:
            if len(self.items) < 64:
                self.items.append(code)
            elif self.items[-1] != "additional_issues_omitted":
                self.items.append("additional_issues_omitted")


class _LoadIssue(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WorkViewError(Exception):
    """A safe, fixed-code CLI or output error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _strict_timestamp(value: Any) -> Tuple[Optional[str], Optional[_datetime.datetime]]:
    """Return the original clean timestamp and aware UTC value, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()
    try:
        parsed = _datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, RuntimeError):
        return None, None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, None
    try:
        return text, parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None, None


def _coerce_now(value: Any) -> _datetime.datetime:
    if isinstance(value, _datetime.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return value.astimezone(UTC)
    text, parsed = _strict_timestamp(value)
    if parsed is None:
        raise ValueError("now must be a timezone-aware ISO timestamp")
    return parsed


def _utc_text(value: _datetime.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _source_time(
    data: Mapping[str, Any],
    field: str,
    now: _datetime.datetime,
    threshold: int,
    issues: _IssueCollector,
) -> Tuple[Optional[str], Optional[int], str]:
    if field not in data or data.get(field) is None:
        issues.add("source_timestamp_missing")
        return None, None, "unknown"
    source_text, source_dt = _strict_timestamp(data.get(field))
    if source_dt is None:
        issues.add("source_timestamp_invalid")
        return None, None, "unknown"
    delta = (now - source_dt).total_seconds()
    age = int(delta)
    if delta < -60:
        freshness = "future"
    elif delta > threshold:
        freshness = "stale"
    else:
        freshness = "fresh"
    return source_text, age, freshness


def _observation(
    name: str,
    observations: Optional[Mapping[str, Any]],
    issues: _IssueCollector,
) -> Dict[str, Optional[str]]:
    raw: Any = observations.get(name) if isinstance(observations, Mapping) else None
    if not isinstance(raw, Mapping):
        return {"sha256": None, "observed_at": None, "read_error": None}

    digest = raw.get("sha256")
    if digest is not None and (not isinstance(digest, str) or not HEX64.fullmatch(digest)):
        issues.add("observation_digest_invalid")
        digest = None
    elif isinstance(digest, str):
        digest = digest.lower()

    observed_at = raw.get("observed_at")
    if observed_at is not None:
        observed_at, observed_dt = _strict_timestamp(observed_at)
        if observed_dt is None:
            issues.add("observation_timestamp_invalid")
            observed_at = None

    read_error = raw.get("read_error")
    if read_error is not None and (not isinstance(read_error, str) or read_error not in READ_ISSUES):
        issues.add("observation_issue_invalid")
        read_error = "read_error"
    return {"sha256": digest, "observed_at": observed_at, "read_error": read_error}


def _source_base(
    name: str,
    data: Any,
    now: _datetime.datetime,
    threshold: int,
    timestamp_field: str,
    observations: Optional[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], _IssueCollector]:
    issues = _IssueCollector()
    obs = _observation(name, observations, issues)
    if data is None:
        read_error = obs["read_error"]
        availability = read_error or "unconfigured"
        if availability not in {"unconfigured", "missing"}:
            availability = "invalid"
        issues.add(read_error or availability)
        return {
            "availability": availability,
            "source_timestamp": None,
            "age_seconds": None,
            "freshness": "unknown",
            "coverage": "unknown",
            "issues": issues.items,
            "sha256": obs["sha256"],
            "observed_at": obs["observed_at"],
            "projection": {},
        }, issues

    if not isinstance(data, Mapping):
        issues.add("source_not_object")
        return {
            "availability": "invalid",
            "source_timestamp": None,
            "age_seconds": None,
            "freshness": "unknown",
            "coverage": "unknown",
            "issues": issues.items,
            "sha256": obs["sha256"],
            "observed_at": obs["observed_at"],
            "projection": {},
        }, issues

    source_timestamp, age_seconds, freshness = _source_time(
        data, timestamp_field, now, threshold, issues
    )
    return {
        "availability": "available",
        "source_timestamp": source_timestamp,
        "age_seconds": age_seconds,
        "freshness": freshness,
        "coverage": "partial",
        "issues": issues.items,
        "sha256": obs["sha256"],
        "observed_at": obs["observed_at"],
        "projection": {},
    }, issues


def _clean_text(
    value: Any,
    limit: int,
    issues: _IssueCollector,
    code: str,
) -> Optional[str]:
    if not isinstance(value, str):
        issues.add(code + "_invalid")
        return None
    chars: List[str] = []
    for char in value:
        category = unicodedata.category(char)
        if char in _BIDI or category == "Cf":
            continue
        if char in "\r\n\t" or category.startswith("Z"):
            chars.append(" ")
        elif category.startswith("C"):
            continue
        else:
            chars.append(char)
    text = re.sub(r"\s+", " ", "".join(chars)).strip()
    if len(text) > limit:
        issues.add(code + "_truncated")
        text = text[:limit].rstrip()
    if not text and value:
        issues.add(code + "_empty")
        return None
    return text


def _bounded_text_list(
    value: Any,
    max_items: int,
    max_chars: int,
    issues: _IssueCollector,
    code: str,
) -> Tuple[List[str], int]:
    if value is None:
        return [], 0
    if not isinstance(value, list):
        issues.add(code + "_invalid")
        return [], 0
    kept: List[str] = []
    invalid = 0
    for item in value:
        text = _clean_text(item, max_chars, issues, code)
        if text is None:
            invalid += 1
            continue
        kept.append(text)
    dropped = max(0, len(kept) - max_items) + invalid
    if len(kept) > max_items:
        issues.add(code + "_list_truncated")
        kept = kept[:max_items]
    return kept, dropped


def _nonnegative_int(value: Any, issues: _IssueCollector, code: str) -> Optional[int]:
    if value is None:
        return None
    if type(value) is int and value >= 0:
        return value
    issues.add(code + "_invalid")
    return None


def _valid_hex(value: Any, pattern: re.Pattern[str], issues: _IssueCollector, code: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        issues.add(code + "_invalid")
        return None
    return value.lower()


def _current_projection(current: Mapping[str, Any], issues: _IssueCollector) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for field in ("item", "source_item_id", "brief_id", "kind", "model"):
        if field in current:
            value = _clean_text(current[field], MAX_CLAYTON_TEXT, issues, "current_" + field)
            if value is not None:
                result[field] = value
    if "started" in current:
        started_text, started_dt = _strict_timestamp(current.get("started"))
        if started_dt is None:
            issues.add("current_started_invalid")
        else:
            result["started"] = started_text
    return result


def _installation_projection(data: Mapping[str, Any], issues: _IssueCollector) -> Dict[str, Any]:
    repo_sha = _valid_hex(data.get("repository_checkout_sha"), HEX40, issues, "repository_checkout_sha")
    deployed_sha = _valid_hex(data.get("deployed_checkout_sha"), HEX40, issues, "deployed_checkout_sha")

    observed_raw = data.get("installed_profile_hashes")
    observed: Dict[str, Optional[str]] = {}
    if observed_raw is not None and not isinstance(observed_raw, Mapping):
        issues.add("installed_profile_hashes_invalid")
        observed_raw = {}
    for name in PROFILE_NAMES:
        observed[name] = _valid_hex(
            observed_raw.get(name) if isinstance(observed_raw, Mapping) else None,
            HEX64,
            issues,
            "installed_profile_hash_" + name.replace(".", "_").replace("-", "_"),
        )

    receipt_key_present = "installed_source_receipt" in data
    receipt_raw = data.get("installed_source_receipt")
    receipt_present = receipt_key_present and receipt_raw is not None
    receipt_valid = False
    receipt: Optional[Dict[str, Any]] = None
    if receipt_present:
        if not isinstance(receipt_raw, Mapping):
            issues.add("install_receipt_invalid")
            receipt = None
        else:
            receipt = {}
            receipt_valid = True
            if "source_commit" in receipt_raw:
                value = _valid_hex(receipt_raw.get("source_commit"), HEX40, issues, "receipt_source_commit")
                if value is not None:
                    receipt["source_commit"] = value
                else:
                    receipt_valid = False
            if "deployed_checkout_sha" in receipt_raw:
                value = _valid_hex(
                    receipt_raw.get("deployed_checkout_sha"), HEX40, issues, "receipt_deployed_checkout_sha"
                )
                if value is not None:
                    receipt["deployed_checkout_sha"] = value
                else:
                    receipt_valid = False
            if "installed_at" in receipt_raw:
                installed_text, installed_dt = _strict_timestamp(receipt_raw.get("installed_at"))
                if installed_dt is None:
                    issues.add("receipt_installed_at_invalid")
                    receipt_valid = False
                else:
                    receipt["installed_at"] = installed_text
            if "profile_hashes" in receipt_raw:
                profile_raw = receipt_raw.get("profile_hashes")
                if not isinstance(profile_raw, Mapping):
                    issues.add("receipt_profile_hashes_invalid")
                    receipt_valid = False
                else:
                    cleaned_profiles: Dict[str, str] = {}
                    for name in PROFILE_NAMES:
                        if name in profile_raw:
                            value = _valid_hex(
                                profile_raw.get(name),
                                HEX64,
                                issues,
                                "receipt_profile_hash_" + name.replace(".", "_").replace("-", "_"),
                            )
                            if value is not None:
                                cleaned_profiles[name] = value
                            else:
                                receipt_valid = False
                    if cleaned_profiles:
                        receipt["profile_hashes"] = cleaned_profiles
            if not receipt:
                receipt_valid = False
                issues.add("install_receipt_invalid")
    else:
        issues.add("install_receipt_unavailable")

    consistency = "unknown"
    if receipt_present and receipt_valid and receipt:
        receipt_profiles = receipt.get("profile_hashes")
        if isinstance(receipt_profiles, Mapping):
            mismatch = False
            complete = True
            compared = 0
            for name in PROFILE_NAMES:
                left = observed.get(name)
                right = receipt_profiles.get(name)
                if left is None or right is None:
                    complete = False
                else:
                    compared += 1
                    if left != right:
                        mismatch = True
            if mismatch:
                consistency = "mismatch"
            elif complete and compared == len(PROFILE_NAMES):
                required = ("source_commit", "deployed_checkout_sha", "installed_at")
                receipt_sha = receipt.get("deployed_checkout_sha")
                if all(receipt.get(key) for key in required) and repo_sha and deployed_sha:
                    consistency = ("consistent" if repo_sha == deployed_sha == receipt_sha == receipt["source_commit"] else "mismatch")
    known_shas = [v for v in (repo_sha, deployed_sha, (receipt or {}).get("source_commit"), (receipt or {}).get("deployed_checkout_sha")) if v]
    if len(set(known_shas)) > 1:
        consistency = "mismatch"
    if consistency == "consistent":
        _, installed = _strict_timestamp((receipt or {}).get("installed_at"))
        _, rendered = _strict_timestamp(data.get("rendered_at"))
        if not rendered or not installed or (installed - rendered).total_seconds() > 60:
            consistency = "unknown"
            issues.add("receipt_time_unverified")

    return {
        "repository_checkout_sha": repo_sha,
        "deployed_checkout_sha": deployed_sha,
        "observed_profile_hashes": observed,
        "receipt_presence": ("reported" if receipt_valid else "invalid") if receipt_present else "unavailable",
        "receipt": receipt,
        "installation_consistency": consistency,
        "readiness": "unverified",
    }


def _project_clayton(
    data: Mapping[str, Any],
    source: Dict[str, Any],
    issues: _IssueCollector,
) -> None:
    structural = True
    if "schema" in data:
        issues.add("unexpected_schema")
        structural = False
    if not isinstance(data.get("rendered_at"), str):
        issues.add("rendered_at_invalid")
        structural = False
    state = data.get("state")
    if not isinstance(state, str):
        issues.add("reported_state_invalid")
        structural = False
        reported_state = None
    elif state in HALTED_REPORTS:
        reported_state = "HALTED"
    elif state in {"RUNNING", "IDLE"}:
        reported_state = state
    else:
        issues.add("reported_state_unrecognized")
        reported_state = None

    current_raw = data.get("current")
    if "current" not in data or (current_raw is not None and not isinstance(current_raw, Mapping)):
        issues.add("current_invalid")
        structural = False
        current = None
    elif current_raw is None:
        current = None
    else:
        current = _current_projection(current_raw, issues)

    waiting, waiting_dropped = _bounded_text_list(
        data.get("waiting_on_you"), MAX_NOTICE_COUNT, MAX_NOTICE_TEXT, issues, "waiting_on_you"
    )
    waiting = [item for item in waiting if item != "_(none listed)_"]
    projection: Dict[str, Any] = {
        "reported_state": reported_state,
        "current": current,
        "last_progress_at": None,
        "waiting_on_you": waiting,
        "pending": _nonnegative_int(data.get("pending"), issues, "pending"),
        "dispatches_today": _nonnegative_int(data.get("dispatches_today"), issues, "dispatches_today"),
        "dispatch_cap": _nonnegative_int(data.get("dispatch_cap"), issues, "dispatch_cap"),
        "queue_completeness": "unknown",
        "budget_verification": "unknown",
        "review_records": "unavailable",
        "completion_records": "unavailable",
        "artifact_verification": "unavailable",
        "outcome_verification": "unavailable",
        "installation": _installation_projection(data, issues),
        "truncation": {"waiting_on_you": waiting_dropped},
    }
    source["projection"] = projection
    if not structural:
        source["availability"] = "invalid"
        source["coverage"] = "unknown"


def _project_boot_pack(
    data: Mapping[str, Any],
    source: Dict[str, Any],
    issues: _IssueCollector,
) -> None:
    structural = True
    if data.get("schema") != BOOT_PACK_SCHEMA:
        issues.add("unexpected_schema")
        structural = False
    sections = data.get("sections")
    if not isinstance(sections, Mapping):
        issues.add("sections_invalid")
        structural = False
        sections = {}
    queue = sections.get("decision_queue") if isinstance(sections, Mapping) else None
    if not isinstance(queue, Mapping) or not isinstance(queue.get("items"), list):
        issues.add("decision_queue_items_invalid")
        structural = False
        decision_items: Optional[List[Any]] = None
    else:
        decision_items = queue.get("items")

    warning_values = data.get("warnings")
    open_values = data.get("open_items")
    if not isinstance(warning_values, list):
        issues.add("warnings_invalid")
        structural = False
        warning_values = None
    if not isinstance(open_values, list):
        issues.add("open_items_invalid")
        structural = False
        open_values = None

    warning_summaries, warning_dropped = _bounded_text_list(
        warning_values, MAX_WARNING_COUNT, MAX_WARNING_TEXT, issues, "warning_summary"
    )
    decision_invalid = 0
    if decision_items is not None:
        for item in decision_items:
            if not isinstance(item, Mapping):
                decision_invalid += 1
        if decision_invalid:
            issues.add("decision_item_invalid")

    source["projection"] = {
        "warning_count": len(warning_values) if isinstance(warning_values, list) else None,
        "open_item_count": len(open_values) if isinstance(open_values, list) else None,
        "decision_item_count": len(decision_items) if isinstance(decision_items, list) else None,
        "warning_summaries": warning_summaries,
        "truncation": {"warning_summaries": warning_dropped},
        "assertions": "reported-only",
    }
    if not structural:
        source["availability"] = "invalid"
        source["coverage"] = "unknown"


def _lane_projection(
    lane: Any,
    index: int,
    issues: _IssueCollector,
) -> Optional[Dict[str, Any]]:
    code_prefix = "lane_%d" % index
    if not isinstance(lane, Mapping):
        issues.add("lane_invalid")
        return None
    name = _clean_text(lane.get("name"), MAX_NOTICE_TEXT, issues, code_prefix + "_name")
    if not name:
        issues.add("lane_name_missing")
        return None
    step = None
    if "step" in lane:
        step = _clean_text(lane.get("step"), MAX_NOTICE_TEXT, issues, code_prefix + "_step")
    waiting, waiting_dropped = _bounded_text_list(
        lane.get("waiting_on_anthony"), MAX_LANE_LIST_COUNT, MAX_NOTICE_TEXT,
        issues, code_prefix + "_waiting_on_anthony"
    )
    blocked, blocked_dropped = _bounded_text_list(
        lane.get("blocked"), MAX_LANE_LIST_COUNT, MAX_NOTICE_TEXT,
        issues, code_prefix + "_blocked"
    )
    updated = None
    if "updated" in lane and lane.get("updated") is not None:
        updated, updated_dt = _strict_timestamp(lane.get("updated"))
        if updated_dt is None:
            issues.add(code_prefix + "_updated_invalid")
            updated = None
    health = lane.get("health")
    if health is not None and (not isinstance(health, str) or health not in KNOWN_HEALTH):
        issues.add(code_prefix + "_health_invalid")
        health = None
    return {
        "name": name,
        "step": step,
        "waiting_on_anthony": waiting,
        "blocked": blocked,
        "updated": updated,
        "health": health,
        "_waiting_dropped": waiting_dropped,
        "_blocked_dropped": blocked_dropped,
    }


def _project_plans(
    data: Mapping[str, Any],
    source: Dict[str, Any],
    issues: _IssueCollector,
) -> None:
    structural = True
    if "schema" in data:
        issues.add("unexpected_schema")
        structural = False
    if "unregistered_plan_files" in data and not isinstance(data["unregistered_plan_files"], list):
        issues.add("unregistered_plan_files_invalid")
    raw_lanes = data.get("lanes")
    if not isinstance(raw_lanes, list):
        issues.add("lanes_invalid")
        structural = False
        raw_lanes = None
    projected: List[Dict[str, Any]] = []
    if isinstance(raw_lanes, list):
        for index, lane in enumerate(raw_lanes):
            item = _lane_projection(lane, index, issues)
            if item is not None:
                projected.append(item)

    names = [item["name"] for item in projected]
    if len(names) != len(set(names)):
        issues.add("duplicate_lane_name")
        structural = False
        projected = []

    projected.sort(key=lambda item: item["name"])
    lane_dropped = max(0, len(projected) - MAX_LANE_COUNT)
    if lane_dropped:
        issues.add("lanes_truncated")
    projected = projected[:MAX_LANE_COUNT]
    waiting_dropped = sum(item.pop("_waiting_dropped", 0) for item in projected) + 0
    blocked_dropped = sum(item.pop("_blocked_dropped", 0) for item in projected) + 0
    source["projection"] = {
        "reported_lane_count": len(raw_lanes) if isinstance(raw_lanes, list) else None,
        "reported_unregistered_plan_count": len(data["unregistered_plan_files"]) if isinstance(data.get("unregistered_plan_files"), list) else None,
        "valid_lane_count": len(projected),
        "lanes": projected,
        "truncated": bool(lane_dropped or waiting_dropped or blocked_dropped),
        "truncation": {
            "lanes": lane_dropped,
            "waiting_on_anthony": waiting_dropped,
            "blocked": blocked_dropped,
        },
        "assertions": "reported-only",
    }
    if not structural:
        source["availability"] = "invalid"
        source["coverage"] = "unknown"


def _finalize_source(source: Dict[str, Any], issues: _IssueCollector) -> Dict[str, Any]:
    source["issues"] = list(issues.items)
    if source["availability"] == "invalid":
        source["projection"] = {}
        source["freshness"] = "unknown"
        source["source_timestamp"] = None
        source["age_seconds"] = None
    return source


def build_view(
    clayton: Optional[Mapping[str, Any]],
    boot_pack: Optional[Mapping[str, Any]],
    plans: Optional[Mapping[str, Any]],
    now: Any,
    observations: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the pure ``work-view/v1`` model from already-loaded objects."""
    now_utc = _coerce_now(now)

    clayton_source, clayton_issues = _source_base(
        "clayton", clayton, now_utc, CLAYTON_MAX_AGE_SECONDS, "rendered_at", observations
    )
    if clayton is not None and isinstance(clayton, Mapping):
        _project_clayton(clayton, clayton_source, clayton_issues)
    _finalize_source(clayton_source, clayton_issues)

    boot_source, boot_issues = _source_base(
        "boot_pack", boot_pack, now_utc, SNAPSHOT_MAX_AGE_SECONDS, "generated_at", observations
    )
    if boot_pack is not None and isinstance(boot_pack, Mapping):
        _project_boot_pack(boot_pack, boot_source, boot_issues)
    _finalize_source(boot_source, boot_issues)

    plans_source, plans_issues = _source_base(
        "plans", plans, now_utc, SNAPSHOT_MAX_AGE_SECONDS, "generated", observations
    )
    if plans is not None and isinstance(plans, Mapping):
        _project_plans(plans, plans_source, plans_issues)
    _finalize_source(plans_source, plans_issues)

    view: Dict[str, Any] = {
        "schema": WORK_VIEW_SCHEMA,
        "audience": "operator-local",
        "advisory": True,
        "no_commands": True,
        "generated_at": _utc_text(now_utc),
        "snapshot_id": None,
        "sources": {
            "clayton": clayton_source,
            "boot_pack": boot_source,
            "plans": plans_source,
        },
    }
    canonical = json.dumps(
        {key: value for key, value in view.items() if key != "snapshot_id"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    view["snapshot_id"] = hashlib.sha256(canonical).hexdigest()
    return view


def load_input(path: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """Load one strict JSON object without following symlinks or exposing errors."""
    if path is None:
        return None, "unconfigured", None
    try:
        candidate = Path(path).expanduser()
    except (TypeError, ValueError, RuntimeError):
        return None, "read_error", None
    if _contains_symlink(candidate):
        return None, "symlink", None
    try:
        info = candidate.stat()
    except FileNotFoundError:
        return None, "missing", None
    except (OSError, ValueError):
        return None, "read_error", None
    if not stat.S_ISREG(info.st_mode):
        return None, "not_regular", None
    if info.st_size > MAX_INPUT_BYTES:
        return None, "oversize", None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(candidate, flags)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None, "not_regular", None
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except (OSError, ValueError):
        return None, "read_error", None
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_INPUT_BYTES:
        return None, "oversize", digest
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "utf8", digest
    try:
        data = json.loads(
            text,
            object_pairs_hook=_object_pairs_no_duplicates,
            parse_constant=_reject_nonfinite,
            parse_float=_finite_float,
        )
    except _LoadIssue as exc:
        return None, exc.code, digest
    except (ValueError, RecursionError):
        return None, "malformed_json", digest
    if not isinstance(data, dict):
        return None, "not_object", digest
    return data, None, digest


def _object_pairs_no_duplicates(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _LoadIssue("duplicate_key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise _LoadIssue("nonfinite_number")
    return result


def _reject_nonfinite(value: str) -> None:
    raise _LoadIssue("nonfinite_number")


def _contains_symlink(path: Path) -> bool:
    """Check every existing component without resolving through a symlink."""
    try:
        absolute = path if path.is_absolute() else Path.cwd() / path
    except (OSError, TypeError, ValueError, RuntimeError):
        return True
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if part == "..":
            current = current.parent
            continue
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _validate_output_dir(out_dir: Any, input_paths: Mapping[str, Optional[str]]) -> Path:
    if out_dir is None:
        raise WorkViewError("output_dir_missing")
    try:
        requested = Path(out_dir).expanduser()
    except (TypeError, ValueError, RuntimeError):
        raise WorkViewError("output_dir_invalid")
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    state_root = _repo_root() / "state"
    if _contains_symlink(state_root) or _contains_symlink(requested):
        raise WorkViewError("output_symlink")
    try:
        resolved_state = state_root.resolve(strict=False)
        resolved_out = requested.resolve(strict=False)
    except OSError:
        raise WorkViewError("output_dir_invalid")
    if not _is_under(resolved_out, resolved_state):
        raise WorkViewError("output_outside_state")
    if requested.exists() and not requested.is_dir():
        raise WorkViewError("output_dir_invalid")

    seen: Dict[Path, str] = {}
    output_targets = {resolved_out / "WORK-VIEW.md", resolved_out / "work-view.json"}
    for name, raw_path in input_paths.items():
        if raw_path is None:
            continue
        try:
            source_path = Path(raw_path).expanduser()
            if not source_path.is_absolute():
                source_path = Path.cwd() / source_path
            resolved_source = source_path.resolve(strict=False)
        except (OSError, TypeError, ValueError, RuntimeError):
            raise WorkViewError("input_path_invalid")
        if resolved_source in seen:
            raise WorkViewError("input_collision")
        seen[resolved_source] = name
        if resolved_source in output_targets or _is_under(resolved_source, resolved_out):
            raise WorkViewError("input_collision")
    return requested


def _atomic_write(path: Path, text: str) -> None:
    if path.is_symlink() or _contains_symlink(path.parent):
        raise WorkViewError("output_symlink")
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        raise WorkViewError("output_write_error")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _md_escape(value: Any) -> str:
    text = "unknown" if value is None else str(value)
    replacements = (
        ("\\", "\\\\"), ("`", "\\`"), ("*", "\\*"), ("_", "\\_"),
        ("[", "\\["), ("]", "\\]"), ("<", "\\<"), (">", "\\>"),
        ("#", "\\#"), ("|", "\\|"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _quoted(value: Any) -> str:
    return _md_escape(json.dumps(str(value), ensure_ascii=True))


def _display_age(value: Any) -> str:
    if not isinstance(value, int):
        return "unknown"
    seconds = abs(value)
    if seconds >= 86400:
        age = "%dd %dh" % (seconds // 86400, (seconds % 86400) // 3600)
    elif seconds >= 3600:
        age = "%dh %dm" % (seconds // 3600, (seconds % 3600) // 60)
    elif seconds >= 60:
        age = "%dm" % (seconds // 60)
    else:
        age = "%ds" % seconds
    return "in " + age if value < 0 else age


def render_markdown(view: Mapping[str, Any]) -> str:
    """Render only the bounded view, escaping all source-reported strings."""
    sources = view.get("sources") if isinstance(view, Mapping) else {}
    if not isinstance(sources, Mapping):
        sources = {}
    lines = [
        "# Your work across systems",
        "Read-only snapshot for you · source-reported information",
        "",
        "As of: `%s` · snapshot: `%s`" % (
            _md_escape(view.get("generated_at", "unknown")),
            _md_escape(view.get("snapshot_id", "unknown")),
        ),
        "",
        "| Source | Available? | Freshness | Source age | Coverage |",
        "|---|---|---|---|---|",
    ]
    for name in SOURCE_NAMES:
        source = sources.get(name) if isinstance(sources, Mapping) else None
        if not isinstance(source, Mapping):
            source = {}
        lines.append(
            "| %s | %s | %s | %s | %s |" % (
                _md_escape({"clayton": "Clayton", "boot_pack": "Fully Aware context", "plans": "IRIS shared plans"}.get(name, name)),
                _md_escape(source.get("availability", "unknown")),
                _md_escape(source.get("freshness", "unknown")),
                _display_age(source.get("age_seconds")),
                _md_escape(source.get("coverage", "unknown")),
            )
        )

    clayton = sources.get("clayton", {})
    cp = clayton.get("projection", {}) if isinstance(clayton, Mapping) else {}
    if not isinstance(cp, Mapping):
        cp = {}
    lines.extend(["", "## Clayton status"])
    lines.append("- reported state: `%s`" % _md_escape(cp.get("reported_state", "unknown")))
    current = cp.get("current")
    if isinstance(current, Mapping) and current:
        current_bits = []
        for field in ("item", "source_item_id", "brief_id", "kind", "model", "started"):
            if current.get(field) is not None:
                current_bits.append("%s=%s" % (field, _quoted(current[field])))
        lines.append("- current item (source-reported): %s" % (", ".join(current_bits) or "unknown"))
    else:
        lines.append("- current item: unavailable")
    lines.append("- executor host ownership: unverified by this supplied status snapshot")
    lines.append("- last verified progress: unavailable; a start time does not prove progress")
    lines.append(
        "- reported queue: %s pending · %s dispatches today · daily cap %s; completeness %s" % (
            _md_escape(cp.get("pending", "unknown")),
            _md_escape(cp.get("dispatches_today", "unknown")),
            _md_escape(cp.get("dispatch_cap", "unknown")),
            _md_escape(cp.get("queue_completeness", "unknown")),
        )
    )
    waits = cp.get("waiting_on_you")
    if isinstance(waits, list) and waits:
        lines.append("- reported waits: %s" % ", ".join(_quoted(item) for item in waits))
    else:
        lines.append("- reported waits: none available; this is not a complete decision inventory")
    installation = cp.get("installation")
    if isinstance(installation, Mapping):
        lines.append(
            "- installation consistency: `%s`; readiness: `%s`; receipt evidence: `%s`" % (
                _md_escape(installation.get("installation_consistency", "unknown")),
                _md_escape(installation.get("readiness", "unverified")),
                _md_escape(installation.get("receipt_presence", "unavailable")),
            )
        )
    lines.append("- verified progress/review: unavailable from this status projection")
    lines.append("- completion records, review records, and artifact/outcome verification: unavailable")

    boot = sources.get("boot_pack", {})
    bp = boot.get("projection", {}) if isinstance(boot, Mapping) else {}
    if not isinstance(bp, Mapping):
        bp = {}
    lines.extend(["", "## Fully Aware boot pack"])
    lines.append(
        "- source-reported counts: %s warnings · %s open items · %s decision items" % (
            _md_escape(bp.get("warning_count", "unknown")),
            _md_escape(bp.get("open_item_count", "unknown")),
            _md_escape(bp.get("decision_item_count", "unknown")),
        )
    )
    summaries = bp.get("warning_summaries")
    if isinstance(summaries, list) and summaries:
        lines.append("- warning summaries (source-reported):")
        lines.extend("  - %s" % _quoted(item) for item in summaries)
    else:
        lines.append("- warning summaries: none available")
    lines.append("- revalidation/authority: unavailable; this view retains reported state only")

    plans = sources.get("plans", {})
    pp = plans.get("projection", {}) if isinstance(plans, Mapping) else {}
    if not isinstance(pp, Mapping):
        pp = {}
    lines.extend(["", "## IRIS shared plans"])
    lines.append("- unregistered plan files reported: %s" % _md_escape(pp.get("reported_unregistered_plan_count")))
    lanes = pp.get("lanes")
    if isinstance(lanes, list) and lanes:
        for lane in lanes:
            if not isinstance(lane, Mapping):
                continue
            bits = [_quoted(lane.get("name", "unknown"))]
            if lane.get("step") is not None:
                bits.append("next step: %s" % _quoted(lane["step"]))
            if lane.get("health") is not None:
                bits.append("reported status: %s" % _md_escape(lane["health"]))
            lines.append("- source-reported lane: " + ", ".join(bits))
            for label in ("waiting_on_anthony", "blocked"):
                values = lane.get(label)
                if isinstance(values, list) and values:
                    lines.append("  - %s: %s" % (label, ", ".join(_quoted(item) for item in values)))
    else:
        lines.append("- no bounded lanes available")
    lines.append("- plan lanes do not complete commitments or join to Clayton items")

    issues = []
    lines.extend(["", "## Source limitations and input issues"])
    for name in SOURCE_NAMES:
        source = sources.get(name)
        if isinstance(source, Mapping):
            truncation = source.get("projection", {}).get("truncation", {})
            if any(truncation.values()):
                lines.append("- %s omitted entries: %s" % (name, _quoted(json.dumps(truncation, sort_keys=True))))
            for issue in source.get("issues", []):
                if isinstance(issue, str) and issue not in issues:
                    issues.append(issue)
    lines.append("- " + (", ".join(_md_escape(issue) for issue in issues) if issues else "none"))
    return "\n".join(lines) + "\n"


def _load_for_cli(
    name: str,
    path: Optional[str],
    now_text: str,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Optional[str]]]:
    data, issue, digest = load_input(path)
    return data, {"sha256": digest, "read_error": issue, "observed_at": now_text}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="render an advisory local work view")
    parser.add_argument("--clayton-status")
    parser.add_argument("--boot-pack")
    parser.add_argument("--plans-snapshot")
    parser.add_argument("--now")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--out-dir")
    args = parser.parse_args(argv)

    if args.now is None:
        now = _datetime.datetime.now(UTC)
    else:
        _, now = _strict_timestamp(args.now)
        if now is None:
            sys.stderr.write("work-view: invalid_now\n")
            return 2
    now_text = _utc_text(_datetime.datetime.now(UTC))

    paths = {
        "clayton": args.clayton_status,
        "boot_pack": args.boot_pack,
        "plans": args.plans_snapshot,
    }
    if args.out_dir is not None:
        try:
            output_dir = _validate_output_dir(args.out_dir, paths)
        except WorkViewError as exc:
            sys.stderr.write("work-view: %s\n" % exc.code)
            return 2
    else:
        output_dir = None

    data: Dict[str, Optional[Dict[str, Any]]] = {}
    observations: Dict[str, Dict[str, Optional[str]]] = {}
    for name in SOURCE_NAMES:
        data[name], observations[name] = _load_for_cli(name, paths[name], now_text)
    view = build_view(
        data["clayton"], data["boot_pack"], data["plans"], now,
        observations=observations,
    )
    markdown = render_markdown(view)
    json_text = json.dumps(view, ensure_ascii=False, indent=2, sort_keys=False) + "\n"

    if output_dir is None:
        sys.stdout.write(json_text if args.format == "json" else markdown)
        return 0

    try:
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _contains_symlink(output_dir):
            raise WorkViewError("output_symlink")
        os.chmod(output_dir, 0o700)
        _atomic_write(output_dir / "WORK-VIEW.md", markdown)
        _atomic_write(output_dir / "work-view.json", json_text)
    except WorkViewError as exc:
        sys.stderr.write("work-view: %s\n" % exc.code)
        return 2
    except OSError:
        sys.stderr.write("work-view: output_write_error\n")
        return 2
    if args.format == "json":
        sys.stdout.write(json_text)
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
