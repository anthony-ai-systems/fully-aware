#!/usr/bin/env python3
"""Daily intelligence pass mechanics: identity, allocation, suppression, handoff.

Code owns the pass identity, allocation accounting, missed-day and recovery rules,
perspective rotation, the closed candidate shape, a novelty floor, suppression
against earlier docket events, receipts and the docket handoff file. The model owns
the questions, the research and the judgment.

Nothing here calls a model or the network. Nothing here writes the IRIS docket: an
accepted candidate becomes an outbox file holding the docket's ``opportunity_proposal``
propose input, and the docket owner decides whether to consume it. The docket is read
as raw JSON without importing IRIS code.

The allocation values in ``POLICY`` are PROPOSALS awaiting Anthony, not rulings.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from zoneinfo import ZoneInfo

SCHEMA = "fa-intelligence-pass/v1"
PLAN_SCHEMA = "fa-intelligence-plan/v1"
DRAFT_SCHEMA = "fa-intelligence-draft/v1"
ACTION_CLASS = "opportunity_proposal"

# PROPOSED values awaiting Anthony's approval -- not rulings, not measured optima.
POLICY = {
    "wall_minutes": 25,
    "model_launches": 3,
    "source_opens": 12,
    "deep_candidates": 2,
    "present_max": 1,
    "local_window": "06:15-11:00",
    "timezone": "America/Los_Angeles",
    "novelty_threshold": 0.45,
    "recovery_lookback_days": 7,
    "max_next_check_days": 30,
}
BUDGET_KEYS = ("wall_minutes", "model_launches", "source_opens", "deep_candidates", "present_max", "local_window")

PERSPECTIVES = ("client_results", "creative_quality", "delivery_economics", "workflows",
                "software_architecture", "delegation", "learning", "personal_routines",
                "cross_domain_transfer")
# An external claim can never become a fact about Anthony or a ratified preference.
FORBIDDEN = ("ratified", "preference", "imprint", "atlas_write", "fact_about_anthony")
REQUIRED = ("dedupe_key", "question", "perspective", "goal_link", "novelty", "internal_evidence",
            "counterevidence", "challenge", "generated_by", "proof", "recommendation", "effort",
            "confidence", "recheck_after")
OPTIONAL = ("external_evidence", "external_gap", "experiment")
PROOF_KINDS = {"analysis", "example", "draft", "benchmark", "prototype"}
EXPERIMENT_KEYS = ("baseline", "mechanism", "success_measure", "ceiling", "stop_rule", "rollback")
VERDICTS = {"survives", "weakened", "refuted"}
COVERAGE = {"complete", "partial", "unavailable"}
MODES = {"scheduled", "recovery", "skip"}
OUTCOMES = {"opportunities_prepared", "no_qualifying_opportunity", "coverage_gap", "preempted", "failed"}
STATUSES = {"presented_to_outbox", "held", "suppressed", "rejected"}
CORPUS_KINDS = {"backlog", "prompt", "bookmark", "radar", "docket"}
RECOVERABLE = {"preempted", "failed"}
RECEIPT_KEYS = {"schema", "date", "mode", "covers_date", "started_at", "finished_at", "allocation", "used",
                "perspective", "questions", "coverage", "candidates", "outcome", "preempted_by", "recover_by"}
OPTIONS = (
    ("proceed", "Proceed as prepared",
     "Uses the prepared proof as the starting point; costs the effort estimate and nothing else is authorised."),
    ("modify", "Proceed with changes",
     "Keeps the idea but changes scope or method first; needs one short instruction."),
    ("defer", "Revisit later",
     "Costs nothing now; the idea is rechecked at the next check date."),
    ("decline", "Decline",
     "Stays suppressed unless its evidence materially changes."),
)

# Mirrors of the IRIS docket's own text/identity rules (initiative_docket.py). They are
# re-implemented, not imported: Fully Aware never imports IRIS code.
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z")
SHORT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
OPPORTUNITY = re.compile(r"opportunity-[a-f0-9]{24}\Z")
PRIVATE = re.compile(r"(?:/Users/|/home/|file://|[A-Z]:\\|sk-[A-Za-z0-9]{12}|Bearer\s|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", re.I)
PHONE = re.compile(r"\+?\d[\d ()-]{8,}\d")
DATE_FILE = re.compile(r"\d{4}-\d{2}-\d{2}\.json\Z")
WORD = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset("""a an and are as at be by can could do does for from has have how i if in into is it
its of on or our should so that the their them then there these this to use using was we what when where
which who why will with would you your""".split())
MAX_INPUT_BYTES = 32768
MAX_JSON = 2 * 1024 * 1024


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def merged(policy):
    return dict(POLICY, **(policy or {}))


def instant(value):
    if isinstance(value, dt.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("aware_timestamp_required")
        return value.astimezone(dt.timezone.utc)
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("aware_timestamp_required")
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        raise ValueError("aware_timestamp_required") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("aware_timestamp_required")
    return parsed.astimezone(dt.timezone.utc)


def stamp(value):
    return instant(value).isoformat().replace("+00:00", "Z")


def as_date(value):
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("iso_date_required")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError("iso_date_required") from None


def local_date(now, policy=None):
    return instant(now).astimezone(ZoneInfo(merged(policy)["timezone"])).date()


def window(day, policy=None):
    """Aware UTC (start, end) of the local pass window on ``day``."""
    policy = merged(policy)
    match = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", policy["local_window"])
    if not match:
        raise ValueError("invalid_local_window")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    zone = ZoneInfo(policy["timezone"])
    start = dt.datetime.combine(day, dt.time(h1, m1), zone)
    end = dt.datetime.combine(day, dt.time(h2, m2), zone)
    if end <= start:
        raise ValueError("invalid_local_window")
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def identifier(value, reason="invalid_identifier"):
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None or ".." in value or "//" in value:
        raise ValueError(reason)
    return value


def prose(value):
    """Whitespace-normalised text (newlines are control characters to the docket)."""
    return " ".join(value.split()) if isinstance(value, str) else value


def private(value):
    return (PRIVATE.search(value) is not None
            or any(sum(c.isdigit() for c in m.group()) >= 10 for m in PHONE.finditer(value)))


def text_reason(name, value, limit=1000):
    if not isinstance(value, str):
        return "invalid_text:" + name
    value = prose(value)
    if not value:
        return "empty_text:" + name
    if len(value) > limit:
        return "text_too_long:" + name
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "unsafe_text:" + name
    if private(value):
        return "private_text:" + name
    return None


def clip(value, limit):
    value = prose(value)
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def short_token(ref):
    """A docket-safe short reference token; paths, URLs and long refs become a hash."""
    if (isinstance(ref, str) and SHORT.fullmatch(ref) and ".." not in ref and not private(ref)):
        return ref
    return "ref-" + sha(ref.encode("utf-8") if isinstance(ref, str) else canonical(ref))[:16]


# --------------------------------------------------------------------------- #
# history, allocation, missed days, rotation
# --------------------------------------------------------------------------- #
def index_history(history):
    """{date: receipt} for receipts with a valid date; later entries win."""
    out = {}
    for receipt in history or []:
        if not isinstance(receipt, dict):
            continue
        try:
            out[as_date(receipt.get("date"))] = receipt
        except ValueError:
            continue
    return out


def budget(policy):
    return {key: policy[key] for key in BUDGET_KEYS}


def unrecovered_days(receipts, today, policy):
    """Missed/preempted/failed days since the last recovery pass, oldest first."""
    if not receipts:
        return []
    start = max(today - dt.timedelta(days=policy["recovery_lookback_days"]), min(receipts))
    recoveries = [day for day, r in receipts.items() if r.get("mode") == "recovery" and day < today]
    if recoveries:
        start = max(start, max(recoveries) + dt.timedelta(days=1))
    days, day = [], start
    while day < today:
        receipt = receipts.get(day)
        if receipt is None or receipt.get("outcome") in RECOVERABLE:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def plan_pass(history, now, *, backlog_count, urgent=None, policy=None, ignore_window=False):
    """Decide whether today's single pass runs, and in which mode.

    A pass is due once per local day whatever the backlog size. An urgent incident
    preempts it (recorded, never silent). A gap of missed, preempted or failed days
    gets at most ONE recovery pass, which is that later day's only pass and names the
    date it covers. ``ignore_window`` exists for stub rehearsals only.
    """
    policy = merged(policy)
    now = instant(now)
    if type(backlog_count) is not int or backlog_count < 0:
        raise ValueError("invalid_backlog_count")
    receipts = index_history(history)
    today = local_date(now, policy)
    start, end = window(today, policy)
    tomorrow_start = window(today + dt.timedelta(days=1), policy)[0]
    plan = {"schema": PLAN_SCHEMA, "run": False, "mode": "skip", "reason": None, "reserved": {},
            "preempted_by": None, "recover_by": None, "date": today.isoformat(), "covers_date": None,
            "perspective": None, "backlog_count": backlog_count, "planned_at": stamp(now)}
    prior = receipts.get(today)
    if prior is not None:
        if prior.get("outcome") == "preempted":
            return dict(plan, reason="preempted_today", preempted_by=prior.get("preempted_by"),
                        recover_by=stamp(tomorrow_start))
        return dict(plan, reason="already_ran_today")
    if not ignore_window and now < start:
        return dict(plan, reason="before_window")
    if not ignore_window and now >= end:
        # Today becomes a missed day; the next day's pass recovers it.
        return dict(plan, reason="window_closed", recover_by=stamp(tomorrow_start))
    if urgent is not None:
        identifier(urgent, "invalid_incident_id")
        return dict(plan, reason="preempted_by_incident", preempted_by=urgent,
                    recover_by=stamp(tomorrow_start), covers_date=today.isoformat())
    gap = unrecovered_days(receipts, today, policy)
    plan.update(run=True, reserved=budget(policy), perspective=choose_perspective(today, history))
    if gap:
        covered = receipts.get(gap[-1])
        why = "recovering_preempted_day" if covered and covered.get("outcome") == "preempted" else (
            "recovering_failed_day" if covered else "recovering_missed_day")
        return dict(plan, mode="recovery", reason=why, covers_date=gap[-1].isoformat(),
                    gap_dates=[d.isoformat() for d in gap])
    return dict(plan, mode="scheduled", reason="due_today", covers_date=today.isoformat())


def missed_days(history, now, since, policy=None):
    """Local dates in [since, today) with no receipt at all (a preemption is a receipt)."""
    receipts = index_history(history)
    today = local_date(now, policy)
    day, out = as_date(since), []
    while day < today:
        if day not in receipts:
            out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


def choose_perspective(date, history):
    """Deterministic by date; never one of the previous three receipts' perspectives."""
    day = as_date(date)
    prior = sorted((d, r["perspective"]) for d, r in index_history(history).items()
                   if d < day and r.get("perspective") in PERSPECTIVES)
    recent = {p for _, p in prior[-3:]}
    start = day.toordinal() % len(PERSPECTIVES)
    for step in range(len(PERSPECTIVES)):
        choice = PERSPECTIVES[(start + step) % len(PERSPECTIVES)]
        if choice not in recent:
            return choice
    raise ValueError("no_perspective_available")  # unreachable while len > 3


