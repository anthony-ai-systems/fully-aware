"""Build a bounded, content-free health projection for local context.

The inputs are already-produced health or boot-pack snapshots.  This module is
deliberately a pure adapter: it does not discover paths, run producers, call
the network, read a store, or grant any authority.  Source prose, paths, IDs,
queue summaries, and unknown source fields never cross the projection.

``observed_at`` is when this projection is evaluated.  ``captured_at`` is the
time at which the caller captured the input snapshots.  Imprint's health CLI
does not carry its own timestamp, so its ``last_verified_at`` is explicitly
the capture time; taste retains its producer timestamp and Atlas reports the
oldest parsed pack/feed timestamp.
"""

from __future__ import annotations

import datetime as _datetime
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Tuple


CONTEXT_HEALTH_SCHEMA = "context-health/v1"
UTC = _datetime.timezone.utc

FRESH = "fresh"
STALE = "stale"
FUTURE = "future"
UNKNOWN = "unknown"

HEALTHY = "healthy"
DEGRADED = "degraded"

AVAILABLE = "available"
PARTIAL = "partial"
UNAVAILABLE = "unavailable"
INVALID = "invalid"

# These windows follow the existing producer contracts.  Imprint health is a
# daily captured artifact, the taste worker health is a short-lived worker
# receipt, the boot pack is a daily snapshot, Atlas's adjudication queue is a
# daily source with date-only ``as_of`` precision, and the Monday DECAY review
# is weekly.
IMPRINT_CAPTURE_MAX_AGE_SECONDS = 36 * 60 * 60
TASTE_MAX_AGE_SECONDS = 45 * 60
TASTE_FUTURE_GRACE_SECONDS = 5 * 60
BOOT_PACK_MAX_AGE_SECONDS = 36 * 60 * 60
ADJUDICATION_MAX_AGE_SECONDS = 36 * 60 * 60
DECAY_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

MAX_SAFE_COUNT = 1_000_000_000
MAX_REASONS = 32

IMPRINT_REASON_CODES = frozenset({
    "compiler_missing",
    "compiler_duplicate",
    "database_integrity_failed",
    "migration_invalid",
    "config_invalid",
    "required_backend_unavailable",
    "hook_parity_failed",
    "spool_stale",
    "quarantine_present",
    "hook_failures_present",
    "unsafe_permissions",
    "retrieval_budget_violated",
    "retrieval_budget_unapproved",
    "record_schema_unsupported",
    "hook_schema_unsupported",
    "domain_latch_unsafe",
    "projection_snapshot_missing",
    "disk_space_exhausted",
    "stale_lock_present",
    "compiler_lock_invalid",
    "abandoned_temp_present",
    "backup_unverified",
    "experimental_loop_stalled",
})

IMPRINT_COUNT_FIELDS = (
    "compiler_count",
    "spool_depth",
    "spool_unacknowledged_count",
    "quarantine_count",
    "hook_failure_count",
    "retrieval_omitted_count",
    "stale_lock_count",
    "abandoned_temp_count",
    "verified_backup_count",
    "invalid_backup_count",
)

TASTE_COUNT_FIELDS = (
    "errors",
    "retry_exhausted",
    "transcript_missing",
    "queue_bad_records",
    "batch_count",
    "batch_errors",
)

DECAY_COUNT_FIELDS = (
    "total",
    "reviewed",
    "needs_update",
    "deferred",
    "pending",
    "unchecked",
)

COMPONENT_NAMES = ("imprint", "taste", "atlas")

