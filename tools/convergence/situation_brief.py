#!/usr/bin/env python3
"""Build a bounded, read-only current situation brief.

This module is deliberately a consumer.  It reads two explicit Fully Aware
snapshots, optionally reads one explicitly supplied sweep receipt, and makes a
small fixed set of loopback GET requests to the existing IRIS service.  It
does not write state, schedule work, send messages, or infer authority.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import stat
import sys
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, ProxyHandler

try:
    from .work_view import build_view, load_input
    from .priority_context import project_priority
    from .sweep_attempt import read_attempt
except ImportError:  # Direct execution from tools/convergence.
    from work_view import build_view, load_input
    from priority_context import project_priority
    from sweep_attempt import read_attempt


SCHEMA = "situation-brief/v1"
IRIS_BASE = "http://127.0.0.1:4180"
IRIS_PATHS = ("/healthz", "/data/board.json", "/focus.json", "/local-agent.json", "/priority.json")
HTTP_MAX_BYTES = 2 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 3.0
SWEEP_MAX_BYTES = 1024 * 1024
DIGEST_MAX_BYTES = 128 * 1024
DIGEST_MAX_AGE_SECONDS = 24 * 60 * 60
LATEST_SWEEP_SCHEMA = "iris-sweep-outcome/v1"
LATEST_CHANGE_MAX_COUNT = 3
LATEST_CHANGE_KEY_CHARS = 120
MAX_OUTPUT_CHARS = 12_000
MAX_EXCERPT_CHARS = 4_000
MAX_FOCUS_REQUESTS = 5
MAX_TEXT = 280
MAX_SOURCE_TEXT = 400
LATEST_CHANGE_EXCERPT_CHARS = MAX_SOURCE_TEXT
LATEST_NEXT_ACTION_CHARS = MAX_SOURCE_TEXT
LATEST_COVERAGE_MAX_SOURCES = 12
MAX_DEADLINE_ROWS = 3
# Existing IRIS freshness_model.verification contract; board generation is 300s.
SOURCE_MAX_AGE_SECONDS = 108000
SELECTED_LANES = ("fully-aware-convergence", "iris", "autonomous-operators")
FOCUS_GROUPS = ("decision", "reconciliation", "prepared", "later", "history")
CLOSED_STATUSES = {
    "complete", "completed", "done", "delivered", "cancelled", "canceled", "parked", "superseded", "closed",
}
HEX64 = set("0123456789abcdefABCDEF")


def _stamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now(value: Optional[dt.datetime] = None) -> dt.datetime:
    result = value or dt.datetime.now(dt.timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("brief time must be timezone-aware")
    return result.astimezone(dt.timezone.utc)


def _parse_time(value: Any, *, allow_date: bool = False) -> Tuple[Optional[dt.datetime], Optional[str]]:
    if not isinstance(value, str) or not value.strip():
        return None, "timestamp_missing"
    text = value.strip()
    if allow_date and len(text) == 10:
        try:
            return dt.datetime.combine(dt.date.fromisoformat(text), dt.time(), tzinfo=dt.timezone.utc), None
        except (TypeError, ValueError):
            return None, "timestamp_invalid"
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None, "timestamp_invalid"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "timestamp_naive"
    try:
        return parsed.astimezone(dt.timezone.utc), None
    except (TypeError, ValueError, OverflowError):
        return None, "timestamp_invalid"


def _time_state(value: Any, now: dt.datetime, *, allow_date: bool = False) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    parsed, issue = _parse_time(value, allow_date=allow_date)
    if parsed is None:
        return None, None, issue
    delta = (now - parsed).total_seconds()
    age = int(delta)
    if delta < 0:
        return None, age, "timestamp_future"
    return (value.strip() if isinstance(value, str) else None), age, None


def _clean_text(value: Any, limit: int = MAX_TEXT, *, preserve_newlines: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None
    chars: List[str] = []
    for char in value:
        category = unicodedata.category(char)
        if category == "Cf" or category.startswith("C") and char not in "\n\t" or char == "\r":
            continue
        if not preserve_newlines and char in "\n\t":
            chars.append(" ")
        else:
            chars.append(char)
    text = "".join(chars).strip()
    if not preserve_newlines:
        text = " ".join(text.split())
    return text[:limit].rstrip() if text else ""


def _hash_ok(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX64


def _reference(path: Optional[str], fallback: str) -> str:
    if not isinstance(path, str) or not path:
        return fallback
    try:
        return Path(path).name or fallback
    except (TypeError, ValueError, OSError):
        return fallback


def _unavailable(reference: str, issue: str, *, observed_at: Optional[str] = None,
                 sha256: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "availability": "unavailable",
        "current": False,
        "freshness": "unknown",
        "source_timestamp": None,
        "age_seconds": None,
        "reference": reference,
        "sha256": sha256,
        "observed_at": observed_at,
        "issues": [issue],
    }
    result.update(extra)
    return result


def _source_metadata(reference: str, sha256: Optional[str], observed_at: str,
                     *, status: Optional[int] = None, byte_count: Optional[int] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "reference": reference,
        "sha256": sha256,
        "observed_at": observed_at,
    }
    if status is not None:
        result["http_status"] = status
    if byte_count is not None:
        result["bytes"] = byte_count
    return result


def _first_issue(meta: Mapping[str, Any]) -> Optional[str]:
    issues = meta.get("issues")
    return issues[0] if isinstance(issues, list) and issues else None


def _bounded_issues(value: Any, limit: int = 12) -> List[str]:
    if not isinstance(value, list):
        return ["issues_invalid"]
    result = [item for item in value if isinstance(item, str) and item]
    if len(result) > limit:
        return result[:limit] + ["additional_issues_omitted"]
    return result


def _unique_limits(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in result:
            result.append(item)
    return result


def _identity(value: Any, limit: int = 160) -> Optional[str]:
    """Keep exact source identities only when they are bounded and safe text."""
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    cleaned = _clean_text(value, limit)
    return cleaned if cleaned == value else None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _http_opener():
    # An empty proxy map is intentional: this reader has one fixed loopback
    # origin and must not inherit process proxy settings.
    return build_opener(ProxyHandler({}), _NoRedirect())


def fetch_endpoint(path: str, *, now: Optional[dt.datetime] = None, opener: Any = None,
                   timeout: float = HTTP_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """GET one fixed IRIS route and return a sanitized observation envelope."""
    observed = _stamp(_now(now))
    if path not in IRIS_PATHS:
        return {"path": path, "status": None, "data": None, "sha256": None, "bytes": 0,
                "observed_at": observed, "issue": "route_not_allowlisted"}
    request = Request(IRIS_BASE + path, headers={"Accept": "application/json"}, method="GET")
    client = opener or _http_opener()
    try:
        with client.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()))
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) > HTTP_MAX_BYTES:
                        return {"path": path, "status": status, "data": None, "sha256": None,
                                "bytes": 0, "observed_at": observed, "issue": "response_oversize"}
                except (TypeError, ValueError):
                    return {"path": path, "status": status, "data": None, "sha256": None,
                            "bytes": 0, "observed_at": observed, "issue": "response_length_invalid"}
            raw = response.read(HTTP_MAX_BYTES + 1)
            observed = _stamp(_now(now))
    except HTTPError as error:
        if 300 <= int(error.code) < 400:
            return {"path": path, "status": int(error.code), "data": None, "sha256": None,
                    "bytes": 0, "observed_at": observed, "issue": "redirect_refused"}
        return {"path": path, "status": int(error.code), "data": None, "sha256": None,
                "bytes": 0, "observed_at": observed, "issue": "http_error"}
    except (URLError, OSError, TimeoutError, ValueError, HTTPException):
        return {"path": path, "status": None, "data": None, "sha256": None,
                "bytes": 0, "observed_at": observed, "issue": "transport_unavailable"}
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > HTTP_MAX_BYTES:
        return {"path": path, "status": status, "data": None, "sha256": digest,
                "bytes": len(raw), "observed_at": observed, "issue": "response_oversize"}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {"path": path, "status": status, "data": None, "sha256": digest,
                "bytes": len(raw), "observed_at": observed, "issue": "response_invalid_json"}
    if not isinstance(data, dict):
        return {"path": path, "status": status, "data": None, "sha256": digest,
                "bytes": len(raw), "observed_at": observed, "issue": "response_not_object"}
    return {"path": path, "status": status, "data": data, "sha256": digest,
            "bytes": len(raw), "observed_at": observed, "issue": None}


def _load_snapshot(path: Optional[str], now: dt.datetime) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    observed = _stamp(now)
    try:
        data, issue, digest = load_input(path)
    except (ValueError, TypeError, OverflowError, RecursionError):
        data, issue, digest = None, "snapshot_invalid", None
    ref = _reference(path, "explicit-input")
    meta = _source_metadata(ref, digest, observed)
    if issue is not None:
        meta.update(_unavailable(ref, issue, observed_at=observed, sha256=digest))
        return None, meta
    if not isinstance(data, dict):
        meta.update(_unavailable(ref, "snapshot_not_object", observed_at=observed, sha256=digest))
        return None, meta
    meta.update({"availability": "available", "current": False, "freshness": "unknown",
                 "source_timestamp": None, "age_seconds": None, "issues": []})
    return data, meta


def _summarize_work_view(boot_path: str, plans_path: str, now: dt.datetime) -> Dict[str, Any]:
    boot, boot_meta = _load_snapshot(boot_path, now)
    plans, plans_meta = _load_snapshot(plans_path, now)
    observations = {
        "clayton": {"read_error": "unconfigured", "observed_at": _stamp(now)},
        "boot_pack": {"sha256": boot_meta.get("sha256"), "observed_at": boot_meta.get("observed_at"),
                       "read_error": _first_issue(boot_meta)},
        "plans": {"sha256": plans_meta.get("sha256"), "observed_at": plans_meta.get("observed_at"),
                  "read_error": _first_issue(plans_meta)},
    }
    try:
        view = build_view(None, boot, plans, now, observations=observations)
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
        view = None
    if not isinstance(view, dict):
        return {"schema": "work-view/v1", "snapshot_id": None, "generated_at": _stamp(now),
                "sources": {"boot_pack": _unavailable(_reference(boot_path, "boot-pack"), "projection_failed"),
                            "plans": _unavailable(_reference(plans_path, "plans"), "projection_failed")},
                "selected_lanes": [], "other_lane_count": None, "limits": ["work-view projection unavailable"]}

    sources = view.get("sources", {}) if isinstance(view.get("sources"), dict) else {}
    result_sources: Dict[str, Any] = {}
    for name, meta in (("boot_pack", boot_meta), ("plans", plans_meta)):
        source = sources.get(name) if isinstance(sources.get(name), dict) else {}
        source_out = {
            **_source_metadata(meta.get("reference", name), meta.get("sha256"), meta.get("observed_at", _stamp(now))),
            "availability": source.get("availability", "unavailable"),
            "current": source.get("availability") == "available" and source.get("freshness") == "fresh",
            "freshness": source.get("freshness", "unknown"),
            "source_timestamp": source.get("source_timestamp"),
            "age_seconds": source.get("age_seconds"),
            "coverage": source.get("coverage", "unknown"),
            "issues": _bounded_issues(source.get("issues")),
        }
        projection = source.get("projection") if isinstance(source.get("projection"), dict) else {}
        if name == "boot_pack":
            source_out["summary"] = {key: projection.get(key) for key in
                                      ("warning_count", "open_item_count", "decision_item_count", "warning_summaries")}
        else:
            lanes = projection.get("lanes") if isinstance(projection.get("lanes"), list) else []
            selected: List[Dict[str, Any]] = []
            for lane in lanes:
                if not isinstance(lane, dict) or lane.get("name") not in SELECTED_LANES:
                    continue
                waiting_raw = lane.get("waiting_on_anthony") if isinstance(lane.get("waiting_on_anthony"), list) else []
                blocked_raw = lane.get("blocked") if isinstance(lane.get("blocked"), list) else []
                waiting_values = [_clean_text(x, 180) for x in waiting_raw if _clean_text(x, 180) is not None]
                blocked_values = [_clean_text(x, 180) for x in blocked_raw if _clean_text(x, 180) is not None]
                selected.append({
                    "name": lane.get("name"),
                    "step": _clean_text(lane.get("step")),
                    "health": lane.get("health"),
                    "updated": lane.get("updated"),
                    "waiting_on_anthony": waiting_values[:3],
                    "waiting_on_anthony_omitted": max(0, len(waiting_values) - 3),
                    "blocked": blocked_values[:3],
                    "blocked_omitted": max(0, len(blocked_values) - 3),
                })
            source_out["_selected_lanes"] = selected
            source_out["reported_lane_count"] = projection.get("reported_lane_count")
            source_out["valid_lane_count"] = projection.get("valid_lane_count")
            valid_count = projection.get("valid_lane_count")
            source_out["other_lane_count"] = max(0, valid_count - len(selected)) if isinstance(valid_count, int) else None
        result_sources[name] = source_out

    plans_source = result_sources.get("plans", {})
    selected_lanes = plans_source.pop("_selected_lanes", []) if isinstance(plans_source, dict) else []
    return {
        "schema": view.get("schema", "work-view/v1"),
        "snapshot_id": view.get("snapshot_id"),
        "generated_at": view.get("generated_at"),
        "advisory": True,
        "no_commands": True,
        "sources": result_sources,
        "selected_lanes": selected_lanes,
        "other_lane_count": result_sources.get("plans", {}).get("other_lane_count"),
        "limits": ["selected lanes are a bounded projection; other lanes are represented by count only"],
    }


def _board_binding(board: Mapping[str, Any]) -> Optional[List[Tuple[str, str]]]:
    if not isinstance(board, dict):
        return None
    items = board.get("items")
    if not isinstance(items, list):
        return None
    result: List[Tuple[str, str]] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item.get("id"):
            return None
        status = item.get("effective_status", item.get("state"))
        if not isinstance(status, str) or not status:
            return None
        if item["id"] in seen:
            return None
        seen.add(item["id"])
        result.append((item["id"], status))
    return result


def _board_proof(board: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(board, dict):
        return None
    proof = board.get("projection")
    if not isinstance(proof, dict):
        return None
    if not isinstance(proof.get("export_generation"), str) or not proof.get("export_generation") or len(proof["export_generation"]) > 160:
        return None
    if not _hash_ok(proof.get("content_sha256")):
        return None
    if not isinstance(proof.get("source_verified_at"), str) or len(proof["source_verified_at"]) > 80:
        return None
    verified, issue = _parse_time(proof["source_verified_at"])
    if issue is not None or verified is None:
        return None
    return {key: proof[key] for key in ("export_generation", "content_sha256", "source_verified_at")}


def _board_shape(board: Mapping[str, Any]) -> Dict[str, Any]:
    items = board.get("items") if isinstance(board.get("items"), list) else []
    activity = board.get("activity") if isinstance(board.get("activity"), list) else []
    return {"items": len(items), "activity": len(activity), "schema_version": board.get("schema_version")}


def _valid_board(board: Any, now: dt.datetime) -> Tuple[bool, List[str], Optional[dt.datetime], Optional[Dict[str, Any]]]:
    issues: List[str] = []
    if not isinstance(board, dict):
        return False, ["board_not_object"], None, None
    if board.get("schema_version") != 3:
        issues.append("board_schema_invalid")
    proof = _board_proof(board)
    if proof is None:
        issues.append("board_proof_invalid")
    generated, generated_issue = _parse_time(board.get("generated_at"))
    if generated_issue:
        issues.append("board_generated_at_" + generated_issue)
    if generated is not None and generated > now:
        issues.append("board_generated_at_future")
    if generated is not None and (now - generated).total_seconds() > 300:
        issues.append("board_generation_stale")
    freshness = board.get("freshness")
    if not isinstance(freshness, dict):
        issues.append("board_freshness_invalid")
    else:
        if freshness.get("state") != "current" or freshness.get("coverage") != "complete" or freshness.get("reason") != "verified":
            issues.append("board_freshness_not_current")
        verified, verified_issue = _parse_time(freshness.get("verified_at"))
        if verified_issue:
            issues.append("board_verified_at_" + verified_issue)
        if verified is not None and verified > now:
            issues.append("board_verified_at_future")
        if verified is not None and (now - verified).total_seconds() > SOURCE_MAX_AGE_SECONDS:
            issues.append("board_verification_stale")
        if proof is not None and freshness.get("verified_at") != proof.get("source_verified_at"):
            issues.append("board_proof_freshness_mismatch")
    if _board_binding(board) is None:
        issues.append("board_binding_invalid")
    return not issues, issues, generated, proof


def _health_current(health: Any, board: Mapping[str, Any], now: dt.datetime) -> Tuple[bool, List[str]]:
    issues: List[str] = []
    if not isinstance(health, dict):
        return False, ["health_not_object"]
    if health.get("schema_version") != 3:
        issues.append("health_schema_invalid")
    if not isinstance(health.get("status"), str) or health.get("status") not in {"ok", "partial"}:
        issues.append("health_status_not_current")
    if health.get("last_error") is not None:
        issues.append("health_last_error")
    freshness = health.get("freshness")
    if not isinstance(freshness, dict) or freshness.get("state") != "current" or freshness.get("coverage") != "complete" or freshness.get("reason") != "verified":
        issues.append("health_freshness_not_current")
    generated, generated_issue = _parse_time(health.get("generated_at"))
    if generated_issue:
        issues.append("health_generated_at_" + generated_issue)
    if generated is not None and generated > now:
        issues.append("health_generated_at_future")
    verified = freshness.get("verified_at") if isinstance(freshness, dict) else None
    verified_time, verified_issue = _parse_time(verified)
    if verified_issue:
        issues.append("health_verified_at_" + verified_issue)
    elif verified_time > now:
        issues.append("health_verified_at_future")
    elif (now - verified_time).total_seconds() > SOURCE_MAX_AGE_SECONDS:
        issues.append("health_verification_stale")
    proof = _board_proof(board)
    if proof is None:
        issues.append("health_board_proof_missing")
    elif verified != proof.get("source_verified_at"):
        issues.append("health_verified_at_proof_mismatch")
    max_stale = health.get("max_stale_seconds")
    board_age = health.get("board_age_seconds")
    if type(max_stale) is not int or max_stale < 0 or type(board_age) is not int or board_age < 0:
        issues.append("health_age_invalid")
    elif board_age > min(300, max_stale):
        issues.append("health_board_stale")
    if generated is not None and type(max_stale) is int and max_stale >= 0 and (now - generated).total_seconds() > min(300, max_stale):
        issues.append("health_generation_stale")
    return not issues, issues


def _project_focus(payload: Any, now: dt.datetime, board_current: bool, meta: Mapping[str, Any]) -> Dict[str, Any]:
    ref = str(meta.get("reference", "/focus.json"))
    observed = _time_state(payload.get("observed_at") if isinstance(payload, dict) else None, now)
    if not isinstance(payload, dict) or payload.get("schema") != "iris-focus/v1" or payload.get("status") != "ok":
        return _unavailable(ref, "focus_shape_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
    observed_text, age, time_issue = observed
    if time_issue:
        return _unavailable(ref, "focus_" + time_issue, observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
    counts = payload.get("counts")
    if not isinstance(counts, dict) or any(type(counts.get(group)) is not int or counts[group] < 0 for group in FOCUS_GROUPS):
        return _unavailable(ref, "focus_counts_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
    requests = payload.get("requests")
    if not isinstance(requests, list):
        return _unavailable(ref, "focus_requests_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
    projected: List[Dict[str, Any]] = []
    for row in requests:
        if not isinstance(row, dict):
            return _unavailable(ref, "focus_request_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        key = _identity(row.get("key"))
        work_id = _identity(row.get("work_id"))
        if key is None or work_id is None:
            return _unavailable(ref, "focus_identity_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        if not isinstance(row.get("group"), str) or row.get("group") not in FOCUS_GROUPS or _identity(row.get("state"), 80) is None:
            return _unavailable(ref, "focus_state_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        if not isinstance(row.get("current_match"), str) or not isinstance(row.get("source_applicability"), str):
            return _unavailable(ref, "focus_match_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        for text_key in ("question", "recommendation"):
            if _clean_text(row.get(text_key)) is None:
                return _unavailable(ref, "focus_text_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        snooze = row.get("snooze_until")
        if snooze is not None:
            # A snooze is intentionally allowed to point into the future.  It
            # still must be an aware, parseable timestamp before it is copied.
            snooze_text, snooze_issue = _parse_time(snooze)
            if snooze_issue:
                return _unavailable(ref, "focus_snooze_" + snooze_issue, observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        response_count = row.get("response_count")
        if type(response_count) is not int or response_count < 0:
            return _unavailable(ref, "focus_response_count_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        projected.append({
            "key": key,
            "work_id": work_id,
            "project": _clean_text(row.get("project"), 160),
            "group": row["group"],
            "state": row["state"],
            "current_match": _clean_text(row["current_match"], 80) or "unknown",
            "source_applicability": _clean_text(row["source_applicability"], 80) or "unknown",
            "snooze_until": snooze,
            "recheck_needed": row.get("recheck_needed") if isinstance(row.get("recheck_needed"), bool) else None,
            "question": _clean_text(row["question"]),
            "recommendation": _clean_text(row["recommendation"]),
            "transport": _clean_text(row.get("transport"), 80) or "unknown",
            "response_count": response_count,
        })
    # Current questions first, followed by answered/current work; withdrawn
    # history must not crowd an actual recorded answer out of the brief.
    projected.sort(key=lambda row: (
        {"decision": 0, "reconciliation": 1, "prepared": 2, "later": 3, "history": 4}[row["group"]],
        row["group"] == "history" and row["source_applicability"] == "withdrawn",
        row["group"] == "history" and row["response_count"] == 0))
    omitted = max(0, len(projected) - MAX_FOCUS_REQUESTS)
    projected = projected[:MAX_FOCUS_REQUESTS]
    source_current = bool(board_current and payload.get("work_verification") == "current"
                          and time_issue is None and age is not None and age <= 300)
    return {
        **_source_metadata(ref, meta.get("sha256"), meta.get("observed_at", _stamp(now)), status=meta.get("http_status"), byte_count=meta.get("bytes")),
        "availability": "available",
        "current": source_current,
        "freshness": "fresh" if source_current else "unknown",
        "source_timestamp": observed_text,
        "age_seconds": age,
        "work_verification": payload.get("work_verification") if isinstance(payload.get("work_verification"), str) and payload.get("work_verification") in {"current", "stale", "unavailable"} else "unknown",
        "counts": {group: counts[group] for group in FOCUS_GROUPS},
        "request_count": len(requests),
        "omitted_requests": omitted,
        "requests": projected,
        "authority": _clean_text(payload.get("authority"), 80) or "unknown",
        "delivery": _clean_text(payload.get("delivery"), 120) or "unknown",
        "issues": (["board_not_current"] if not board_current else []),
        "limits": ["transport is a source record; no human-read, answer, delivery, or authority is inferred"],
    }


def _project_local_agent(payload: Any, now: dt.datetime, meta: Mapping[str, Any]) -> Dict[str, Any]:
    ref = str(meta.get("reference", "/local-agent.json"))
    if not isinstance(payload, dict) or payload.get("schema") != "iris-local-agent-view/v1":
        return _unavailable(ref, "local_agent_shape_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
    observed_text, age, time_issue = _time_state(payload.get("observed_at"), now)
    if time_issue:
        return _unavailable(ref, "local_agent_" + time_issue, observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
    freshness = payload.get("freshness")
    if not isinstance(freshness, str) or freshness not in {"fresh", "stale", "unavailable"}:
        return _unavailable(ref, "local_agent_freshness_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
    activity = payload.get("activity")
    projected_activity = None
    activity_updated_age: Optional[int] = None
    if activity is not None:
        if not isinstance(activity, dict):
            return _unavailable(ref, "local_agent_activity_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        required = ("run_id", "parent_run_id", "owner_task_id", "lane", "title", "host", "model", "state",
                    "started_at", "updated_at", "elapsed_seconds", "budget_seconds", "last_event_at", "summary", "artifact", "review")
        if any(key not in activity for key in required):
            return _unavailable(ref, "local_agent_activity_fields_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        for key in ("run_id", "owner_task_id", "lane", "title", "host", "model", "state"):
            if _identity(activity.get(key)) is None:
                return _unavailable(ref, "local_agent_activity_identity_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        if activity.get("parent_run_id") is not None and _identity(activity.get("parent_run_id")) is None:
            return _unavailable(ref, "local_agent_parent_run_identity_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        for key in ("started_at", "updated_at"):
            _text, _age, issue = _time_state(activity.get(key), now)
            if issue:
                return _unavailable(ref, "local_agent_activity_" + issue, observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
            if key == "updated_at":
                activity_updated_age = _age
        if activity.get("last_event_at") is not None:
            _text, _age, issue = _time_state(activity.get("last_event_at"), now)
            if issue:
                return _unavailable(ref, "local_agent_event_" + issue, observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        if (not isinstance(activity.get("elapsed_seconds"), (int, float))
                or isinstance(activity.get("elapsed_seconds"), bool)
                or (type(activity["elapsed_seconds"]) is int and activity["elapsed_seconds"].bit_length() > 53)
                or not math.isfinite(activity["elapsed_seconds"])
                or activity["elapsed_seconds"] < 0):
            return _unavailable(ref, "local_agent_elapsed_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        if type(activity.get("budget_seconds")) is not int or activity["budget_seconds"] < 1:
            return _unavailable(ref, "local_agent_budget_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
        artifact = activity.get("artifact")
        if artifact is not None:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("label"), str) or not _hash_ok(artifact.get("sha256")) or type(artifact.get("bytes")) is not int or artifact["bytes"] < 0:
                return _unavailable(ref, "local_agent_artifact_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
            artifact = {"label": _clean_text(artifact["label"], 120) or "", "sha256": artifact["sha256"], "bytes": artifact["bytes"]}
        review = activity.get("review")
        if review is not None:
            if (not isinstance(review, dict) or not isinstance(review.get("verdict"), str)
                    or review.get("verdict") not in {"accepted", "needs_revision", "rejected"}
                    or _clean_text(review.get("summary"), 500) is None):
                return _unavailable(ref, "local_agent_review_invalid", observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
            _reviewed, _age, issue = _time_state(review.get("reviewed_at"), now)
            if issue:
                return _unavailable(ref, "local_agent_review_" + issue, observed_at=meta.get("observed_at"), sha256=meta.get("sha256"))
            review = {"verdict": review["verdict"], "summary": _clean_text(review["summary"], 500) or "", "reviewed_at": review["reviewed_at"]}
        projected_activity = {
            "run_id": activity["run_id"], "parent_run_id": activity["parent_run_id"], "owner_task_id": activity["owner_task_id"],
            "lane": _clean_text(activity["lane"], 80) or "", "title": _clean_text(activity["title"], 180) or "",
            "host": _clean_text(activity["host"], 100) or "", "model": _clean_text(activity["model"], 100) or "",
            "state": _clean_text(activity["state"], 80) or "unknown", "started_at": activity["started_at"],
            "updated_at": activity["updated_at"], "last_event_at": activity["last_event_at"],
            "elapsed_seconds": activity["elapsed_seconds"], "budget_seconds": activity["budget_seconds"],
            "summary": _clean_text(activity["summary"], 500) or "", "artifact": artifact, "review": review,
        }
    return {
        **_source_metadata(ref, meta.get("sha256"), meta.get("observed_at", _stamp(now)), status=meta.get("http_status"), byte_count=meta.get("bytes")),
        "availability": "available",
        "current": payload.get("status") == "ok" and freshness == "fresh" and activity_updated_age is not None and activity_updated_age <= 300,
        "freshness": ("stale" if freshness == "fresh" and (activity_updated_age is None or activity_updated_age > 300) else freshness),
        "source_timestamp": observed_text,
        "age_seconds": age,
        "status": _clean_text(payload.get("status"), 40) or "unknown",
        "reason": _clean_text(payload.get("reason"), 240) or "unknown",
        "activity": projected_activity,
        "process_inference": "unsupported",
        "issues": (["local_agent_activity_stale"] if freshness == "fresh" and (activity_updated_age is None or activity_updated_age > 300) else []),
        "limits": ["stale or accepted activity is an observation; no process-running or business-acceptance claim is inferred"],
    }


def _observe_iris(now: dt.datetime, opener: Any = None) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    order: List[str] = []
    board_before = fetch_endpoint("/data/board.json", opener=opener); order.append("/data/board.json")
    health = fetch_endpoint("/healthz", opener=opener); order.append("/healthz")
    focus = fetch_endpoint("/focus.json", opener=opener); order.append("/focus.json")
    local_agent = fetch_endpoint("/local-agent.json", opener=opener); order.append("/local-agent.json")
    priority = fetch_endpoint("/priority.json", opener=opener); order.append("/priority.json")
    board_after = fetch_endpoint("/data/board.json", opener=opener); order.append("/data/board.json")

    collection_now, collection_issue = _parse_time(board_after.get("observed_at"))
    if collection_issue is not None or collection_now is None:
        collection_now = _now()

    before_data, after_data = board_before.get("data"), board_after.get("data")
    before_valid, before_issues, _before_generated, before_proof = _valid_board(before_data, collection_now)
    after_valid, after_issues, _after_generated, after_proof = _valid_board(after_data, collection_now)
    issues = list(dict.fromkeys(before_issues + after_issues))
    coherent = before_valid and after_valid and before_proof == after_proof and _board_binding(before_data) == _board_binding(after_data)
    if before_proof != after_proof:
        issues.append("producer_proof_mismatch")
    if _board_binding(before_data) != _board_binding(after_data):
        issues.append("ordered_work_binding_mismatch")
    health_current, health_issues = _health_current(health.get("data"), after_data if isinstance(after_data, dict) else {}, collection_now)
    issues.extend(health_issues)
    current = bool(coherent and health_current and board_after.get("status") == 200)
    board_summary: Dict[str, Any] = {
        "reference": "/data/board.json",
        "availability": "available" if isinstance(after_data, dict) else "unavailable",
        "current": current,
        "freshness": "fresh" if current else "unknown",
        "before": _source_metadata("/data/board.json", board_before.get("sha256"), board_before.get("observed_at", _stamp(now)), status=board_before.get("status"), byte_count=board_before.get("bytes")),
        "after": _source_metadata("/data/board.json", board_after.get("sha256"), board_after.get("observed_at", _stamp(now)), status=board_after.get("status"), byte_count=board_after.get("bytes")),
        "proof": after_proof,
        "shape": _board_shape(after_data) if isinstance(after_data, dict) else None,
        "issues": list(dict.fromkeys(issues)),
        "read_order": order,
        "limits": ["current requires exact before/after producer proof and ordered ID/status binding plus current health verification"],
    }
    if isinstance(after_data, dict):
        board_summary["execution_allowed"] = after_data.get("execution_allowed") if isinstance(after_data.get("execution_allowed"), bool) else None
    health_data = health.get("data")
    health_summary = _http_source_summary(health, "/healthz")
    if isinstance(health_data, dict):
        for key in ("status", "service", "source_health"):
            health_summary[key] = _clean_text(health_data.get(key), 120)
        for key in ("generated_at", "last_refresh_at"):
            value = health_data.get(key)
            parsed, issue = _parse_time(value)
            health_summary[key] = value.strip() if isinstance(value, str) and parsed is not None and issue is None else None
        error = health_data.get("last_error")
        health_summary["last_error"] = _clean_text(error, 240) if isinstance(error, str) else ("present" if error is not None else None)
        for key in ("items", "activity", "board_age_seconds", "max_stale_seconds"):
            value = health_data.get(key)
            health_summary[key] = value if type(value) is int and value >= 0 else None
        freshness = health_data.get("freshness")
        health_summary["freshness"] = {
            "state": _clean_text(freshness.get("state"), 40),
            "coverage": _clean_text(freshness.get("coverage"), 40),
            "reason": _clean_text(freshness.get("reason"), 80),
            "verified_at": (freshness.get("verified_at").strip()
                            if isinstance(freshness.get("verified_at"), str)
                            and _parse_time(freshness.get("verified_at"))[0] is not None
                            and _parse_time(freshness.get("verified_at"))[1] is None else None),
        } if isinstance(freshness, dict) else None
        coverage = health_data.get("coverage")
        health_summary["coverage"] = {
            "notion_inventory": _clean_text(coverage.get("notion_inventory"), 40),
            "repository_enrichment": _clean_text(coverage.get("repository_enrichment"), 40),
            "aggregate": _clean_text(coverage.get("aggregate"), 40),
            "narrative_deadlines": _clean_text(coverage.get("narrative_deadlines"), 40),
        } if isinstance(coverage, dict) else None
    health_summary["current"] = health_current and health.get("status") == 200
    health_summary["issues"] = health_issues
    try:
        focus_summary = _project_focus(focus.get("data"), collection_now, current, _http_source_summary(focus, "/focus.json"))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        focus_summary = _unavailable("/focus.json", "focus_projection_invalid")
    try:
        local_summary = _project_local_agent(local_agent.get("data"), collection_now, _http_source_summary(local_agent, "/local-agent.json"))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        local_summary = _unavailable("/local-agent.json", "local_agent_projection_invalid")
    priority_summary = project_priority(priority.get("data") if priority.get("status") == 200 else None,
                                        collection_now, _http_source_summary(priority, "/priority.json"),
                                        work_current=current)
    iris = {"health": health_summary, "board": board_summary, "focus": focus_summary, "local_agent": local_summary,
            "priorities": priority_summary,
            "limits": ["IRIS projections are read-only; task status, model result, accepted action, human answer, and authority remain separate"]}
    return iris, after_data if isinstance(after_data, dict) else None


def _http_source_summary(observation: Mapping[str, Any], reference: str) -> Dict[str, Any]:
    return _source_metadata(reference, observation.get("sha256"), observation.get("observed_at", ""),
                            status=observation.get("status"), byte_count=observation.get("bytes"))


def _read_secure_regular(path: Path, limit: int) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        if not path.is_absolute() or ".." in path.parts or any(parent.is_symlink() for parent in (path, *path.parents)):
            return None, "declared_path_unsafe"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                return None, "declared_file_invalid"
            data = b""
            while len(data) <= limit:
                chunk = os.read(descriptor, min(16_384, limit + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            after = os.fstat(descriptor)
            if len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return None, "declared_file_changed"
            if len(data) > limit:
                return None, "declared_file_oversize"
            return data, None
        finally:
            os.close(descriptor)
    except (OSError, ValueError, TypeError):
        return None, "declared_file_unavailable"


def _load_sweep(path: Optional[str], now: dt.datetime) -> Dict[str, Any]:
    ref = _reference(path, "sweep-outcome")
    if path is None:
        return _unavailable(ref, "not_configured")
    try:
        data, issue, outcome_sha = load_input(path)
    except (ValueError, TypeError, OverflowError, RecursionError):
        data, issue, outcome_sha = None, "sweep_outcome_invalid", None
    if issue is not None or not isinstance(data, dict):
        return _unavailable(ref, issue or "sweep_outcome_invalid", observed_at=_stamp(now), sha256=outcome_sha)
    digest = data.get("daily_digest")
    if not isinstance(digest, dict):
        return _unavailable(ref, "daily_digest_missing", observed_at=_stamp(now), sha256=outcome_sha)
    declared_path = digest.get("path")
    declared_sha = digest.get("sha256")
    if not isinstance(declared_path, str) or not declared_path or not _hash_ok(declared_sha):
        return _unavailable(ref, "daily_digest_declaration_invalid", observed_at=_stamp(now), sha256=outcome_sha)
    outcome_path = Path(path).expanduser()
    target = Path(declared_path)
    if not target.is_absolute():
        target = outcome_path.parent / target
    raw, read_issue = _read_secure_regular(target, DIGEST_MAX_BYTES)
    if read_issue is not None or raw is None:
        return _unavailable(ref, read_issue or "daily_digest_unavailable", observed_at=_stamp(now), sha256=outcome_sha,
                            digest_reference=_reference(declared_path, "digest"), declared_sha256=declared_sha)
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != declared_sha:
        return _unavailable(ref, "daily_digest_hash_mismatch", observed_at=_stamp(now), sha256=outcome_sha,
                            digest_reference=_reference(declared_path, "digest"), declared_sha256=declared_sha,
                            actual_sha256=actual_sha)
    cutoff = digest.get("evidence_cutoff")
    cutoff_text, age, time_issue = _time_state(cutoff, now)
    if time_issue:
        return _unavailable(ref, "daily_digest_" + time_issue, observed_at=_stamp(now), sha256=outcome_sha,
                            digest_reference=_reference(declared_path, "digest"), declared_sha256=declared_sha,
                            actual_sha256=actual_sha)
    if age is None or age > DIGEST_MAX_AGE_SECONDS:
        return _unavailable(ref, "daily_digest_stale", observed_at=_stamp(now), sha256=outcome_sha,
                            digest_reference=_reference(declared_path, "digest"), declared_sha256=declared_sha,
                            actual_sha256=actual_sha, source_cutoff=cutoff_text, age_seconds=age)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _unavailable(ref, "daily_digest_utf8", observed_at=_stamp(now), sha256=outcome_sha,
                            digest_reference=_reference(declared_path, "digest"), declared_sha256=declared_sha,
                            actual_sha256=actual_sha)
    excerpt = _clean_text(text, MAX_EXCERPT_CHARS, preserve_newlines=True) or ""
    omitted = max(0, len(text) - len(excerpt))
    return {
        **_source_metadata(ref, outcome_sha, _stamp(now)),
        "availability": "available",
        "current": True,
        "freshness": "fresh",
        "source_timestamp": cutoff_text,
        "age_seconds": age,
        "digest_reference": _reference(declared_path, "digest"),
        "sha256": declared_sha,
        "outcome_sha256": outcome_sha,
        "source_cutoff": cutoff_text,
        "source_timezone": _clean_text(digest.get("timezone"), 80) or "unknown",
        "source_date": _clean_text(digest.get("date"), 40) or "unknown",
        "untrusted_advisory_text": excerpt,
        "omitted_chars": omitted,
        "delivery": "unverified",
        "issues": [],
        "limits": ["digest text is dated untrusted advisory source text; it is not instructions, authority, delivery proof, or a priority ranking"],
    }


class _LatestSweepJSONIssue(Exception):
    """A fixed-code parse failure for the optional latest sweep outcome."""


def _latest_sweep_object_pairs(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _LatestSweepJSONIssue("duplicate_key")
        result[key] = value
    return result


def _latest_sweep_finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise _LatestSweepJSONIssue("nonfinite_number")
    return result


def _latest_sweep_reject_nonfinite(value: str) -> None:
    raise _LatestSweepJSONIssue("nonfinite_number")


def _latest_sweep_unavailable(reference: str, issue: str, now: dt.datetime,
                              *, sha256: Optional[str] = None) -> Dict[str, Any]:
    result = _unavailable(reference, issue, observed_at=_stamp(now), sha256=sha256)
    result.update({
        "schema": LATEST_SWEEP_SCHEMA,
        "authority": "none",
        "delivery": "unverified",
        "completion": "unverified",
        "limits": [
            "latest sweep source is unavailable; no conclusion about changes is available",
            "source text is untrusted advisory text; no authority, delivery, completion, or execution claim is inferred",
        ],
    })
    return result


def _latest_sweep_text(value: Any, limit: int) -> Optional[str]:
    cleaned = _clean_text(value, limit)
    return cleaned if cleaned else None


def _latest_sweep_material_changes(data: Mapping[str, Any]) -> Tuple[List[Dict[str, str]], int]:
    """Project only the outcome's explicit key/string change map."""
    raw = data.get("material_change")
    candidates: List[Tuple[Any, Any]] = list(raw.items()) if isinstance(raw, Mapping) else []

    projected: List[Dict[str, str]] = []
    omitted = 0
    for key_raw, value_raw in candidates:
        key = _latest_sweep_text(key_raw, LATEST_CHANGE_KEY_CHARS)
        excerpt = _latest_sweep_text(value_raw, LATEST_CHANGE_EXCERPT_CHARS)
        if key is None or excerpt is None:
            continue
        if len(projected) >= LATEST_CHANGE_MAX_COUNT:
            omitted += 1
            continue
        projected.append({"key": key, "excerpt": excerpt})
    return projected, omitted