# --------------------------------------------------------------------------- #
# candidates
# --------------------------------------------------------------------------- #
def forbidden_keys(value, path=""):
    found = []
    if isinstance(value, dict):
        for key, inner in value.items():
            name = str(key).lower()
            if any(word in name for word in FORBIDDEN):
                found.append("forbidden_key:" + (path + "." if path else "") + str(key))
            found.extend(forbidden_keys(inner, (path + "." if path else "") + str(key)))
    elif isinstance(value, list):
        for inner in value:
            found.extend(forbidden_keys(inner, path))
    return found


def _timestamp_ok(value):
    try:
        instant(value)
        return True
    except ValueError:
        return False


def _date_or_timestamp_ok(value):
    try:
        as_date(value)
        return True
    except ValueError:
        return _timestamp_ok(value)


def validate_candidate(c):
    """Reasons the candidate is refused; an empty list means valid."""
    if not isinstance(c, dict):
        return ["candidate_not_object"]
    reasons = forbidden_keys(c)
    reasons += ["unknown_field:" + k for k in sorted(set(c) - set(REQUIRED) - set(OPTIONAL))]
    reasons += ["missing_field:" + k for k in REQUIRED if k not in c]
    if "dedupe_key" in c and not (isinstance(c["dedupe_key"], str) and len(c["dedupe_key"]) <= 80
                                  and SLUG.fullmatch(c["dedupe_key"])):
        reasons.append("invalid_dedupe_key")
    for name in ("question", "goal_link", "novelty", "recommendation"):
        if name in c:
            reasons.append(text_reason(name, c[name]))
    if "effort" in c:
        reasons.append(text_reason("effort", c["effort"], 200))
    if "generated_by" in c:
        reasons.append(text_reason("generated_by", c["generated_by"], 160))
    if "perspective" in c and c["perspective"] not in PERSPECTIVES:
        reasons.append("invalid_perspective")
    ev = c.get("internal_evidence")
    if "internal_evidence" in c:
        if not isinstance(ev, list) or not ev:
            reasons.append("internal_evidence_required")
        else:
            for item in ev:
                if (not isinstance(item, dict) or set(item) != {"ref", "observed_at"}
                        or not isinstance(item["ref"], str) or not item["ref"].strip() or len(item["ref"]) > 300
                        or not _timestamp_ok(item["observed_at"])):
                    reasons.append("invalid_internal_evidence")
                    break
    has_ext, has_gap = "external_evidence" in c, "external_gap" in c
    if has_ext and has_gap:
        reasons.append("external_evidence_and_gap_both_present")
    elif not has_ext and not has_gap:
        reasons.append("external_evidence_or_gap_required")
    elif has_gap:
        reasons.append(text_reason("external_gap", c["external_gap"]))
    else:
        ext = c["external_evidence"]
        if not isinstance(ext, list) or not ext:
            reasons.append("external_evidence_empty")
        else:
            for item in ext:
                if (not isinstance(item, dict)
                        or set(item) != {"source", "published_or_accessed", "claim", "status"}
                        or not isinstance(item["source"], str) or not item["source"].strip()
                        or not _date_or_timestamp_ok(item["published_or_accessed"])
                        or not isinstance(item["claim"], str) or not prose(item["claim"])
                        or item["status"] not in {"verified", "unverified"}):
                    reasons.append("invalid_external_evidence")
                    break
    if "counterevidence" in c:
        counter = c["counterevidence"]
        if not isinstance(counter, list) or not counter:
            reasons.append("counterevidence_required")
        else:
            for item in counter:
                reason = text_reason("counterevidence", item)
                if reason:
                    reasons.append(reason)
                    break
    if "challenge" in c:
        ch = c["challenge"]
        if not isinstance(ch, dict) or set(ch) != {"by", "verdict", "notes"}:
            reasons.append("invalid_challenge")
        else:
            if not isinstance(ch["by"], str) or not ch["by"].strip():
                reasons.append("invalid_challenge")
            elif ch["by"] == c.get("generated_by"):
                reasons.append("challenger_is_generator")
            if ch["verdict"] not in VERDICTS:
                reasons.append("invalid_challenge_verdict")
            elif ch["verdict"] == "refuted":
                reasons.append("challenge_refuted")
            if not isinstance(ch["notes"], str):
                reasons.append("invalid_challenge")
    proof_kind = None
    if "proof" in c:
        proof = c["proof"]
        if (not isinstance(proof, dict) or set(proof) != {"kind", "ref"} or proof["kind"] not in PROOF_KINDS
                or not isinstance(proof["ref"], str) or not proof["ref"].strip() or len(proof["ref"]) > 300):
            reasons.append("invalid_proof")
        else:
            proof_kind = proof["kind"]
    if "experiment" in c:
        exp = c["experiment"]
        if (not isinstance(exp, dict) or set(exp) != set(EXPERIMENT_KEYS)
                or any(not isinstance(exp[k], str) or not prose(exp[k]) for k in EXPERIMENT_KEYS)):
            reasons.append("invalid_experiment")
    elif proof_kind in {"prototype", "benchmark"}:
        reasons.append("experiment_required")
    if "confidence" in c:
        value = c["confidence"]
        if type(value) not in (int, float) or not 0 <= value <= 1:
            reasons.append("invalid_confidence")
    if "recheck_after" in c:
        try:
            as_date(c["recheck_after"])
        except ValueError:
            reasons.append("invalid_recheck_after")
    return [r for r in dict.fromkeys(reasons) if r]