# ``degraded_reasons`` is source data.  Only this fixed set is allowed through.
FIXED_REASON_CODES = frozenset({
    *IMPRINT_REASON_CODES,
    "imprint_source_unavailable",
    "imprint_source_invalid",
    "imprint_schema_invalid",
    "imprint_status_invalid",
    "imprint_reasons_invalid",
    "imprint_reason_unknown",
    "imprint_metric_invalid",
    "imprint_health_degraded",
    "imprint_health_inconsistent",
    "imprint_capture_stale",
    "imprint_capture_future",
    "taste_source_unavailable",
    "taste_source_invalid",
    "taste_schema_invalid",
    "taste_status_invalid",
    "taste_count_invalid",
    "taste_count_relation_invalid",
    "taste_health_degraded",
    "taste_errors_present",
    "taste_health_inconsistent",
    "taste_timestamp_missing",
    "taste_timestamp_invalid",
    "taste_timestamp_naive",
    "taste_stale",
    "taste_future",
    "atlas_source_unavailable",
    "atlas_source_invalid",
    "atlas_schema_invalid",
    "atlas_sections_invalid",
    "atlas_pack_timestamp_missing",
    "atlas_pack_timestamp_invalid",
    "atlas_pack_timestamp_naive",
    "atlas_pack_stale",
    "atlas_pack_future",
    "atlas_decay_unavailable",
    "atlas_decay_invalid",
    "atlas_decay_cadence_invalid",
    "atlas_decay_timestamp_missing",
    "atlas_decay_timestamp_invalid",
    "atlas_decay_timestamp_naive",
    "atlas_decay_stale",
    "atlas_decay_future",
    "atlas_decay_freshness_invalid",
    "atlas_adjudication_unavailable",
    "atlas_adjudication_invalid",
    "atlas_adjudication_timestamp_missing",
    "atlas_adjudication_timestamp_invalid",
    "atlas_adjudication_timestamp_naive",
    "atlas_adjudication_stale",
    "atlas_adjudication_future",
    "capture_timestamp_future",
    "component_unknown",
    "projection_invalid",
})


def _normalise_argument(value: Any, name: str) -> _datetime.datetime:
    """Return an aware UTC argument or raise a stable, non-sensitive error."""
    if not isinstance(value, _datetime.datetime):
        raise ValueError(f"{name}_must_be_timezone_aware")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}_must_be_timezone_aware")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise ValueError(f"{name}_must_be_timezone_aware") from None