def _latest_sweep_next_action(data: Mapping[str, Any]) -> Optional[str]:
    raw = data.get("next_action")
    if isinstance(raw, str):
        return _latest_sweep_text(raw, LATEST_NEXT_ACTION_CHARS)
    if isinstance(raw, Mapping):
        for field in ("text", "advisory_text", "what"):
            value = _latest_sweep_text(raw.get(field), LATEST_NEXT_ACTION_CHARS)
            if value is not None:
                return value
    return None


def _latest_sweep_coverage(value: Any) -> Tuple[Any, str]:
    if isinstance(value, str):
        return _latest_sweep_text(value, MAX_SOURCE_TEXT), "unknown"
    if not isinstance(value, Mapping):
        return None, "unknown"
    projected: Dict[str, str] = {}
    for key_raw, state_raw in value.items():
        if len(projected) >= LATEST_COVERAGE_MAX_SOURCES:
            break
        if not isinstance(key_raw, str) or not key_raw or len(key_raw) > 80:
            continue
        key = key_raw.strip()
        if not key or not all(char.isalnum() or char in "._:/-" for char in key):
            continue
        if not isinstance(state_raw, Mapping):
            continue
        state = _latest_sweep_text(state_raw.get("state"), MAX_SOURCE_TEXT)
        if state is not None:
            projected[key] = state
    return (projected or None), ("partial" if projected else "unknown")