# --------------------------------------------------------------------------- #
# novelty, identity, suppression
# --------------------------------------------------------------------------- #
def tokens(text):
    return frozenset(w for w in WORD.findall(text.lower()) if len(w) > 1 and w not in STOPWORDS)


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def novelty_check(c, corpus, policy=None):
    """Crude, auditable floor: token-set Jaccard over question + recommendation.

    It catches near-duplicates only. It cannot judge material novelty; the model
    still has to say what is materially new, and the challenger has to test it.
    """
    threshold = merged(policy)["novelty_threshold"]
    mine = tokens("%s %s" % (c.get("question", ""), c.get("recommendation", "")))
    scored = []
    for entry in corpus or []:
        if (not isinstance(entry, dict) or entry.get("kind") not in CORPUS_KINDS
                or not isinstance(entry.get("text"), str) or not isinstance(entry.get("id"), str)):
            continue
        scored.append((entry["id"], round(jaccard(mine, tokens(entry["text"])), 4)))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    score = scored[0][1] if scored else 0.0
    return {"novel": score < threshold, "nearest": scored[:3], "score": score, "threshold": threshold,
            "method": "token_set_jaccard_floor"}


def opportunity_id(dedupe_key):
    return "opportunity-" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:24]


def evidence_revision(c):
    """sha256 over the evidence set (refs, sources, counterevidence), order-insensitive.

    Observation timestamps are excluded, so re-reading the same evidence tomorrow is
    not a change of evidence.
    """
    internal = sorted({i["ref"] for i in c.get("internal_evidence") or [] if isinstance(i, dict)
                       and isinstance(i.get("ref"), str)})
    external = sorted({e["source"] for e in c.get("external_evidence") or [] if isinstance(e, dict)
                       and isinstance(e.get("source"), str)})
    counter = sorted({prose(t) for t in c.get("counterevidence") or [] if isinstance(t, str)})
    return sha({"internal_evidence": internal, "external_evidence": external, "counterevidence": counter})