def _timestamp_text(value: _datetime.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_aware_timestamp(value: Any, prefix: str) -> Tuple[Optional[_datetime.datetime], Optional[str]]:
    if not isinstance(value, str) or not value.strip():
        return None, f"{prefix}_timestamp_missing"
    text = value.strip()
    try:
        parsed = _datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None, f"{prefix}_timestamp_invalid"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, f"{prefix}_timestamp_naive"
    try:
        return parsed.astimezone(UTC), None
    except (OverflowError, ValueError):
        return None, f"{prefix}_timestamp_invalid"


def _parse_date_or_aware(value: Any, prefix: str) -> Tuple[Optional[_datetime.datetime], Optional[str]]:
    """Parse Atlas date-only ``as_of`` at UTC day boundary or aware timestamp."""
    if not isinstance(value, str) or not value.strip():
        return None, f"{prefix}_timestamp_missing"
    text = value.strip()
    if len(text) == 10:
        try:
            day = _datetime.date.fromisoformat(text)
        except (TypeError, ValueError):
            return None, f"{prefix}_timestamp_invalid"
        return _datetime.datetime.combine(day, _datetime.time(), tzinfo=UTC), None
    return _parse_aware_timestamp(text, prefix)


def _safe_count(value: Any) -> bool:
    return (
        type(value) is int
        and 0 <= value <= MAX_SAFE_COUNT
    )


def _bounded_counts(source: Any, fields: Iterable[str]) -> Tuple[Dict[str, int], bool]:
    """Copy only known non-negative integer fields; never coerce booleans."""
    if not isinstance(source, Mapping):
        return {}, True
    result: Dict[str, int] = {}
    invalid = False
    for name in fields:
        if name not in source:
            invalid = True
            continue
        value = source.get(name)
        if _safe_count(value):
            result[name] = value
        else:
            invalid = True
    return result, invalid


def _add_reason(reasons: List[str], reason: Optional[str]) -> None:
    if reason is None or reason not in FIXED_REASON_CODES:
        return
    if reason not in reasons and len(reasons) < MAX_REASONS:
        reasons.append(reason)


def _base_component(
    *,
    availability: str,
    coverage: str,
    freshness: str = UNKNOWN,
    last_verified_at: Optional[str] = None,
    status: str = UNKNOWN,
    reasons: Optional[Iterable[str]] = None,
    counts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    safe_reasons: List[str] = []
    for reason in reasons or ():
        _add_reason(safe_reasons, reason)
    return {
        "availability": availability,
        "coverage": coverage,
        "freshness": freshness if freshness in {FRESH, STALE, FUTURE, UNKNOWN} else UNKNOWN,
        "last_verified_at": last_verified_at,
        "status": status if status in {HEALTHY, DEGRADED, UNKNOWN} else UNKNOWN,
        "reasons": safe_reasons,
        "counts": dict(counts or {}),
    }


def _classify_age(
    value: Optional[_datetime.datetime],
    observed_at: _datetime.datetime,
    *,
    stale_after: int,
    stale_reason: str,
    future_reason: str,
    future_is_stale: bool = False,
    future_grace: int = 0,
) -> Tuple[str, List[str]]:
    if value is None:
        return UNKNOWN, []
    age = (observed_at - value).total_seconds()
    if age > stale_after:
        return STALE, [stale_reason]
    if age < -future_grace:
        return (STALE if future_is_stale else FUTURE), [future_reason]
    return FRESH, []


def _build_imprint(
    source: Any,
    observed_at: _datetime.datetime,
    captured_at: _datetime.datetime,
) -> Dict[str, Any]:
    if source is None:
        return _base_component(
            availability=UNAVAILABLE,
            coverage=UNKNOWN,
            reasons=["imprint_source_unavailable"],
        )
    if not isinstance(source, Mapping):
        return _base_component(
            availability=INVALID,
            coverage=UNKNOWN,
            reasons=["imprint_source_invalid"],
        )

    reasons: List[str] = []
    structural_invalid = False
    if source.get("health_schema_version") != "1.0.0":
        structural_invalid = True
        _add_reason(reasons, "imprint_schema_invalid")

    source_status = source.get("status")
    if not isinstance(source_status, str) or source_status not in {HEALTHY, DEGRADED}:
        structural_invalid = True
        _add_reason(reasons, "imprint_status_invalid")

    source_reasons = source.get("degraded_reasons")
    if not isinstance(source_reasons, (list, tuple)):
        structural_invalid = True
        _add_reason(reasons, "imprint_reasons_invalid")
        source_reasons = []

    known_reasons: List[str] = []
    unknown_reason = False
    for reason in source_reasons:
        if isinstance(reason, str) and reason in IMPRINT_REASON_CODES:
            if reason not in known_reasons:
                known_reasons.append(reason)
        else:
            unknown_reason = True
    for reason in sorted(known_reasons):
        _add_reason(reasons, reason)
    if unknown_reason:
        _add_reason(reasons, "imprint_reason_unknown")

    metrics = source.get("metrics")
    if not isinstance(metrics, Mapping):
        structural_invalid = True
        _add_reason(reasons, "imprint_source_invalid")
        metrics = {}
    counts, metric_invalid = _bounded_counts(metrics, IMPRINT_COUNT_FIELDS)
    if metric_invalid:
        _add_reason(reasons, "imprint_metric_invalid")

    freshness, age_reasons = _classify_age(
        captured_at,
        observed_at,
        stale_after=IMPRINT_CAPTURE_MAX_AGE_SECONDS,
        stale_reason="imprint_capture_stale",
        future_reason="imprint_capture_future",
        future_is_stale=False,
        future_grace=300,
    )
    for reason in age_reasons:
        _add_reason(reasons, reason)

    if source_status == DEGRADED:
        _add_reason(reasons, "imprint_health_degraded")
    elif source_status == HEALTHY and (known_reasons or unknown_reason):
        _add_reason(reasons, "imprint_health_inconsistent")

    if structural_invalid:
        availability = INVALID
        coverage = UNKNOWN
        status = UNKNOWN
        verified = None
    else:
        availability = PARTIAL if metric_invalid or unknown_reason else AVAILABLE
        coverage = "partial" if availability == PARTIAL else "complete"
        degraded = bool(
            source_status == DEGRADED
            or known_reasons
            or unknown_reason
            or metric_invalid
            or freshness != FRESH
        )
        status = DEGRADED if degraded else HEALTHY
        verified = _timestamp_text(captured_at)
    return _base_component(
        availability=availability,
        coverage=coverage,
        freshness=freshness,
        last_verified_at=verified,
        status=status,
        reasons=reasons,
        counts=counts,
    )


def _build_taste(
    source: Any,
    observed_at: _datetime.datetime,
) -> Dict[str, Any]:
    if source is None:
        return _base_component(
            availability=UNAVAILABLE,
            coverage=UNKNOWN,
            reasons=["taste_source_unavailable"],
        )
    if not isinstance(source, Mapping):
        return _base_component(
            availability=INVALID,
            coverage=UNKNOWN,
            reasons=["taste_source_invalid"],
        )

    reasons: List[str] = []
    structural_invalid = False
    if source.get("schema") != "taste-distiller-health/v1":
        structural_invalid = True
        _add_reason(reasons, "taste_schema_invalid")

    source_status = source.get("status")
    if not isinstance(source_status, str) or source_status not in {HEALTHY, DEGRADED}:
        structural_invalid = True
        _add_reason(reasons, "taste_status_invalid")

    timestamp, timestamp_reason = _parse_aware_timestamp(source.get("generated_at"), "taste")
    _add_reason(reasons, timestamp_reason)
    if timestamp_reason is not None:
        structural_invalid = True

    counts, count_invalid = _bounded_counts(source, TASTE_COUNT_FIELDS)
    if count_invalid:
        _add_reason(reasons, "taste_count_invalid")

    relation_invalid = False
    if not count_invalid:
        if counts["retry_exhausted"] > counts["errors"]:
            relation_invalid = True
        if counts["batch_errors"] > counts["batch_count"]:
            relation_invalid = True
    if relation_invalid:
        _add_reason(reasons, "taste_count_relation_invalid")

    freshness, age_reasons = _classify_age(
        timestamp,
        observed_at,
        stale_after=TASTE_MAX_AGE_SECONDS,
        stale_reason="taste_stale",
        future_reason="taste_future",
        future_is_stale=True,
        future_grace=TASTE_FUTURE_GRACE_SECONDS,
    )
    for reason in age_reasons:
        _add_reason(reasons, reason)

    has_errors = bool(
        not count_invalid
        and any(counts[name] for name in (
            "errors", "retry_exhausted", "transcript_missing",
            "queue_bad_records", "batch_errors",
        ))
    )
    if source_status == DEGRADED:
        _add_reason(reasons, "taste_health_degraded")
    if has_errors:
        _add_reason(reasons, "taste_errors_present")
    if source_status == HEALTHY and has_errors:
        _add_reason(reasons, "taste_health_inconsistent")

    if structural_invalid:
        availability = INVALID
        coverage = UNKNOWN
        status = UNKNOWN
    elif count_invalid or relation_invalid:
        availability = PARTIAL
        coverage = "partial"
        status = UNKNOWN
    else:
        availability = AVAILABLE
        coverage = "complete"
        status = DEGRADED if source_status == DEGRADED or has_errors or freshness != FRESH else HEALTHY
    return _base_component(
        availability=availability,
        coverage=coverage,
        freshness=freshness,
        last_verified_at=_timestamp_text(timestamp) if timestamp is not None else None,
        status=status,
        reasons=reasons,
        counts=counts,
    )


def _decay_projection(
    decay: Any,
    observed_at: _datetime.datetime,
) -> Tuple[str, str, str, List[str], Dict[str, Any], Optional[_datetime.datetime]]:
    """Return decay availability/coverage/freshness/reasons/counts/as-of."""
    reasons: List[str] = []
    if not isinstance(decay, Mapping):
        return UNAVAILABLE, UNKNOWN, UNKNOWN, ["atlas_decay_unavailable"], {}, None
    if decay.get("present") is not True:
        return UNAVAILABLE, UNKNOWN, UNKNOWN, ["atlas_decay_unavailable"], {}, None

    invalid = False
    if decay.get("cadence") != "weekly (Monday)":
        invalid = True
        _add_reason(reasons, "atlas_decay_cadence_invalid")
    threshold = decay.get("freshness_threshold_seconds")
    if type(threshold) is not int or threshold != DECAY_MAX_AGE_SECONDS:
        invalid = True
        _add_reason(reasons, "atlas_decay_invalid")
    as_of, time_reason = _parse_date_or_aware(decay.get("as_of"), "atlas_decay")
    _add_reason(reasons, time_reason)
    if time_reason is not None:
        invalid = True
    state_counts = decay.get("state_counts")
    if not isinstance(state_counts, Mapping):
        state_counts = decay.get("counts")
    counts, count_invalid = _bounded_counts(state_counts, DECAY_COUNT_FIELDS)
    if count_invalid:
        invalid = True
        _add_reason(reasons, "atlas_decay_invalid")

    source_freshness = decay.get("freshness")
    if not isinstance(source_freshness, str) or source_freshness not in {FRESH, STALE, UNKNOWN}:
        invalid = True
        _add_reason(reasons, "atlas_decay_freshness_invalid")

    if invalid:
        return INVALID, UNKNOWN, UNKNOWN, reasons, {}, as_of

    freshness, age_reasons = _classify_age(
        as_of,
        observed_at,
        stale_after=DECAY_MAX_AGE_SECONDS,
        stale_reason="atlas_decay_stale",
        future_reason="atlas_decay_future",
        future_is_stale=False,
    )
    for reason in age_reasons:
        _add_reason(reasons, reason)
    if source_freshness == STALE:
        freshness = STALE
        _add_reason(reasons, "atlas_decay_stale")
    elif source_freshness == UNKNOWN:
        freshness = UNKNOWN
        _add_reason(reasons, "atlas_decay_invalid")
    return AVAILABLE, "complete", freshness, reasons, {"decay": counts}, as_of


def _adjudication_projection(
    queue: Any,
    observed_at: _datetime.datetime,
) -> Tuple[str, str, str, List[str], Dict[str, Any], Optional[_datetime.datetime]]:
    """Project queue counts, date ages, and the oldest valid source time."""
    reasons: List[str] = []
    if not isinstance(queue, Mapping) or not isinstance(queue.get("items"), list):
        return UNAVAILABLE, UNKNOWN, UNKNOWN, ["atlas_adjudication_unavailable"], {}, None

    items = queue.get("items")
    valid = 0
    invalid = 0
    freshnesses: List[str] = []
    source_times: List[_datetime.datetime] = []
    finding_total = 0
    actioned_total = 0
    structured_findings = True
    structured_actioned = True
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("kind") != "adjudication" or item.get("source") != "adjudication:atlas-v2":
            continue
        as_of, time_reason = _parse_date_or_aware(item.get("as_of"), "atlas_adjudication")
        if time_reason is not None or as_of is None:
            invalid += 1
            _add_reason(reasons, time_reason or "atlas_adjudication_invalid")
            continue
        valid += 1
        source_times.append(as_of)
        freshness, age_reasons = _classify_age(
            as_of,
            observed_at,
            stale_after=ADJUDICATION_MAX_AGE_SECONDS,
            stale_reason="atlas_adjudication_stale",
            future_reason="atlas_adjudication_future",
            future_is_stale=False,
        )
        freshnesses.append(freshness)
        for reason in age_reasons:
            _add_reason(reasons, reason)
        findings = item.get("findings")
        actioned = item.get("actioned")
        if _safe_count(findings):
            finding_total += findings
        else:
            structured_findings = False
        if _safe_count(actioned):
            actioned_total += actioned
        else:
            structured_actioned = False

    if valid == 0:
        _add_reason(reasons, "atlas_adjudication_invalid" if invalid else "atlas_adjudication_unavailable")
        counts = {"invalid_items": invalid} if invalid else {}
        return (INVALID if invalid else UNAVAILABLE), UNKNOWN, UNKNOWN, reasons, counts, None

    if invalid:
        _add_reason(reasons, "atlas_adjudication_invalid")
    freshness = FRESH
    if STALE in freshnesses:
        freshness = STALE
    elif FUTURE in freshnesses:
        freshness = FUTURE
    elif UNKNOWN in freshnesses:
        freshness = UNKNOWN
    counts: Dict[str, Any] = {"queue_items": valid}
    if invalid:
        counts["invalid_items"] = invalid
    if structured_findings:
        counts["finding_count"] = finding_total
    if structured_actioned:
        counts["actioned_count"] = actioned_total
    return (
        PARTIAL if invalid else AVAILABLE,
        "partial" if invalid else "complete",
        freshness,
        reasons,
        {"adjudication": counts},
        min(source_times),
    )


def _build_atlas(
    source: Any,
    observed_at: _datetime.datetime,
) -> Dict[str, Any]:
    if source is None:
        return _base_component(
            availability=UNAVAILABLE,
            coverage=UNKNOWN,
            reasons=["atlas_source_unavailable"],
        )
    if not isinstance(source, Mapping):
        return _base_component(
            availability=INVALID,
            coverage=UNKNOWN,
            reasons=["atlas_source_invalid"],
        )

    reasons: List[str] = []
    if source.get("schema") != "boot-pack/v1":
        _add_reason(reasons, "atlas_schema_invalid")
        return _base_component(
            availability=INVALID,
            coverage=UNKNOWN,
            reasons=reasons,
        )
    sections = source.get("sections")
    if not isinstance(sections, Mapping):
        return _base_component(
            availability=INVALID,
            coverage=UNKNOWN,
            reasons=["atlas_sections_invalid"],
        )

    pack_timestamp, pack_reason = _parse_aware_timestamp(source.get("generated_at"), "atlas_pack")
    if pack_reason is not None:
        _add_reason(reasons, pack_reason)
    pack_freshness, pack_age_reasons = _classify_age(
        pack_timestamp,
        observed_at,
        stale_after=BOOT_PACK_MAX_AGE_SECONDS,
        stale_reason="atlas_pack_stale",
        future_reason="atlas_pack_future",
        future_is_stale=False,
    )
    for reason in pack_age_reasons:
        _add_reason(reasons, reason)

    decay_availability, decay_coverage, decay_freshness, decay_reasons, decay_counts, _decay_as_of = _decay_projection(
        sections.get("decay"), observed_at,
    )
    for reason in decay_reasons:
        _add_reason(reasons, reason)

    (
        adjudication_availability,
        adjudication_coverage,
        adjudication_freshness,
        adjudication_reasons,
        adjudication_counts,
        adjudication_oldest,
    ) = _adjudication_projection(
        sections.get("decision_queue"), observed_at,
    )
    for reason in adjudication_reasons:
        _add_reason(reasons, reason)

    feed_availability = (decay_availability, adjudication_availability)
    valid_feeds = sum(item == AVAILABLE for item in feed_availability)
    invalid_feeds = sum(item == INVALID for item in feed_availability)
    partial_feeds = sum(item == PARTIAL for item in feed_availability)
    if pack_reason is not None:
        availability = INVALID
        coverage = UNKNOWN
    elif valid_feeds == 2:
        availability = AVAILABLE
        coverage = "complete"
    elif valid_feeds == 1 or partial_feeds:
        availability = PARTIAL
        coverage = "partial"
    elif invalid_feeds == 2:
        availability = INVALID
        coverage = UNKNOWN
    elif invalid_feeds:
        availability = PARTIAL
        coverage = "partial"
    else:
        availability = UNAVAILABLE
        coverage = UNKNOWN

    freshnesses = [pack_freshness, decay_freshness, adjudication_freshness]
    if STALE in freshnesses:
        freshness = STALE
    elif FUTURE in freshnesses:
        freshness = FUTURE
    elif UNKNOWN in freshnesses:
        freshness = UNKNOWN
    else:
        freshness = FRESH

    counts: Dict[str, Any] = {}
    counts.update(decay_counts)
    counts.update(adjudication_counts)
    verified_times = []
    if pack_timestamp is not None:
        verified_times.append(pack_timestamp)
    if decay_availability == AVAILABLE and _decay_as_of is not None:
        verified_times.append(_decay_as_of)
    if adjudication_availability in {AVAILABLE, PARTIAL} and adjudication_oldest is not None:
        verified_times.append(adjudication_oldest)
    last_verified = min(verified_times) if verified_times else None
    if availability in {INVALID, UNAVAILABLE}:
        status = UNKNOWN
    else:
        status = HEALTHY if (
            availability == AVAILABLE
            and freshness == FRESH
            and decay_freshness == FRESH
            and adjudication_freshness == FRESH
        ) else DEGRADED
    if pack_reason is not None:
        status = UNKNOWN
    return _base_component(
        availability=availability,
        coverage=coverage,
        freshness=freshness,
        last_verified_at=_timestamp_text(last_verified) if last_verified is not None else None,
        status=status,
        reasons=reasons,
        counts=counts,
    )


def _overall_status(components: Mapping[str, Mapping[str, Any]]) -> str:
    statuses = [component.get("status") for component in components.values()]
    if all(status == HEALTHY for status in statuses):
        return HEALTHY
    if any(status == DEGRADED for status in statuses):
        return DEGRADED
    return UNKNOWN


def build_health(
    imprint_health: Optional[dict],
    taste_health: Optional[dict],
    boot_pack: Optional[dict],
    observed_at: _datetime.datetime,
    captured_at: _datetime.datetime,
) -> Dict[str, Any]:
    """Return the closed ``context-health/v1`` content-free projection."""
    observed = _normalise_argument(observed_at, "observed_at")
    captured = _normalise_argument(captured_at, "captured_at")
    components = {
        "imprint": _build_imprint(imprint_health, observed, captured),
        "taste": _build_taste(taste_health, observed),
        "atlas": _build_atlas(boot_pack, observed),
    }
    reasons: List[str] = []
    for component in components.values():
        for reason in component["reasons"]:
            _add_reason(reasons, reason)
    if (captured - observed).total_seconds() > 300:
        _add_reason(reasons, "capture_timestamp_future")
    if any(component["status"] == UNKNOWN for component in components.values()):
        _add_reason(reasons, "component_unknown")
    return {
        "schema": CONTEXT_HEALTH_SCHEMA,
        "observed_at": _timestamp_text(observed),
        "captured_at": _timestamp_text(captured),
        "status": _overall_status(components),
        "reasons": reasons,
        "components": components,
    }


def _safe_report_timestamp(value: Any) -> str:
    parsed, _reason = _parse_aware_timestamp(value, "render")
    return _timestamp_text(parsed) if parsed is not None else "unknown"


def _safe_reason_values(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [reason for reason in value if isinstance(reason, str) and reason in FIXED_REASON_CODES]


def _render_counts(value: Any) -> List[str]:
    if not isinstance(value, Mapping):
        return []
    lines: List[str] = []
    for name in sorted(name for name in value if isinstance(name, str)):
        if name not in {
            *IMPRINT_COUNT_FIELDS,
            *TASTE_COUNT_FIELDS,
            *DECAY_COUNT_FIELDS,
            "decay",
            "adjudication",
        }:
            continue
        child = value[name]
        if isinstance(child, Mapping):
            for child_name in sorted(child_name for child_name in child if isinstance(child_name, str)):
                if child_name not in {
                    *DECAY_COUNT_FIELDS,
                    "queue_items", "invalid_items", "finding_count", "actioned_count",
                } or not _safe_count(child[child_name]):
                    continue
                lines.append(f"  - {name}.{child_name}: {child[child_name]}")
        elif _safe_count(child):
            lines.append(f"  - {name}: {child}")
    return lines


def render_health(report: Mapping[str, Any]) -> str:
    """Render only fixed, already-sanitized health fields as Markdown."""
    lines = ["# Context health", "", "- schema: context-health/v1"]
    if not isinstance(report, Mapping) or report.get("schema") != CONTEXT_HEALTH_SCHEMA:
        lines.extend(["- status: unknown", "- reason: projection_invalid", ""])
        return "\n".join(lines)
    report_status = report.get("status")
    status = report_status if isinstance(report_status, str) and report_status in {HEALTHY, DEGRADED, UNKNOWN} else UNKNOWN
    lines.extend([
        f"- status: {status}",
        f"- observed_at: {_safe_report_timestamp(report.get('observed_at'))}",
        f"- captured_at: {_safe_report_timestamp(report.get('captured_at'))}",
    ])
    reasons = _safe_reason_values(report.get("reasons"))
    lines.append("- reasons: " + (", ".join(dict.fromkeys(reasons)) if reasons else "none"))
    lines.append("")
    components = report.get("components")
    if not isinstance(components, Mapping):
        lines.append("- components: unknown")
        return "\n".join(lines) + "\n"
    for name in COMPONENT_NAMES:
        component = components.get(name)
        lines.append(f"## {name}")
        if not isinstance(component, Mapping):
            lines.append("- status: unknown")
            lines.append("- reason: component_unknown")
            lines.append("")
            continue
        raw_status = component.get("status")
        c_status = raw_status if isinstance(raw_status, str) and raw_status in {HEALTHY, DEGRADED, UNKNOWN} else UNKNOWN
        raw_availability = component.get("availability")
        availability = raw_availability if isinstance(raw_availability, str) and raw_availability in {AVAILABLE, PARTIAL, UNAVAILABLE, INVALID} else UNKNOWN
        raw_coverage = component.get("coverage")
        coverage = raw_coverage if isinstance(raw_coverage, str) and raw_coverage in {"complete", "partial", UNKNOWN} else UNKNOWN
        raw_freshness = component.get("freshness")
        freshness = raw_freshness if isinstance(raw_freshness, str) and raw_freshness in {FRESH, STALE, FUTURE, UNKNOWN} else UNKNOWN
        lines.extend([
            f"- status: {c_status}",
            f"- availability: {availability}",
            f"- coverage: {coverage}",
            f"- freshness: {freshness}",
            f"- last_verified_at: {_safe_report_timestamp(component.get('last_verified_at')) if component.get('last_verified_at') is not None else 'unknown'}",
        ])
        component_reasons = _safe_reason_values(component.get("reasons"))
        lines.append("- reasons: " + (", ".join(dict.fromkeys(component_reasons)) if component_reasons else "none"))
        count_lines = _render_counts(component.get("counts"))
        lines.append("- counts:")
        lines.extend(count_lines or ["  - none"])
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "ADJUDICATION_MAX_AGE_SECONDS",
    "BOOT_PACK_MAX_AGE_SECONDS",
    "CONTEXT_HEALTH_SCHEMA",
    "DECAY_MAX_AGE_SECONDS",
    "IMPRINT_CAPTURE_MAX_AGE_SECONDS",
    "TASTE_FUTURE_GRACE_SECONDS",
    "TASTE_MAX_AGE_SECONDS",
    "build_health",
    "render_health",
]


def main(argv=None):
    import argparse
    import json
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from work_view import load_input
    parser = argparse.ArgumentParser(description="Project captured context health without reading private stores")
    parser.add_argument("--imprint-health")
    parser.add_argument("--taste-health")
    parser.add_argument("--boot-pack")
    parser.add_argument("--captured-at", required=True,
                        help="actual Imprint health invocation capture time; never a file re-read time")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)
    captured, issue = _parse_aware_timestamp(args.captured_at, "capture")
    if issue:
        parser.error("captured-at must be timezone-aware")
    def source(path):
        data, problem, _digest = load_input(path)
        # Missing/unconfigured observations are unavailable; malformed or
        # unreadable configured snapshots are invalid evidence.
        return data if problem in (None, "unconfigured", "missing") else object()
    imprint = source(args.imprint_health)
    taste = source(args.taste_health)
    boot = source(args.boot_pack)
    report = build_health(imprint, taste, boot, _datetime.datetime.now(UTC), captured)
    print(json.dumps(report, indent=2) if args.format == "json" else render_health(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
