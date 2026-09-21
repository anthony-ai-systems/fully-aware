"""Project the public IRIS priority receipt into a bounded read-only view.

The loopback reader owns transport and observation metadata.  This module only
validates the already decoded ``/priority.json`` value and constructs a small
allowlisted object for the situation brief.  It never reads files, makes
requests, writes state, or infers authority from a priority row.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Dict, List, Mapping as TypingMapping, Optional, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


SCHEMA = "iris-priority-context/v1"
REFERENCE = "/priority.json"
ZONE = "America/Los_Angeles"
LOCAL_ZONE = ZoneInfo(ZONE)
MAX_CHECK_AGE_SECONDS = 300
MAX_EVIDENCE_AGE_SECONDS = 24 * 60 * 60
MAX_OUTPUT_CHARS = 4_000
EXCERPT = " [excerpt]"

MODES = {
    "anthony_judgment",
    "agent_preparation",
    "authorized_execution",
    "external_dependency",
}
BINDINGS = {"current", "unbound", "changed", "unavailable"}
STATUSES = {"current", "stale", "unavailable"}
REASONS = {"verified_evidence", "not_configured", "evidence_unavailable", "plan_expired"}
CURRENT_FIELDS = {"schema", "status", "reason", "checked_at", "plan"}
PLAN_FIELDS = {
    "local_date",
    "timezone",
    "prepared_at",
    "evidence_cutoff",
    "digest_cutoff",
    "digest_presentation",
    "coverage",
    "coverage_note",
    "capacity",
    "rows",
}
ROW_FIELDS = {
    "id",
    "label",
    "title",
    "owner",
    "reason",
    "mode",
    "estimated_minutes",
    "estimate_basis",
    "url",
    "binding_status",
}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
HEX64_RE = re.compile(r"^[a-f0-9]{64}$")
GOOGLE_DOC_RE = re.compile(r"^/document/d/[A-Za-z0-9_-]{10,120}/(?:edit|preview)$")
NOTION_RE = re.compile(r"^/(?:[A-Za-z0-9_-]+/)?(?:[A-Za-z0-9-]+-)?[a-fA-F0-9]{32}/?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _Invalid(ValueError):
    """Internal fail-closed marker; its message never reaches the caller."""


def _require(condition: bool) -> None:
    if not condition:
        raise _Invalid()


def _aware(value: Any) -> Tuple[dt.datetime, str]:
    """Parse an aware ISO timestamp and return UTC time plus original text."""
    _require(type(value) is str and 1 <= len(value) <= 64)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        raise _Invalid() from None
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None)
    try:
        return parsed.astimezone(dt.timezone.utc), value
    except (TypeError, ValueError, OverflowError):
        raise _Invalid() from None


def _date(value: Any) -> dt.date:
    _require(type(value) is str and DATE_RE.fullmatch(value) is not None)
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise _Invalid() from None
    _require(parsed.isoformat() == value)
    return parsed


def _safe_text(value: Any, source_limit: int) -> str:
    """Validate source prose and remove controls; renderers own escaping."""
    _require(type(value) is str and 1 <= len(value) <= source_limit)
    chars: List[str] = []
    for char in value:
        category = unicodedata.category(char)
        if category.startswith("C"):
            if char in "\r\n\t":
                chars.append(" ")
            continue
        chars.append(char)
    clean = " ".join("".join(chars).split())
    _require(bool(clean))
    return clean


def _excerpt(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    _require(limit > len(EXCERPT))
    return value[: limit - len(EXCERPT)].rstrip() + EXCERPT


def _safe_url(value: Any) -> Optional[str]:
    if value is None:
        return None
    _require(type(value) is str and 1 <= len(value) <= 300)
    try:
        _require(
            value.startswith("https://")
            and "?" not in value
            and "#" not in value
            and not any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
        )
        parsed = urlsplit(value)
        host = parsed.hostname
        _require(
            parsed.scheme == "https"
            and parsed.netloc in {"docs.google.com", "app.notion.com", "notion.so", "www.notion.so"}
            and host in {"docs.google.com", "app.notion.com", "notion.so", "www.notion.so"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and not parsed.query
            and not parsed.fragment
        )
        if host == "docs.google.com":
            _require(GOOGLE_DOC_RE.fullmatch(parsed.path) is not None)
        else:
            _require(NOTION_RE.fullmatch(parsed.path) is not None)
    except (ValueError, TypeError):
        raise _Invalid() from None
    return value


def _metadata(metadata: Any, now: dt.datetime) -> Tuple[Dict[str, Any], bool]:
    """Keep only the caller-owned route, digest and observation timestamp."""
    result: Dict[str, Any] = {"reference": REFERENCE, "sha256": None, "observed_at": None}
    if not isinstance(metadata, Mapping):
        return result, False
    # ``reference``/``sha256`` are the names used by situation_brief.  The
    # route/hash aliases are accepted only as input convenience and never
    # echoed as separate fields.
    reference = metadata.get("reference", metadata.get("route"))
    digest = metadata.get("sha256", metadata.get("hash"))
    observed = metadata.get("observed_at")
    if reference != REFERENCE:
        return result, False
    if digest is not None:
        if type(digest) is not str or HEX64_RE.fullmatch(digest) is None:
            return result, False
        result["sha256"] = digest
    try:
        parsed, text = _aware(observed)
        _require(parsed <= now)
    except _Invalid:
        return result, False
    result["observed_at"] = text
    return result, True


def _empty(metadata: Dict[str, Any], *, availability: str, status: str, reason: str) -> Dict[str, Any]:
    """Return a stable, row-free result with no source error text."""
    return {
        **metadata,
        "availability": availability,
        "current": False,
        "status": status,
        "reason": reason,
        "checked_at": None,
        "local_date": None,
        "timezone": None,
        "prepared_at": None,
        "evidence_cutoff": None,
        "digest_cutoff": None,
        "digest_presentation": None,
        "coverage": None,
        "coverage_note": None,
        "capacity": None,
        "rows": [],
        "omitted_rows": 0,
    }


def _output_plan(
    *,
    metadata: Dict[str, Any],
    checked_text: str,
    local_date: str,
    prepared_text: str,
    evidence_text: str,
    digest_text: str,
    coverage: str,
    coverage_note: str,
    capacity: str,
    digest_presentation: str,
    rows: List[Dict[str, Any]],
    omitted_rows: int,
    work_current: bool,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        **metadata,
        "availability": "available",
        "current": True,
        "status": "current",
        "reason": "verified_evidence",
        "checked_at": checked_text,
        "local_date": local_date,
        "timezone": ZONE,
        "prepared_at": prepared_text,
        "evidence_cutoff": evidence_text,
        "digest_cutoff": digest_text,
        "digest_presentation": digest_presentation,
        "coverage": coverage,
        "coverage_note": coverage_note,
        "capacity": capacity,
        "rows": rows,
        "omitted_rows": omitted_rows,
    }
    if not work_current:
        changed = False
        for row in rows:
            if row["binding_status"] == "current":
                row["binding_status"] = "unavailable"
                changed = True
        if changed:
            result["binding_note"] = "work binding unavailable because the surrounding board observation is not current"
    return _bound(result)


def _encoded_size(value: Dict[str, Any]) -> int:
    # Use the ordinary JSON representation as the bound; consumers may add
    # spaces rather than using compact separators.
    return len(json.dumps(value, ensure_ascii=True))


def _bound(value: Dict[str, Any]) -> Dict[str, Any]:
    """Keep valid max-size receipts bounded while retaining identity/time data."""
    prose: List[Tuple[Dict[str, Any], str, str, int]] = []
    fields = (("coverage_note", 160), ("capacity", 160))
    for key, limit in fields:
        prose.append((value, key, value[key], limit))
    for row in value.get("rows", []):
        for key, limit in (("label", 120), ("title", 120), ("owner", 120), ("reason", 180), ("estimate_basis", 120)):
            prose.append((row, key, row[key], limit))

    # Rebuild from the full sanitized prose each pass, so repeated shortening
    # never accumulates multiple excerpt markers.
    limits = {(id(owner), key): min(limit, len(text)) for owner, key, text, limit in prose}

    def rebuild() -> None:
        for owner, key, text, _limit in prose:
            owner[key] = _excerpt(text, limits[(id(owner), key)])

    rebuild()
    while _encoded_size(value) > MAX_OUTPUT_CHARS:
        candidates = [
            (len(owner[key]), owner, key, text)
            for owner, key, text, _limit in prose
            if limits[(id(owner), key)] > len(EXCERPT) + 1
        ]
        if not candidates:
            break
        _length, owner, key, text = max(candidates, key=lambda item: item[0])
        current = limits[(id(owner), key)]
        limits[(id(owner), key)] = max(len(EXCERPT) + 1, current - max(8, (_encoded_size(value) - MAX_OUTPUT_CHARS + 3) // 4))
        rebuild()

    # Safe URLs are useful but optional display detail.  In the pathological
    # max-size case, shedding them is preferable to shedding row identity or
    # evidence timestamps.  The URL was validated before this point.
    if _encoded_size(value) > MAX_OUTPUT_CHARS:
        for row in value.get("rows", []):
            row["url"] = None
        while _encoded_size(value) > MAX_OUTPUT_CHARS:
            candidates = [
                (len(owner[key]), owner, key, text)
                for owner, key, text, _limit in prose
                if limits[(id(owner), key)] > len(EXCERPT) + 1
            ]
            if not candidates:
                break
            _length, owner, key, text = max(candidates, key=lambda item: item[0])
            current = limits[(id(owner), key)]
            limits[(id(owner), key)] = max(len(EXCERPT) + 1, current - 4)
            rebuild()
    return value


def _validate_plan(plan: Any, now: dt.datetime) -> Tuple[Dict[str, Any], dt.datetime, dt.datetime, dt.datetime, dt.datetime]:
    _require(type(plan) is dict and set(plan) == PLAN_FIELDS)
    _require(plan["timezone"] == ZONE)
    local_date = _date(plan["local_date"])
    prepared, prepared_text = _aware(plan["prepared_at"])
    evidence, evidence_text = _aware(plan["evidence_cutoff"])
    digest, digest_text = _aware(plan["digest_cutoff"])
    _require(all(value <= now for value in (prepared, evidence, digest)))
    _require(digest <= evidence <= prepared)
    _require(evidence.astimezone(LOCAL_ZONE).date() == local_date)
    _require(digest.astimezone(LOCAL_ZONE).date() == local_date)
    _require(plan["digest_presentation"] == "recorded_unverified")
    _require(plan["coverage"] in {"complete", "partial"})
    coverage_note = _excerpt(_safe_text(plan["coverage_note"], 600), 160)
    capacity = _excerpt(_safe_text(plan["capacity"], 1000), 160)
    source_rows = plan["rows"]
    _require(type(source_rows) is list and 1 <= len(source_rows) <= 8)
    rows: List[Dict[str, Any]] = []
    ids = set()
    for source in source_rows:
        _require(type(source) is dict and set(source) == ROW_FIELDS)
        row_id = source["id"]
        _require(type(row_id) is str and ID_RE.fullmatch(row_id) is not None and row_id not in ids)
        ids.add(row_id)
        _require(source["mode"] in MODES and source["binding_status"] in BINDINGS)
        minutes = source["estimated_minutes"]
        _require(minutes is None or (type(minutes) is int and 1 <= minutes <= 480))
        url = _safe_url(source["url"])
        label = _excerpt(_safe_text(source["label"], 160), 120)
        title = _excerpt(_safe_text(source["title"], 160), 120)
        owner = _excerpt(_safe_text(source["owner"], 160), 120)
        reason = _excerpt(_safe_text(source["reason"], 600), 180)
        basis = _excerpt(_safe_text(source["estimate_basis"], 300), 120)
        rows.append({
            "id": row_id,
            "label": label,
            "title": title,
            "owner": owner,
            "reason": reason,
            "mode": source["mode"],
            "estimated_minutes": minutes,
            "estimate_basis": basis,
            "url": url,
            "binding_status": source["binding_status"],
        })
    projected = rows[:4]
    return ({
        "local_date": local_date.isoformat(),
        "prepared_at": prepared_text,
        "evidence_cutoff": evidence_text,
        "digest_cutoff": digest_text,
        "digest_presentation": plan["digest_presentation"],
        "coverage": plan["coverage"],
        "coverage_note": coverage_note,
        "capacity": capacity,
        "rows": projected,
        "omitted_rows": len(rows) - len(projected),
    }, prepared, evidence, digest, now)


def project_priority(value: Any, observed_at: dt.datetime, metadata: TypingMapping[str, Any], *, work_current: bool = True) -> Dict[str, Any]:
    """Return a bounded priority projection without authority or side effects.

    ``value`` is the decoded public ``/priority.json`` response.  ``observed_at``
    is the caller's aware observation clock, and ``metadata`` is limited to the
    caller-owned route/hash/observation fields.  Every malformed or unsafe
    source fails closed with a fixed reason and no rows.
    """
    try:
        _require(type(work_current) is bool)
        _require(isinstance(observed_at, dt.datetime))
        now = observed_at.astimezone(dt.timezone.utc)
        _require(observed_at.tzinfo is not None and observed_at.utcoffset() is not None)
        metadata_out, metadata_ok = _metadata(metadata, now)
        if not metadata_ok:
            return _empty(metadata_out, availability="unavailable", status="unavailable", reason="evidence_unavailable")
        if not isinstance(value, dict) or set(value) != CURRENT_FIELDS:
            return _empty(metadata_out, availability="unavailable", status="unavailable", reason="evidence_unavailable")
        source_status = value["status"]
        source_reason = value["reason"]
        _require(source_status in STATUSES and source_reason in REASONS and value["schema"] == SCHEMA)
        checked, checked_text = _aware(value["checked_at"])
        _require(checked <= now)
        if source_status in {"stale", "unavailable"}:
            _require(value["plan"] is None)
            if source_status == "stale":
                _require(source_reason == "plan_expired")
                return _empty(metadata_out, availability="available", status="stale", reason="plan_expired")
            _require(source_reason in {"not_configured", "evidence_unavailable"})
            return _empty(metadata_out, availability="unavailable", status="unavailable", reason=source_reason)

        _require(source_reason == "verified_evidence")
        plan, prepared, evidence, _digest, _ = _validate_plan(value["plan"], now)
        _require(prepared <= checked)
        age = (now - checked).total_seconds()
        local_today = now.astimezone(LOCAL_ZONE).date().isoformat()
        evidence_age = (now - evidence).total_seconds()
        if (age > MAX_CHECK_AGE_SECONDS or evidence_age > MAX_EVIDENCE_AGE_SECONDS
                or plan["local_date"] != local_today):
            return _empty(metadata_out, availability="available", status="stale", reason="plan_expired")
        return _output_plan(
            metadata=metadata_out,
            checked_text=checked_text,
            local_date=plan["local_date"],
            prepared_text=plan["prepared_at"],
            evidence_text=plan["evidence_cutoff"],
            digest_text=plan["digest_cutoff"],
            coverage=plan["coverage"],
            coverage_note=plan["coverage_note"],
            capacity=plan["capacity"],
            digest_presentation=plan["digest_presentation"],
            rows=plan["rows"],
            omitted_rows=plan["omitted_rows"],
            work_current=work_current,
        )
    except (_Invalid, AttributeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return _empty(
            {"reference": REFERENCE, "sha256": None, "observed_at": None},
            availability="unavailable",
            status="unavailable",
            reason="evidence_unavailable",
        )


__all__ = ["project_priority"]