def docket_events(value):
    if isinstance(value, dict):
        value = value.get("events")
    if not isinstance(value, list):
        raise ValueError("invalid_docket_events")
    return value


def suppression(c, docket, now=None):
    """Classify a candidate against raw docket events for the same opportunity id.

    Code enforces "the evidence revision changed"; whether the change is material
    stays with the model and the reviewer.
    """
    now = instant(now or dt.datetime.now(dt.timezone.utc))
    oid, revision = opportunity_id(c["dedupe_key"]), evidence_revision(c)
    resolved, snoozed_until = [], None
    for event in docket_events(docket):
        if not isinstance(event, dict) or not isinstance(event.get("input"), dict):
            continue
        data = event["input"]
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        if source.get("work_item_id") != oid:
            continue
        command, rev = event.get("command"), source.get("work_revision")
        if command == "respond" and data.get("disposition") in {"declined", "answered"}:
            resolved.append((data["disposition"], rev))
        elif command == "follow-through" and data.get("outcome") == "recorded":
            resolved.append(("follow_through_recorded", rev))
        elif command == "snooze":
            try:
                until = instant(data.get("until"))
            except ValueError:
                continue
            if until > now and (snoozed_until is None or until > snoozed_until):
                snoozed_until = until
    base = {"opportunity_id": oid, "evidence_revision": revision,
            "prior": [{"disposition": d, "same_evidence": r == revision} for d, r in resolved]}
    if any(r == revision for _, r in resolved):
        return dict(base, status="suppressed")
    if snoozed_until is not None:
        return dict(base, status="snoozed", until=stamp(snoozed_until))
    if resolved:
        return dict(base, status="eligible_changed_evidence")
    return dict(base, status="eligible")


# --------------------------------------------------------------------------- #
# docket handoff
# --------------------------------------------------------------------------- #
def _safe(value, limit=1000):
    if not (isinstance(value, str) and 0 < len(value.strip()) <= limit and value == value.strip()):
        raise ValueError("invalid_text")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("unsafe_text")
    if private(value):
        raise ValueError("private_text_refused")


def _reference(value):
    if not isinstance(value, dict) or set(value) != {"kind", "id"} or value["kind"] != "artifact":
        raise ValueError("invalid_reference")
    identifier(value["id"], "invalid_reference")
    if value["id"].startswith("/"):
        raise ValueError("invalid_reference")


def check_propose(p, now):
    """The docket's propose rules as FA understands them; raises ValueError(reason)."""
    now = instant(now)
    if not isinstance(p, dict) or set(p) != {"event_id", "action_class", "source", "packet", "next_check_at", "origin"}:
        raise ValueError("invalid_fields")
    identifier(p["event_id"], "invalid_event_id")
    if p["action_class"] != ACTION_CLASS:
        raise ValueError("invalid_action_class")
    src = p["source"]
    if not isinstance(src, dict) or set(src) != {"work_item_id", "work_revision", "verified_at", "reference", "terminal"}:
        raise ValueError("invalid_source")
    if not isinstance(src["work_item_id"], str) or not OPPORTUNITY.fullmatch(src["work_item_id"]):
        raise ValueError("opportunity_identity_mismatch")
    if not isinstance(src["work_revision"], str) or not SHA.fullmatch(src["work_revision"]):
        raise ValueError("invalid_work_revision")
    if src["terminal"] is not False:
        raise ValueError("invalid_terminal")
    instant(src["verified_at"])
    _reference(src["reference"])
    packet = p["packet"]
    fields = {"why_now", "recommendation", "question", "options", "authority_needed", "scope",
              "next_owner", "follow_through", "evidence_refs"}
    if not isinstance(packet, dict) or set(packet) != fields:
        raise ValueError("invalid_packet")
    for name in fields - {"options", "evidence_refs"}:
        _safe(packet[name])
    if not packet["why_now"].startswith("Independent discovery:"):
        raise ValueError("why_now_not_independent_discovery")
    options = packet["options"]
    if not isinstance(options, list) or [o.get("id") for o in options if isinstance(o, dict)] != [o[0] for o in OPTIONS]:
        raise ValueError("invalid_options")
    for option in options:
        if set(option) != {"id", "label", "tradeoff"}:
            raise ValueError("invalid_options")
        _safe(option["label"], 200)
        _safe(option["tradeoff"], 500)
    refs = packet["evidence_refs"]
    if not isinstance(refs, list) or len(refs) > 5:
        raise ValueError("invalid_evidence_refs")
    for ref in refs:
        _reference(ref)
    delta = instant(p["next_check_at"]) - now
    if not dt.timedelta(0) < delta <= dt.timedelta(days=POLICY["max_next_check_days"]):
        raise ValueError("invalid_next_check")
    origin = p["origin"]
    if not isinstance(origin, dict) or set(origin) != {"kind", "question", "goal_link", "novelty",
                                                        "counterevidence", "proof_ref", "discovered_at"}:
        raise ValueError("invalid_origin")
    if origin["kind"] != "independent_discovery":
        raise ValueError("invalid_origin_kind")
    for name in ("question", "goal_link", "novelty", "counterevidence"):
        _safe(origin[name])
    _reference(origin["proof_ref"])
    if not dt.timedelta(0) <= now - instant(origin["discovered_at"]) <= dt.timedelta(days=30):
        raise ValueError("invalid_discovered_at")
    if len(canonical(p)) > MAX_INPUT_BYTES:
        raise ValueError("input_too_large")
    return p