def _load_latest_sweep(path: Optional[str], now: dt.datetime) -> Dict[str, Any]:
    """Load one explicit IRIS sweep outcome without following linked paths."""
    reference = _reference(path, "latest-sweep-outcome")
    if path is None:
        return _latest_sweep_unavailable(reference, "not_configured", now)
    try:
        candidate = Path(path).expanduser()
    except (TypeError, ValueError, RuntimeError):
        return _latest_sweep_unavailable(reference, "declared_path_invalid", now)
    raw, read_issue = _read_secure_regular(candidate, SWEEP_MAX_BYTES)
    if read_issue is not None or raw is None:
        return _latest_sweep_unavailable(reference, read_issue or "declared_file_unavailable", now)
    outcome_sha = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _latest_sweep_unavailable(reference, "latest_sweep_utf8", now, sha256=outcome_sha)
    try:
        data = json.loads(
            text,
            object_pairs_hook=_latest_sweep_object_pairs,
            parse_constant=_latest_sweep_reject_nonfinite,
            parse_float=_latest_sweep_finite_float,
        )
    except _LatestSweepJSONIssue as exc:
        return _latest_sweep_unavailable(reference, "latest_sweep_" + str(exc), now, sha256=outcome_sha)
    except (ValueError, RecursionError):
        return _latest_sweep_unavailable(reference, "latest_sweep_malformed_json", now, sha256=outcome_sha)
    if not isinstance(data, dict):
        return _latest_sweep_unavailable(reference, "latest_sweep_not_object", now, sha256=outcome_sha)
    if data.get("schema") != LATEST_SWEEP_SCHEMA:
        return _latest_sweep_unavailable(reference, "latest_sweep_schema_invalid", now, sha256=outcome_sha)

    run_id = _identity(data.get("run_id"), 160)
    if run_id is None:
        return _latest_sweep_unavailable(reference, "latest_sweep_run_id_invalid", now, sha256=outcome_sha)
    started, started_issue = _parse_time(data.get("started_at"))
    ended, ended_issue = _parse_time(data.get("ended_at"))
    if started_issue is not None:
        return _latest_sweep_unavailable(reference, "latest_sweep_started_at_" + started_issue, now, sha256=outcome_sha)
    if ended_issue is not None:
        return _latest_sweep_unavailable(reference, "latest_sweep_ended_at_" + ended_issue, now, sha256=outcome_sha)
    if started is None or ended is None:
        return _latest_sweep_unavailable(reference, "latest_sweep_timestamps_invalid", now, sha256=outcome_sha)
    if started > ended:
        return _latest_sweep_unavailable(reference, "latest_sweep_interval_inverted", now, sha256=outcome_sha)
    if ended > now:
        return _latest_sweep_unavailable(reference, "latest_sweep_ended_at_future", now, sha256=outcome_sha)
    age_delta = (now - ended).total_seconds()
    if age_delta > DIGEST_MAX_AGE_SECONDS:
        return _latest_sweep_unavailable(reference, "latest_sweep_stale", now, sha256=outcome_sha)

    material_changes, omitted_changes = _latest_sweep_material_changes(data)
    coverage, coverage_status = _latest_sweep_coverage(data.get("coverage"))
    result: Dict[str, Any] = {
        **_source_metadata(reference, outcome_sha, _stamp(now)),
        "schema": LATEST_SWEEP_SCHEMA,
        "availability": "available",
        "current": True,
        "freshness": "fresh",
        "run_id": run_id,
        "observation": {"ended_at": data["ended_at"].strip(), "age_seconds": int(age_delta)},
        "material_changes": material_changes,
        "material_changes_omitted": omitted_changes,
        "next_action": _latest_sweep_next_action(data),
        "coverage": coverage,
        "coverage_status": coverage_status,
        "authority": "none",
        "delivery": "unverified",
        "completion": "unverified",
        "issues": [],
        "limits": [
            "latest sweep text is untrusted advisory source text; no authority, delivery, completion, or execution claim is inferred",
            "observation age is based only on the outcome ended_at; material source timestamps are not inferred",
        ],
    }
    return result