def next_check(c, now, policy=None):
    policy = merged(policy)
    now = instant(now)
    ceiling = now + dt.timedelta(days=policy["max_next_check_days"])
    zone = ZoneInfo(policy["timezone"])
    recheck = dt.datetime.combine(as_date(c["recheck_after"]), dt.time(9), zone).astimezone(dt.timezone.utc)
    if recheck <= now:
        recheck = now + dt.timedelta(days=1)
    return min(recheck, ceiling)


def to_docket_propose(c, *, now, event_id, verified_at, policy=None):
    """The exact propose input for the docket's ``opportunity_proposal`` class.

    This is a handoff file for the docket owner; Fully Aware never writes the docket.
    ``verified_at`` must be re-stamped by the consumer if the file is older than the
    docket's source-recheck window.
    """
    reasons = validate_candidate(c)
    if reasons:
        raise ValueError("invalid_candidate:" + reasons[0])
    now = instant(now)
    oid, revision = opportunity_id(c["dedupe_key"]), evidence_revision(c)
    proof_ref = {"kind": "artifact", "id": short_token(c["proof"]["ref"])}
    refs = []
    for ref in [i["ref"] for i in c["internal_evidence"]] + [e["source"] for e in c.get("external_evidence", [])]:
        token = short_token(ref)
        if token not in refs:
            refs.append(token)
    check_at = next_check(c, now, policy)
    counter = clip("; ".join(prose(t) for t in c["counterevidence"]), 1000)
    verdict = c["challenge"]["verdict"]
    proposal = {
        "event_id": event_id,
        "action_class": ACTION_CLASS,
        "source": {"work_item_id": oid, "work_revision": revision, "verified_at": stamp(verified_at),
                   "reference": proof_ref, "terminal": False},
        "packet": {
            "why_now": clip("Independent discovery: " + prose(c["novelty"]), 1000),
            "recommendation": prose(c["recommendation"]),
            "question": "Should this independently discovered opportunity proceed as prepared, "
                        "proceed with changes, wait, or be declined?",
            "options": [{"id": i, "label": label, "tradeoff": tradeoff} for i, label, tradeoff in OPTIONS],
            "authority_needed": "Your choice only. This proposal grants no authority and commits no work.",
            "scope": clip("Perspective %s; prepared proof: %s; effort: %s; confidence %.2f; independent "
                          "challenge verdict: %s." % (c["perspective"], c["proof"]["kind"], prose(c["effort"]),
                                                      float(c["confidence"]), verdict), 1000),
            "next_owner": "Fully Aware intelligence pass prepares; Anthony decides.",
            "follow_through": "Rechecked on %s. A decline stays suppressed unless the evidence changes."
                              % check_at.date().isoformat(),
            "evidence_refs": [{"kind": "artifact", "id": token} for token in refs[:5]],
        },
        "next_check_at": stamp(check_at),
        "origin": {"kind": "independent_discovery", "question": prose(c["question"]),
                   "goal_link": prose(c["goal_link"]), "novelty": prose(c["novelty"]),
                   "counterevidence": counter, "proof_ref": proof_ref, "discovered_at": stamp(now)},
    }
    return check_propose(proposal, now)


# --------------------------------------------------------------------------- #
# receipts
# --------------------------------------------------------------------------- #
def finalize_receipt(receipt, policy=None):
    """Validate a pass receipt against its closed shape and accounting rules."""
    policy = merged(policy)
    if not isinstance(receipt, dict):
        raise ValueError("receipt_not_object")
    r = dict(receipt)
    r.setdefault("schema", SCHEMA)
    if set(r) != RECEIPT_KEYS or r["schema"] != SCHEMA:
        raise ValueError("invalid_receipt_fields")
    day, covers = as_date(r["date"]), as_date(r["covers_date"])
    if r["mode"] not in MODES or r["outcome"] not in OUTCOMES:
        raise ValueError("invalid_mode_or_outcome")
    if r["mode"] == "scheduled" and covers != day:
        raise ValueError("scheduled_pass_covers_other_day")
    if r["mode"] == "recovery" and covers >= day:
        raise ValueError("recovery_must_cover_earlier_day")
    if (r["mode"] == "skip") != (r["outcome"] == "preempted"):
        raise ValueError("skip_mode_is_preemption_only")
    if r["outcome"] == "preempted":
        identifier(r["preempted_by"], "invalid_incident_id")
        instant(r["recover_by"])
    elif r["preempted_by"] is not None or r["recover_by"] is not None:
        raise ValueError("preemption_fields_without_preemption")
    if instant(r["finished_at"]) < instant(r["started_at"]):
        raise ValueError("receipt_time_order")
    if not isinstance(r["allocation"], dict):
        raise ValueError("invalid_allocation")
    used = r["used"]
    if (not isinstance(used, dict) or set(used) != {"wall_seconds", "model_launches", "source_opens"}
            or any(type(used[k]) not in (int, float) or used[k] < 0 for k in used)):
        raise ValueError("invalid_used")
    if r["perspective"] not in PERSPECTIVES and not (r["perspective"] is None and r["outcome"] == "preempted"):
        raise ValueError("invalid_perspective")
    if (not isinstance(r["questions"], list) or len(r["questions"]) > 5
            or any(not isinstance(q, str) for q in r["questions"])):
        raise ValueError("invalid_questions")
    cov = r["coverage"]
    if (not isinstance(cov, dict) or set(cov) != {"internal", "external", "gaps"}
            or cov["internal"] not in COVERAGE or cov["external"] not in COVERAGE
            or not isinstance(cov["gaps"], list) or any(not isinstance(g, str) for g in cov["gaps"])):
        raise ValueError("invalid_coverage")
    presented = 0
    for row in r["candidates"] if isinstance(r["candidates"], list) else [None]:
        if (not isinstance(row, dict) or set(row) != {"dedupe_key", "opportunity_id", "status", "reasons"}
                or row["status"] not in STATUSES or not isinstance(row["reasons"], list)):
            raise ValueError("invalid_candidate_row")
        if row["status"] != "rejected" and (not isinstance(row["dedupe_key"], str)
                                            or row["opportunity_id"] != opportunity_id(row["dedupe_key"])):
            raise ValueError("candidate_identity_mismatch")
        presented += row["status"] == "presented_to_outbox"
    cap = r["allocation"].get("present_max", policy["present_max"])
    if presented > cap:
        raise ValueError("present_max_exceeded")
    if (r["outcome"] == "opportunities_prepared") != (presented > 0):
        raise ValueError("outcome_does_not_match_candidates")
    if r["outcome"] == "no_qualifying_opportunity" and (cov["internal"] != "complete" or cov["external"] != "complete"):
        # Incomplete coverage cannot support "nothing worth doing"; say what was not seen.
        raise ValueError("no_qualifying_opportunity_requires_complete_coverage")
    return r


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("receipt_dir_is_symlink")
    if not path.is_dir():
        if not path.parent.is_dir():
            raise ValueError("receipt_dir_parent_missing")
        path.mkdir(mode=0o700)
    return path


def _write_json(path, value, *, replace):
    data = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)  # atomic no-clobber
            except FileExistsError:
                raise ValueError("receipt_exists") from None
            os.unlink(tmp)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return path


def write_receipt(directory, receipt, policy=None):
    """Atomically write ``<dir>/<local-date>.json`` (0600, dir 0700); never overwrites."""
    receipt = finalize_receipt(receipt, policy)
    base = private_dir(directory)
    return _write_json(base / (as_date(receipt["date"]).isoformat() + ".json"), receipt, replace=False)


def write_outbox(directory, proposal):
    oid = proposal["source"]["work_item_id"]
    if not OPPORTUNITY.fullmatch(oid):
        raise ValueError("opportunity_identity_mismatch")
    box = private_dir(Path(private_dir(directory)) / "outbox")
    return _write_json(box / (oid + ".json"), proposal, replace=True)


def load_history(directory):
    """Receipts under ``directory`` plus the names of files that were ignored."""
    base, history, ignored = Path(directory), [], []
    if not base.is_dir():
        return history, ignored
    for path in sorted(base.iterdir()):
        if not DATE_FILE.fullmatch(path.name) or not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ignored.append(path.name)
            continue
        if isinstance(value, dict) and value.get("schema") == SCHEMA and value.get("date") == path.name[:10]:
            history.append(value)
        else:
            ignored.append(path.name)
    return history, ignored