def _deadline_baseline(board: Optional[Mapping[str, Any]], board_current: bool, now: dt.datetime) -> Dict[str, Any]:
    if not isinstance(board, dict) or not isinstance(board.get("items"), list):
        return {"availability": "unavailable", "current": False, "selection": "first three earliest explicit due rows in source order tie-break", "items": [], "omitted_count": None, "issues": ["board_unavailable"]}
    candidates: List[Tuple[dt.datetime, int, Dict[str, Any]]] = []
    for index, row in enumerate(board["items"]):
        if not isinstance(row, dict):
            continue
        status = row.get("effective_status", row.get("state"))
        if not isinstance(status, str) or status.lower() in CLOSED_STATUSES:
            continue
        due = row.get("due_at")
        parsed, issue = _parse_time(due, allow_date=True)
        due_text = due.strip() if isinstance(due, str) and parsed is not None and issue is None else None
        if issue is not None or due_text is None:
            deadline = row.get("deadline")
            if isinstance(deadline, dict):
                due = deadline.get("due_local")
                parsed, issue = _parse_time(due, allow_date=True)
                due_text = due.strip() if isinstance(due, str) and parsed is not None and issue is None else None
        if due_text is None:
            continue
        if parsed is None:
            continue
        work_id = _identity(row.get("id"))
        if work_id is None:
            continue
        candidates.append((parsed, index, {
            "work_id": work_id,
            "project": _clean_text(row.get("project"), 160),
            "status": _clean_text(status, 80) or "unknown",
            "due_at": due_text,
            "due_bucket": _clean_text(row.get("due_bucket"), 40),
            "due_label": _clean_text(row.get("due_label"), 120),
        }))
    candidates.sort(key=lambda value: (value[0], value[1]))
    rows = [value[2] for value in candidates[:MAX_DEADLINE_ROWS]]
    return {
        "availability": "available",
        "current": bool(board_current),
        "basis": "deterministic explicit due fields only",
        "selection": "earliest explicit due rows; source order breaks ties",
        "ranking": "none",
        "items": rows,
        "omitted_count": max(0, len(candidates) - MAX_DEADLINE_ROWS),
        "issues": [] if board_current else ["board_not_current"],
    }