def outbox_corpus(directory):
    """Earlier handoffs as novelty corpus entries (kind ``docket``)."""
    box, out = Path(directory) / "outbox", []
    if not box.is_dir():
        return out
    for path in sorted(box.glob("opportunity-*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            origin, packet = value["origin"], value["packet"]
            out.append({"id": value["source"]["work_item_id"], "kind": "docket",
                        "text": "%s %s" % (origin["question"], packet["recommendation"])})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


# --------------------------------------------------------------------------- #
# host helpers: parse model output, assemble a draft, settle it into a receipt
# --------------------------------------------------------------------------- #
def parse_model_json(text):
    """The JSON object in a model's last message (bare, fenced, or embedded)."""
    if not isinstance(text, str):
        raise ValueError("model_output_not_text")
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", body, re.S)
    if fence:
        body = fence.group(1)
    elif not body.startswith("{"):
        first, last = body.find("{"), body.rfind("}")
        if first < 0 or last <= first:
            raise ValueError("model_output_has_no_json")
        body = body[first:last + 1]
    try:
        value = json.loads(body)
    except ValueError:
        raise ValueError("model_output_malformed_json") from None
    if not isinstance(value, dict):
        raise ValueError("model_output_not_object")
    return value


def assemble_draft(plan, output, challenges, *, generator, challenger, started_at, now,
                   model_launches, failed=None):
    """Join the plan, the generator's output and the fresh challenges into a draft.

    The host, not the model, sets ``generated_by`` and ``challenge.by``; any challenge
    the generator wrote about its own candidate is discarded.
    """
    now, started = instant(now), instant(started_at)
    draft = {"schema": DRAFT_SCHEMA, "date": plan["date"], "mode": plan["mode"],
             "covers_date": plan["covers_date"], "started_at": stamp(started), "finished_at": stamp(now),
             "allocation": plan["reserved"],
             "used": {"wall_seconds": max(0, int((now - started).total_seconds())),
                      "model_launches": model_launches, "source_opens": 0},
             "perspective": plan["perspective"], "questions": [],
             "coverage": {"internal": "unavailable", "external": "unavailable", "gaps": []},
             "candidates": [], "outcome": None, "preempted_by": None, "recover_by": None}
    value = None
    if failed is None:
        try:
            value = output if isinstance(output, dict) else parse_model_json(output)
        except ValueError as exc:
            failed = str(exc)
    if failed is not None:
        draft["coverage"]["gaps"] = [clip("pass_failed: " + str(failed), 300)]
        draft["outcome"] = "failed"
        return draft
    questions = value.get("questions")
    draft["questions"] = [clip(q, 500) for q in questions[:5] if isinstance(q, str) and prose(q)] \
        if isinstance(questions, list) else []
    cov = value.get("coverage") if isinstance(value.get("coverage"), dict) else {}
    gaps = cov.get("gaps") if isinstance(cov.get("gaps"), list) else []
    draft["coverage"] = {"internal": cov.get("internal") if cov.get("internal") in COVERAGE else "unavailable",
                         "external": cov.get("external") if cov.get("external") in COVERAGE else "unavailable",
                         "gaps": [clip(g, 300) for g in gaps if isinstance(g, str) and prose(g)][:10]}
    opens = (value.get("used") or {}).get("source_opens") if isinstance(value.get("used"), dict) else None
    if type(opens) is int and opens >= 0:
        draft["used"]["source_opens"] = opens  # self-reported by the model
    else:
        draft["coverage"]["gaps"].append("source_opens_not_reported")
    candidates = value.get("candidates") if isinstance(value.get("candidates"), list) else []
    for index, raw in enumerate(candidates):
        c = dict(raw) if isinstance(raw, dict) else {"_malformed": raw}
        c.pop("challenge", None)
        c["generated_by"] = generator
        ch = (challenges or {}).get(index)
        if isinstance(ch, dict) and ch.get("verdict") in VERDICTS:
            c["challenge"] = {"by": challenger, "verdict": ch["verdict"],
                              "notes": clip(ch.get("notes") if isinstance(ch.get("notes"), str) else "", 2000)}
        draft["candidates"].append(c)
    return draft


def _row(c, status, reasons):
    key = c.get("dedupe_key") if isinstance(c, dict) else None
    ok = isinstance(key, str) and SLUG.fullmatch(key) is not None
    return {"dedupe_key": key if isinstance(key, str) else None,
            "opportunity_id": opportunity_id(key) if ok else None, "status": status, "reasons": reasons}


def settle(draft, *, now, corpus=None, docket=None, docket_problem=None, policy=None):
    """Turn a draft into (receipt, proposals) deterministically.

    Order: shape/challenge validation -> suppression -> novelty -> allocation caps.
    ``docket_problem`` (the docket was named but unreadable) holds every otherwise
    eligible candidate, because a declined idea must not be re-presented unseen.
    """
    policy = merged(policy)
    now = instant(now)
    alloc = draft.get("allocation") or {}
    deep = alloc.get("deep_candidates", policy["deep_candidates"])
    cap = alloc.get("present_max", policy["present_max"])
    rows, proposals = [], []
    gaps = list(draft["coverage"]["gaps"])
    if docket is None and docket_problem is None:
        gaps.append("docket_not_consulted")
    elif docket_problem:
        gaps.append("docket_unreadable: " + docket_problem)
    for index, c in enumerate(draft.get("candidates") or []):
        if index >= deep:
            rows.append(_row(c, "rejected", ["over_deep_candidate_allocation"]))
            continue
        reasons = validate_candidate(c)
        if reasons:
            rows.append(_row(c, "rejected", reasons))
            continue
        if docket_problem:
            rows.append(_row(c, "held", ["suppression_unverifiable"]))
            continue
        verdict = suppression(c, docket or [], now)
        if verdict["status"] == "suppressed":
            rows.append(_row(c, "suppressed", ["same_evidence_as_resolved_opportunity"]))
            continue
        if verdict["status"] == "snoozed":
            rows.append(_row(c, "held", ["snoozed_until:" + verdict["until"]]))
            continue
        oid = opportunity_id(c["dedupe_key"])
        pool = [e for e in corpus or [] if isinstance(e, dict) and e.get("id") not in {oid, c["dedupe_key"]}]
        novelty = novelty_check(c, pool, policy)
        if not novelty["novel"]:
            rows.append(_row(c, "rejected", ["not_novel:%s:%.2f" % novelty["nearest"][0]]))
            continue
        if len(proposals) >= cap:
            rows.append(_row(c, "held", ["present_max_reached"]))
            continue
        event_id = "fa-intelligence-%s-%s" % (draft["date"], oid[-12:])
        try:
            proposals.append(to_docket_propose(c, now=now, event_id=event_id, verified_at=now, policy=policy))
        except ValueError as exc:
            rows.append(_row(c, "rejected", ["docket_shape:" + str(exc)]))
            continue
        extra = [verdict["status"]] if verdict["status"] != "eligible" else []
        rows.append(_row(c, "presented_to_outbox", extra))
    receipt = {k: draft[k] for k in RECEIPT_KEYS - {"schema", "candidates", "coverage", "outcome"}}
    coverage = dict(draft["coverage"], gaps=gaps)
    if (docket is None or docket_problem) and coverage["internal"] == "complete":
        coverage["internal"] = "partial"  # suppression could not be checked against the docket
    receipt.update(schema=SCHEMA, candidates=rows, coverage=coverage)
    cov = receipt["coverage"]
    if draft.get("outcome") in {"failed", "preempted"}:
        receipt["outcome"] = draft["outcome"]
    elif proposals:
        receipt["outcome"] = "opportunities_prepared"
    elif cov["internal"] != "complete" or cov["external"] != "complete":
        receipt["outcome"] = "coverage_gap"
    else:
        receipt["outcome"] = draft.get("outcome") or "no_qualifying_opportunity"
    return finalize_receipt(receipt, policy), proposals


def preemption_receipt(plan, now):
    now = stamp(now)
    return {"schema": SCHEMA, "date": plan["date"], "mode": "skip", "covers_date": plan["covers_date"] or plan["date"],
            "started_at": now, "finished_at": now, "allocation": {},
            "used": {"wall_seconds": 0, "model_launches": 0, "source_opens": 0}, "perspective": None,
            "questions": [], "coverage": {"internal": "unavailable", "external": "unavailable",
                                          "gaps": ["preempted_by_incident"]},
            "candidates": [], "outcome": "preempted", "preempted_by": plan["preempted_by"],
            "recover_by": plan["recover_by"]}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def read_json_file(path, limit=MAX_JSON):
    data = Path(path).read_bytes()
    if len(data) > limit:
        raise ValueError("file_too_large")
    return json.loads(data.decode("utf-8"))


def load_corpus(path):
    if not path:
        return []
    value = read_json_file(path)
    value = value.get("entries") if isinstance(value, dict) else value
    if not isinstance(value, list):
        raise ValueError("invalid_corpus")
    return value


def emit(value):
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="decide whether today's pass runs (exit 0 run, 4 skip)")
    plan.add_argument("--dir", required=True, help="receipt directory")
    plan.add_argument("--backlog-count", type=int, default=0)
    plan.add_argument("--corpus", help="corpus JSON; its backlog entries set --backlog-count")
    plan.add_argument("--urgent", help="incident id that preempts today's pass")
    plan.add_argument("--record-preemption", action="store_true", help="write the preemption receipt")
    plan.add_argument("--ignore-window", action="store_true", help="stub rehearsals only")
    val = sub.add_parser("validate", help="exit 0 valid, 1 invalid")
    val.add_argument("--candidate", required=True)
    nov = sub.add_parser("novelty", help="exit 0 novel, 1 not novel")
    nov.add_argument("--candidate", required=True)
    nov.add_argument("--corpus", required=True)
    todo = sub.add_parser("to-docket", help="print the docket propose input")
    todo.add_argument("--candidate", required=True)
    todo.add_argument("--event-id", required=True)
    todo.add_argument("--verified-at")
    cand = sub.add_parser("candidate", help="host helper: print candidate N of a model output (exit 3 if none)")
    cand.add_argument("--output", required=True)
    cand.add_argument("--index", type=int, required=True)
    draft = sub.add_parser("draft", help="host helper: assemble a draft receipt")
    draft.add_argument("--plan", required=True)
    draft.add_argument("--output")
    draft.add_argument("--challenge", action="append", default=[], metavar="N:FILE")
    draft.add_argument("--generator", required=True)
    draft.add_argument("--challenger", required=True)
    draft.add_argument("--started-at", required=True)
    draft.add_argument("--model-launches", type=int, required=True)
    draft.add_argument("--failed", help="record the pass as failed with this reason")
    fin = sub.add_parser("finalize", help="settle a draft (or check a receipt) and write it")
    fin.add_argument("--receipt", required=True)
    fin.add_argument("--dir", help="receipt directory; omit to validate only")
    fin.add_argument("--corpus")
    fin.add_argument("--docket", help="raw IRIS docket JSON, read-only")
    for parser in (plan, todo, draft, fin):
        parser.add_argument("--now", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    try:
        now = instant(a.now) if getattr(a, "now", None) else dt.datetime.now(dt.timezone.utc)
        if a.command == "plan":
            history, ignored = load_history(a.dir)
            backlog = a.backlog_count
            if a.corpus:
                backlog = sum(1 for e in load_corpus(a.corpus) if isinstance(e, dict) and e.get("kind") == "backlog")
            result = plan_pass(history, now, backlog_count=backlog, urgent=a.urgent, ignore_window=a.ignore_window)
            result["ignored_receipts"] = ignored
            if result["reason"] == "preempted_by_incident" and a.record_preemption:
                result["receipt_path"] = str(write_receipt(a.dir, preemption_receipt(result, now)))
            emit(result)
            return 0 if result["run"] else 4
        if a.command == "validate":
            reasons = validate_candidate(read_json_file(a.candidate))
            emit({"valid": not reasons, "reasons": reasons})
            return 0 if not reasons else 1
        if a.command == "novelty":
            result = novelty_check(read_json_file(a.candidate), load_corpus(a.corpus))
            emit(result)
            return 0 if result["novel"] else 1
        if a.command == "to-docket":
            emit(to_docket_propose(read_json_file(a.candidate), now=now, event_id=a.event_id,
                                   verified_at=a.verified_at or now))
            return 0
        if a.command == "candidate":
            value = parse_model_json(Path(a.output).read_text(encoding="utf-8"))
            items = value.get("candidates") if isinstance(value.get("candidates"), list) else []
            if not 0 <= a.index < len(items):
                return 3
            item = dict(items[a.index]) if isinstance(items[a.index], dict) else items[a.index]
            if isinstance(item, dict):
                item.pop("challenge", None)  # the challenger never sees a self-assessment
            emit(item)
            return 0
        if a.command == "draft":
            plan_value = read_json_file(a.plan)
            challenges = {}
            for spec in a.challenge:
                index, _, path = spec.partition(":")
                try:
                    challenges[int(index)] = parse_model_json(Path(path).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    challenges[int(index)] = None
            output = None
            failed = a.failed
            if failed is None:
                try:
                    output = Path(a.output).read_text(encoding="utf-8") if a.output else None
                except OSError:
                    output = None
                if not output:
                    failed = "generator_output_missing"
            emit(assemble_draft(plan_value, output, challenges, generator=a.generator, challenger=a.challenger,
                                started_at=a.started_at, now=now, model_launches=a.model_launches, failed=failed))
            return 0
        if a.command == "finalize":
            value = read_json_file(a.receipt)
            proposals = []
            if isinstance(value, dict) and value.get("schema") == DRAFT_SCHEMA:
                docket, problem = None, None
                if a.docket:
                    try:
                        docket = docket_events(read_json_file(a.docket))
                    except FileNotFoundError:
                        problem = "missing"
                    except (OSError, ValueError):
                        problem = "unreadable"
                corpus = load_corpus(a.corpus) + (outbox_corpus(a.dir) if a.dir else [])
                receipt, proposals = settle(value, now=now, corpus=corpus, docket=docket, docket_problem=problem)
            else:
                receipt = finalize_receipt(value)
            result = {"outcome": receipt["outcome"], "candidates": receipt["candidates"], "outbox": []}
            if a.dir:
                if (Path(a.dir) / (receipt["date"] + ".json")).exists():
                    raise ValueError("receipt_exists")  # before any outbox write
                for proposal in proposals:
                    result["outbox"].append(str(write_outbox(a.dir, proposal)))
                result["receipt_path"] = str(write_receipt(a.dir, receipt))
            emit(result)
            return 0
    except ValueError as exc:
        emit({"schema": SCHEMA, "refused": str(exc)[:200]})
        return 1
    except Exception as exc:  # noqa: BLE001 -- anything else is a hard error
        emit({"schema": SCHEMA, "error": type(exc).__name__ + ": " + str(exc)[:200]})
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