def _shrink(brief: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce optional detail while retaining the current decision evidence."""
    def encoded() -> int:
        return len(json.dumps(brief, ensure_ascii=False, separators=(",", ":")))

    def add_limit(text: str) -> None:
        limits = brief.setdefault("limits", [])
        if text not in limits:
            limits.append(text)

    brief["limits"] = _unique_limits(brief.get("limits"))
    work = brief.get("work") if isinstance(brief.get("work"), dict) else None
    iris = brief.get("iris") if isinstance(brief.get("iris"), dict) else None
    focus = iris.get("focus") if isinstance(iris, dict) and isinstance(iris.get("focus"), dict) else None
    digest = brief.get("sweep_digest") if isinstance(brief.get("sweep_digest"), dict) else None
    latest = brief.get("latest_sweep") if isinstance(brief.get("latest_sweep"), dict) else None

    if encoded() > MAX_OUTPUT_CHARS:
        # Before dropping source rows, compact duplicated transport metadata.
        if isinstance(iris, dict):
            board = iris.get("board")
            if isinstance(board, dict):
                for side in ("before", "after"):
                    value = board.get(side)
                    if isinstance(value, dict):
                        board[side] = {"reference": value.get("reference"), "sha256": value.get("sha256"),
                                       "http_status": value.get("http_status")}
            health = iris.get("health")
            if isinstance(health, dict):
                health["coverage"] = None
        add_limit("transport metadata compacted to preserve bounded current detail")

    if encoded() > MAX_OUTPUT_CHARS and isinstance(focus, dict):
        rows = focus.get("requests") if isinstance(focus.get("requests"), list) else []
        total = focus.get("request_count") if type(focus.get("request_count")) is int else len(rows)
        decision = [row for row in rows if isinstance(row, dict) and row.get("group") == "decision"]
        chosen: List[Dict[str, Any]] = []
        if decision:
            exact = next((row for row in decision if row.get("current_match") == "exact" and row.get("source_applicability") == "current"), decision[0])
            chosen.append(exact)
        for row in rows:
            if isinstance(row, dict) and row not in chosen:
                chosen.append(row)
        if len(chosen) > 2:
            chosen = chosen[:2]
        focus["requests"] = chosen
        focus["omitted_requests"] = max(0, total - len(chosen))
        add_limit("non-decision focus rows omitted by output bound")


    if encoded() > MAX_OUTPUT_CHARS and isinstance(latest, dict):
        next_action = latest.get("next_action")
        if isinstance(next_action, str) and len(next_action) > 240:
            latest["next_action"] = next_action[:240].rstrip()
            latest["next_action_omitted_chars"] = latest.get("next_action_omitted_chars", 0) + len(next_action) - len(latest["next_action"])
            add_limit("latest sweep next-action prose shortened by output bound; omitted character count retained")
        changes = latest.get("material_changes")
        if isinstance(changes, list):
            omitted_chars = 0
            for change in changes:
                if not isinstance(change, dict) or not isinstance(change.get("excerpt"), str):
                    continue
                excerpt = change["excerpt"]
                if len(excerpt) > 240:
                    change["excerpt"] = excerpt[:240].rstrip()
                    omitted_chars += len(excerpt) - len(change["excerpt"])
            if omitted_chars:
                latest["material_changes_omitted_chars"] = latest.get("material_changes_omitted_chars", 0) + omitted_chars
                add_limit("latest sweep material-change excerpts shortened by output bound; omitted character count retained")

    if encoded() > MAX_OUTPUT_CHARS and isinstance(digest, dict):
        text = digest.get("untrusted_advisory_text")
        if isinstance(text, str) and len(text) > 1000:
            digest["untrusted_advisory_text"] = text[:1000]
            digest["omitted_chars"] = digest.get("omitted_chars", 0) + len(text) - 1000
            add_limit("digest excerpt shortened to the minimum retained evidence")

    if encoded() > MAX_OUTPUT_CHARS and isinstance(focus, dict):
        for request in focus.get("requests", []):
            if isinstance(request, dict):
                for key in ("question", "recommendation"):
                    if isinstance(request.get(key), str):
                        request[key] = request[key][:160]
        add_limit("focus wording shortened by output bound")

    if encoded() > MAX_OUTPUT_CHARS and isinstance(work, dict):
        for lane in work.get("selected_lanes", []):
            if isinstance(lane, dict) and isinstance(lane.get("step"), str):
                lane["step"] = lane["step"][:180]
        add_limit("lane wording shortened by output bound")

    if encoded() > MAX_OUTPUT_CHARS and isinstance(iris, dict):
        local = iris.get("local_agent")
        if isinstance(local, dict) and isinstance(local.get("activity"), dict):
            local["activity"]["summary"] = str(local["activity"].get("summary", ""))[:200]
        add_limit("local activity summary shortened by output bound")

    if encoded() > MAX_OUTPUT_CHARS:
        # Keep the useful content before duplicated explanation. An available
        # priority digest must not silently become an empty successful source.
        sections = [work, iris, latest, digest, brief.get("deadline_baseline")]
        if isinstance(iris, dict):
            sections.extend(iris.values())
            local = iris.get("local_agent")
            activity = local.get("activity") if isinstance(local, dict) else None
            review = activity.get("review") if isinstance(activity, dict) else None
            if isinstance(review, dict) and isinstance(review.get("summary"), str):
                review["summary"] = review["summary"][:200]
        for section in sections:
            if isinstance(section, dict):
                section.pop("limits", None)
        add_limit("detail shortened; transport is not human reading or an answer; digest is advisory text, not a priority ranking")

    if encoded() > MAX_OUTPUT_CHARS and isinstance(focus, dict):
        rows = focus.get("requests", [])
        if len(rows) > 1:
            focus["requests"] = rows[:1]
            focus["omitted_requests"] = focus.get("omitted_requests", 0) + len(rows) - 1
            add_limit("additional focus detail omitted; counts retain the full request inventory")

    if encoded() > MAX_OUTPUT_CHARS:
        baseline = brief.get("deadline_baseline")
        if isinstance(baseline, dict) and isinstance(baseline.get("items"), list):
            baseline["omitted_count"] = baseline.get("omitted_count", 0) + len(baseline["items"])
            baseline["items"] = []
            add_limit("optional deadline-baseline rows omitted before priority evidence")
    if encoded() > MAX_OUTPUT_CHARS and isinstance(work, dict):
        for lane in work.get("selected_lanes", []):
            if isinstance(lane, dict):
                for key in ("waiting_on_anthony", "blocked"):
                    values = lane.get(key)
                    if isinstance(values, list) and len(values) > 1:
                        lane[key] = values[:1]
                        lane[key + "_omitted"] = lane.get(key + "_omitted", 0) + len(values) - 1
        summary = work.get("sources", {}).get("boot_pack", {}).get("summary", {})
        warnings = summary.get("warning_summaries") if isinstance(summary, dict) else None
        if isinstance(warnings, list) and len(warnings) > 1:
            summary["warning_summaries"] = warnings[:1]
            summary["warning_summaries_omitted"] = len(warnings) - 1
        add_limit("additional lane and warning prose omitted before current priorities; counts retained")
    if encoded() > MAX_OUTPUT_CHARS and isinstance(iris, dict):
        priority = iris.get("priorities")
        if isinstance(priority, dict) and priority.get("rows"):
            omitted_links = 0
            for row in priority["rows"]:
                omitted_links += int(row.get("url") is not None)
                row["url"] = None
                for key in ("label", "title", "owner", "reason", "estimate_basis"):
                    if isinstance(row.get(key), str) and len(row[key]) > 60:
                        row[key] = row[key][:50].rstrip() + " [excerpt]"
            priority["omitted_links"] = omitted_links
            add_limit("priority prose excerpted and links omitted by output bound; row identities, timestamps and counts retained")
            for row in priority["rows"]:
                if isinstance(row, dict):
                    row.pop("estimate_basis", None)
                    for key, limit in (("label", 32), ("title", 32), ("owner", 24), ("reason", 32)):
                        if isinstance(row.get(key), str) and len(row[key]) > limit:
                            row[key] = row[key][:limit].rstrip() + " [excerpt]"
            for key in ("coverage_note", "capacity"):
                if isinstance(priority.get(key), str) and len(priority[key]) > 80:
                    priority[key] = priority[key][:80].rstrip() + " [excerpt]"
            add_limit("optional priority explanation fields shortened while preserving all current row identities")

    if encoded() > MAX_OUTPUT_CHARS and isinstance(latest, dict):
        next_action = latest.get("next_action")
        if isinstance(next_action, str) and len(next_action) > 100:
            latest["next_action"] = next_action[:100].rstrip()
            latest["next_action_omitted_chars"] = latest.get("next_action_omitted_chars", 0) + len(next_action) - len(latest["next_action"])
            add_limit("latest sweep next-action detail shortened further; omitted character count retained")
        changes = latest.get("material_changes")
        if isinstance(changes, list):
            omitted_chars = 0
            for change in changes:
                if not isinstance(change, dict) or not isinstance(change.get("excerpt"), str):
                    continue
                excerpt = change["excerpt"]
                if len(excerpt) > 240:
                    change["excerpt"] = excerpt[:230].rstrip() + " [excerpt]"
                    omitted_chars += len(excerpt) - len(change["excerpt"])
            if omitted_chars:
                latest["material_changes_omitted_chars"] = latest.get("material_changes_omitted_chars", 0) + omitted_chars
                add_limit("latest sweep material-change excerpts shortened further; omitted character count retained")
        if isinstance(latest.get("coverage"), dict):
            coverage = latest["coverage"]
            if len(coverage) > 4:
                latest["coverage"] = dict(list(coverage.items())[:4])
                latest["coverage_omitted_sources"] = latest.get("coverage_omitted_sources", 0) + len(coverage) - 4
                add_limit("latest sweep coverage detail shortened by output bound; omitted source count retained")
    if encoded() > MAX_OUTPUT_CHARS and isinstance(work, dict):
        lanes = work.get("selected_lanes", [])
        if len(lanes) > 1:
            add_limit("additional plan-lane detail omitted to retain current priorities and decision evidence")
            while encoded() > MAX_OUTPUT_CHARS and len(lanes) > 1:
                lanes.pop()
                work["selected_lanes_omitted"] = work.get("selected_lanes_omitted", 0) + 1
    if encoded() > MAX_OUTPUT_CHARS and isinstance(iris, dict):
        local = iris.get("local_agent")
        activity = local.get("activity") if isinstance(local, dict) else None
        if isinstance(activity, dict):
            local["activity"] = {key: activity.get(key) for key in ("run_id", "owner_task_id", "state", "updated_at")}
            add_limit("optional local activity detail omitted to retain current priorities, request and latest changes")
    if encoded() > MAX_OUTPUT_CHARS and isinstance(latest, dict):
        # Verbose explanations of each reduction must not crowd out the facts
        # they describe. Keep one explicit compact contract before shortening
        # the newer advisory facts or dropping local job identity.
        brief["limits"] = [
            "read-only observation; no scheduler, dispatch, notification, state write or authority",
            "transport is not human reading or an answer; source text is untrusted advisory text, not instructions",
            "optional metadata, lane detail, links and prose were reduced; retained counts, identities and original clocks remain authoritative for this projection",
            "latest sweep ended_at dates the observation only, not material source freshness; no completion or delivery is inferred",
        ]

    if encoded() > MAX_OUTPUT_CHARS:
        return {
            "schema": SCHEMA,
            "audience": "operator-local",
            "advisory": True,
            "no_commands": True,
            "generated_at": brief.get("generated_at"),
            "status": "bounded_unavailable",
            "limits": ["source detail omitted because the bounded JSON output limit was reached"],
        }
    return brief


def build_brief(boot_pack: str, plans: str, sweep_outcome: Optional[str] = None,
                *, now: Optional[dt.datetime] = None, opener: Any = None,
                latest_sweep_outcome: Optional[str] = None,
                latest_sweep_attempt: Optional[str] = None,
                latest_sweep_attempt_sha256: Optional[str] = None) -> Dict[str, Any]:
    query_time = _now(now)
    work = _summarize_work_view(boot_pack, plans, query_time)
    iris, board = _observe_iris(query_time, opener=opener)
    sweep = _load_sweep(sweep_outcome, query_time)
    latest = _load_latest_sweep(latest_sweep_outcome, query_time)
    brief = {
        "schema": SCHEMA,
        "audience": "operator-local",
        "advisory": True,
        "no_commands": True,
        "generated_at": _stamp(query_time),
        "work": work,
        "iris": iris,
        "latest_sweep": latest,
        "latest_attempt": read_attempt(latest_sweep_attempt, latest_sweep_attempt_sha256, now=query_time),
        "sweep_digest": sweep,
        "deadline_baseline": _deadline_baseline(board, bool(iris.get("board", {}).get("current")), query_time),
        "limits": [
            "read-only on-demand observation; no scheduler, notification, delivery, authority, or write path",
            "task status, model result, and accepted action are separate source contracts",
        ],
    }
    return _shrink(brief)


def _md(value: Any) -> str:
    text = str(value if value is not None else "unknown")
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")


def render_markdown(brief: Mapping[str, Any]) -> str:
    lines = ["# Current situation brief", "", "Read-only advisory projection; source-reported evidence retains its own freshness and authority limits.", ""]
    lines.append("As of: `%s`" % _md(brief.get("generated_at")))
    priority = brief.get("iris", {}).get("priorities", {})
    lines.extend(["", "## Current planning priorities", "- status `%s`; current `%s`; reason `%s`" % (
        _md(priority.get("status")), _md(priority.get("current")), _md(priority.get("reason")))])
    lines.append("- Reviewed source advice only; no approval or execution authority.")
    lines.append("- Coverage: %s — %s; evidence cutoff `%s`; daily digest cutoff `%s`." % (
        _md(priority.get("coverage")), _md(priority.get("coverage_note")),
        _md(priority.get("evidence_cutoff")), _md(priority.get("digest_cutoff"))))
    if priority.get("binding_note"):
        lines.append("- %s." % _md(priority["binding_note"]))
    for row in priority.get("rows", []):
        lines.append("  - %s: %s — %s; owner %s; mode `%s`; binding `%s`" % (
            _md(row.get("label")), _md(row.get("title")), _md(row.get("reason")),
            _md(row.get("owner")), _md(row.get("mode")), _md(row.get("binding_status"))))
    lines.append("- omitted priority rows: %s" % _md(priority.get("omitted_rows")))
    latest = brief.get("latest_sweep", {}) if isinstance(brief.get("latest_sweep"), dict) else {}
    lines.extend(["", "## Latest sweep update", "- availability `%s`; freshness `%s`; run `%s`" % (
        _md(latest.get("availability")), _md(latest.get("freshness")), _md(latest.get("run_id")))])
    if latest.get("availability") == "available":
        observation = latest.get("observation") if isinstance(latest.get("observation"), dict) else {}
        lines.append("- observation ended `%s`; age `%s` seconds" % (
            _md(observation.get("ended_at")), _md(observation.get("age_seconds"))))
        lines.append("- material changes (untrusted advisory source text; no authority, delivery, or completion claim):")
        changes = latest.get("material_changes") if isinstance(latest.get("material_changes"), list) else []
        for change in changes:
            if isinstance(change, dict):
                lines.append("  - `%s`: %s" % (_md(change.get("key")), _md(change.get("excerpt"))))
        lines.append("- omitted material changes: %s; omitted change characters: %s" % (
            _md(latest.get("material_changes_omitted")), _md(latest.get("material_changes_omitted_chars", 0))))
        lines.append("- next action (untrusted advisory text): %s" % _md(latest.get("next_action")))
        lines.append("- coverage: %s" % _md(latest.get("coverage")))
    else:
        issues = latest.get("issues") if isinstance(latest.get("issues"), list) else []
        lines.append("- unavailable; no conclusion about changes is available; issues `%s`" % _md(", ".join(issues)))
    attempt = brief.get("latest_attempt", {})
    lines.extend(["", "## Latest scheduled attempt",
                  "- availability `%s`; status `%s`; trigger `%s`; closed `%s`; reason `%s`" % (
                      _md(attempt.get("availability")), _md(attempt.get("status")),
                      _md(attempt.get("trigger_at")), _md(attempt.get("closed_at")), _md(attempt.get("reason"))),
                  "- Attempt evidence does not establish source freshness, human delivery or successful follow-through."])
    work = brief.get("work", {}) if isinstance(brief.get("work"), dict) else {}
    lines.extend(["", "## Fully Aware", "- snapshot: `%s`" % _md(work.get("snapshot_id"))])
    for name in ("boot_pack", "plans"):
        source = work.get("sources", {}).get(name, {}) if isinstance(work.get("sources"), dict) else {}
        lines.append("- %s: %s, freshness `%s`, current `%s`, age `%s`" %
                     (name, _md(source.get("availability")), _md(source.get("freshness")), _md(source.get("current")), _md(source.get("age_seconds"))))
    lanes = work.get("selected_lanes", [])
    if isinstance(lanes, list):
        for lane in lanes:
            if isinstance(lane, dict):
                lines.append("  - %s: %s" % (_md(lane.get("name")), _md(lane.get("step"))))
    iris = brief.get("iris", {}) if isinstance(brief.get("iris"), dict) else {}
    health = iris.get("health", {}) if isinstance(iris.get("health"), dict) else {}
    board = iris.get("board", {}) if isinstance(iris.get("board"), dict) else {}
    lines.extend(["", "## IRIS", "- service: %s; current `%s`; freshness `%s`" % (_md(health.get("status")), _md(health.get("current")), _md(health.get("freshness"))),
                  "- board: %s items, %s activity; current `%s`" % (_md((board.get("shape") or {}).get("items") if isinstance(board.get("shape"), dict) else None), _md((board.get("shape") or {}).get("activity") if isinstance(board.get("shape"), dict) else None), _md(board.get("current")))])
    focus = iris.get("focus", {}) if isinstance(iris.get("focus"), dict) else {}
    lines.append("- focus: %s requests; authority `%s`; delivery `%s`" % (_md(focus.get("request_count")), _md(focus.get("authority")), _md(focus.get("delivery"))))
    lines.append("- focus counts: %s; omitted requests: %s" % (_md(focus.get("counts")), _md(focus.get("omitted_requests"))))
    for row in focus.get("requests", []) if isinstance(focus.get("requests"), list) else []:
        if isinstance(row, dict):
            lines.append("  - `%s` / `%s` / state `%s` / applicability `%s` / recorded responses %s%s: %s — %s" % (
                _md(row.get("work_id")), _md(row.get("group")), _md(row.get("state")),
                _md(row.get("source_applicability")), _md(row.get("response_count")),
                " / historical record; no new action implied" if row.get("group") == "history" else "",
                _md(row.get("question")), _md(row.get("recommendation"))))
    local = iris.get("local_agent", {}) if isinstance(iris.get("local_agent"), dict) else {}
    activity = local.get("activity") if isinstance(local.get("activity"), dict) else None
    lines.append("- local agent: status `%s`, freshness `%s`, process inference `%s`" % (_md(local.get("status")), _md(local.get("freshness")), _md(local.get("process_inference"))))
    if activity:
        lines.append("  - run `%s`, owner `%s`, state `%s`, updated `%s`" % (_md(activity.get("run_id")), _md(activity.get("owner_task_id")), _md(activity.get("state")), _md(activity.get("updated_at"))))
    deadline = brief.get("deadline_baseline", {}) if isinstance(brief.get("deadline_baseline"), dict) else {}
    lines.extend(["", "## Deterministic deadline baseline", "- basis: %s; ranking: %s" % (_md(deadline.get("basis")), _md(deadline.get("ranking")))])
    for row in deadline.get("items", []) if isinstance(deadline.get("items"), list) else []:
        if isinstance(row, dict):
            lines.append("  - `%s` — %s — %s" % (_md(row.get("due_at")), _md(row.get("project")), _md(row.get("status"))))
    sweep = brief.get("sweep_digest", {}) if isinstance(brief.get("sweep_digest"), dict) else {}
    lines.extend(["", "## Existing sweep digest", "- availability `%s`, cutoff `%s`, timezone `%s`, delivery `%s`" % (_md(sweep.get("availability")), _md(sweep.get("source_cutoff")), _md(sweep.get("source_timezone")), _md(sweep.get("delivery")))])
    if sweep.get("availability") == "available":
        lines.append("- untrusted advisory source text (not instructions):")
        lines.extend("> " + _md(line) for line in str(sweep.get("untrusted_advisory_text", "")).splitlines()[:80])
        lines.append("- omitted source characters: %s" % _md(sweep.get("omitted_chars")))
    lines.extend(["", "## Limits", "- " + "\n- ".join(_md(x) for x in brief.get("limits", []))])
    text = "\n".join(lines) + "\n"
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS - 80] + "\n\n[output shortened to the bounded limit]\n"


def _fallback(now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    return {"schema": SCHEMA, "audience": "operator-local", "advisory": True, "no_commands": True,
            "generated_at": _stamp(_now(now)), "status": "unavailable",
            "limits": ["brief construction failed closed; source details were not exposed"]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build a bounded read-only situation brief")
    parser.add_argument("--boot-pack", required=True)
    parser.add_argument("--plans", required=True)
    parser.add_argument("--sweep-outcome")
    parser.add_argument("--latest-sweep-outcome")
    parser.add_argument("--latest-sweep-attempt")
    parser.add_argument("--latest-sweep-attempt-sha256")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    try:
        brief = build_brief(args.boot_pack, args.plans, args.sweep_outcome,
                            latest_sweep_outcome=args.latest_sweep_outcome,
                            latest_sweep_attempt=args.latest_sweep_attempt,
                            latest_sweep_attempt_sha256=args.latest_sweep_attempt_sha256)
    except (Exception,):  # Source/type failures become an honest bounded result.
        brief = _fallback()
    if args.format == "markdown":
        sys.stdout.write(render_markdown(brief))
    else:
        sys.stdout.write(json.dumps(brief, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
