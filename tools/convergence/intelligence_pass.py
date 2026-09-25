#!/usr/bin/env python3
"""Daily intelligence pass mechanics: identity, allocation, suppression, handoff.

Code owns the pass identity, allocation accounting, missed-day rules, perspective
rotation, the closed candidate shape, the novelty corpus and a novelty floor,
suppression against earlier docket events, receipts, saved proofs and the docket
handoff file. The model owns the questions, the research and the judgment.

Nothing here calls a model. The only network read is ``build_corpus``'s optional GET
of the local board on the loopback interface. Nothing here writes the IRIS docket: an
accepted candidate becomes an outbox file holding the docket's ``opportunity_proposal``
propose input, and the docket owner decides whether to consume it. The docket is read
as raw JSON without importing IRIS code.

The allocation values in ``POLICY`` are PROPOSALS awaiting Anthony, not rulings.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.parse
import urllib.request
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
    "gap_lookback_days": 7,
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
            "confidence", "recheck_after", "decision_question", "why_now")
OPTIONAL = ("external_evidence", "external_gap", "experiment", "proof_body", "discovered_at")
EXTERNAL_KEYS = frozenset({"source", "published_or_accessed", "claim", "status"})
MAX_PROOF_BYTES = 20 * 1024
PROOF_KINDS = {"analysis", "example", "draft", "benchmark", "prototype"}
EXPERIMENT_KEYS = ("baseline", "mechanism", "success_measure", "ceiling", "stop_rule", "rollback")
VERDICTS = {"survives", "weakened", "refuted"}
COVERAGE = {"complete", "partial", "unavailable"}
MODES = {"scheduled", "scheduled_after_gap", "skip"}
OUTCOMES = {"opportunities_prepared", "no_qualifying_opportunity", "coverage_gap", "preempted", "failed"}
STATUSES = {"presented_to_outbox", "held", "suppressed", "rejected"}
CORPUS_KINDS = {"backlog", "prompt", "bookmark", "radar", "docket"}
NOT_COMPLETED = {"preempted", "failed"}
RECEIPT_KEYS = {"schema", "date", "mode", "covers_date", "gap_dates", "started_at", "finished_at", "allocation",
                "used", "allocation_exceeded", "allocation_exceeded_reason", "perspective", "questions", "coverage",
                "candidates", "outcome", "preempted_by", "recover_by", "corpus_build_seconds", "refusal_reasons"}
# used key -> (allocation key, multiplier to the used unit)
ALLOCATION_LIMITS = (("model_launches", "model_launches", 1), ("wall_seconds", "wall_minutes", 60),
                     ("source_opens", "source_opens", 1))
BOARD_URL = "http://127.0.0.1:4180/data/board.json"
LOOPBACK = re.compile(r"http://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?/")
MAX_BOARD_BYTES = 8 * 1024 * 1024
OPTIONS = (
    ("proceed", "Proceed as prepared",
     "Uses the prepared proof as the starting point; costs the effort estimate and nothing else is authorised."),
    ("modify", "Proceed with changes",
     "Keeps the idea but changes scope or method first; needs one short instruction."),
    ("defer", "Revisit later",
     "Costs nothing now; the idea is rechecked at the next check date."),
    ("decline", "Decline",
     "Stays suppressed until it cites evidence not cited before."),
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
    policy = dict(policy or {})
    if "recovery_lookback_days" in policy:
        # The key's former name is still honoured; the new name wins when both are set.
        policy.setdefault("gap_lookback_days", policy["recovery_lookback_days"])
        del policy["recovery_lookback_days"]
    return dict(POLICY, **policy)


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


def gap_days(receipts, today, policy):
    """The run of days just before ``today`` with no completed pass, oldest first.

    A day counts when it has no receipt, or only a preempted or failed one. The run
    stops at the latest completed pass and never reaches back past the first receipt
    or the lookback, so each missed day is listed by the next pass until one
    completes. Missed days are listed, never re-run and never backdated.
    """
    if not receipts:
        return []
    floor = max(today - dt.timedelta(days=policy["gap_lookback_days"]), min(receipts))
    days, day = [], today - dt.timedelta(days=1)
    while day >= floor:
        receipt = receipts.get(day)
        if receipt is not None and receipt.get("outcome") not in NOT_COMPLETED:
            break
        days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


def plan_pass(history, now, *, backlog_count, urgent=None, policy=None, ignore_window=False):
    """Decide whether today's single pass runs, and in which mode.

    A pass is due once per local day whatever the backlog size. An urgent incident
    preempts it (recorded, never silent). The first pass after a run of missed,
    preempted or failed days still covers TODAY (mode ``scheduled_after_gap``) and
    lists those days in ``gap_dates``; they are never re-run or backdated. At most
    one pass per local day. ``ignore_window`` exists for stub rehearsals only.
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
            "gap_dates": [], "perspective": None, "backlog_count": backlog_count, "planned_at": stamp(now)}
    prior = receipts.get(today)
    if prior is not None:
        if prior.get("outcome") == "preempted":
            return dict(plan, reason="preempted_today", preempted_by=prior.get("preempted_by"),
                        recover_by=stamp(tomorrow_start))
        return dict(plan, reason="already_ran_today")
    if not ignore_window and now < start:
        return dict(plan, reason="before_window")
    if not ignore_window and now >= end:
        # Today becomes a missed day; the next day's pass lists it in gap_dates.
        return dict(plan, reason="window_closed", recover_by=stamp(tomorrow_start))
    if urgent is not None:
        identifier(urgent, "invalid_incident_id")
        return dict(plan, reason="preempted_by_incident", preempted_by=urgent,
                    recover_by=stamp(tomorrow_start), covers_date=today.isoformat())
    gap = gap_days(receipts, today, policy)
    plan.update(run=True, reserved=budget(policy), perspective=choose_perspective(today, history),
                covers_date=today.isoformat())
    if gap:
        return dict(plan, mode="scheduled_after_gap", reason="due_today_after_gap",
                    gap_dates=[d.isoformat() for d in gap])
    return dict(plan, mode="scheduled", reason="due_today")


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


def _url_ok(value):
    return (isinstance(value, str) and 0 < len(value) <= 2000 and value == value.strip()
            and re.match(r"https?://\S+\Z", value) is not None)


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
    for name in ("question", "goal_link", "novelty", "recommendation", "decision_question", "why_now"):
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
                        or set(item) - {"url"} != EXTERNAL_KEYS
                        or ("url" in item and not _url_ok(item["url"]))
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
        # ``ref`` is optional from the model: the host sets it to the opportunity id
        # once it has saved ``proof_body`` under state/intelligence/proofs/.
        proof = c["proof"]
        if (not isinstance(proof, dict) or set(proof) - {"ref"} != {"kind"} or proof["kind"] not in PROOF_KINDS
                or ("ref" in proof and (not isinstance(proof["ref"], str) or not proof["ref"].strip()
                                        or len(proof["ref"]) > 300))):
            reasons.append("invalid_proof")
        else:
            proof_kind = proof["kind"]
    if "proof_body" in c:
        # A local markdown file, never sent to the docket, so the private-text rules
        # do not apply to it; only its size and type are checked.
        body = c["proof_body"]
        if (not isinstance(body, str) or not body.strip() or "\x00" in body
                or len(body.encode("utf-8")) > MAX_PROOF_BYTES):
            reasons.append("invalid_proof_body")
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


IDENTITY_TRAIL = ",.;:/"
URL_LIKE = re.compile(r"https?://", re.I)
MAX_EVIDENCE_REFS = 5  # the docket packet holds at most five evidence refs


def url_identity(value):
    """A URL without its query string or fragment, lowercased, trailing ``,.;:/`` removed."""
    value = value.strip()
    try:
        parts = urllib.parse.urlsplit(value)
        value = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        value = re.split(r"[?#]", value, maxsplit=1)[0]
    return value.lower().rstrip(IDENTITY_TRAIL) or None


def internal_identity(ref):
    """The first whitespace-delimited token of an internal ref, lowercased, with
    trailing ``,.;:/`` removed; a URL token also loses its query and fragment."""
    parts = ref.split() if isinstance(ref, str) else []
    if not parts:
        return None
    if URL_LIKE.match(parts[0]):
        return url_identity(parts[0])
    return parts[0].lower().rstrip(IDENTITY_TRAIL) or None


def external_identity(item):
    """An external item's ``url`` when present, else its source. A URL (either one)
    is normalised by ``url_identity``; a plain source is lowercased, whitespace
    collapsed, cut to 80 characters, with trailing ``,.;:/`` removed."""
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    if isinstance(url, str) and url.strip():
        return url_identity(url)
    source = item.get("source")
    if isinstance(source, str) and source.strip():
        if URL_LIKE.match(source.strip()):
            return url_identity(source)
        return " ".join(source.lower().split())[:80].rstrip(IDENTITY_TRAIL) or None
    return None


def evidence_identities(c):
    """(internal, external) evidence identities in first-seen order, deduplicated."""
    internal = [internal_identity(i.get("ref")) for i in c.get("internal_evidence") or [] if isinstance(i, dict)]
    external = [external_identity(e) for e in c.get("external_evidence") or []]
    return ([x for x in dict.fromkeys(internal) if x], [x for x in dict.fromkeys(external) if x])


def evidence_tokens(c):
    """The docket-safe tokens of every evidence identity (``short_token`` of each),
    internal first, deduplicated. The first five become ``packet.evidence_refs``."""
    internal, external = evidence_identities(c)
    return list(dict.fromkeys(short_token(ref) for ref in internal + external))


def evidence_revision(c):
    """sha256 over the SET of evidence identities, never over model-written prose.

    Internal evidence counts by the first token of each ref (``board:8c7450d0``),
    external evidence by its ``url`` or normalised source. Counterevidence, claims,
    ref descriptions, observation times and wording are excluded, so rewording the
    same idea keeps its revision.
    """
    internal, external = evidence_identities(c)
    return sha({"internal": sorted(internal), "external": sorted(external)})


def docket_events(value):
    if isinstance(value, dict):
        value = value.get("events")
    if not isinstance(value, list):
        raise ValueError("invalid_docket_events")
    return value


def proposed_identities(events, oid):
    """{work_revision: (evidence tokens, complete)} from earlier docket proposals of ``oid``.

    The tokens are the proposal's ``packet.evidence_refs`` ids. A packet holds at
    most five refs, so a proposal with five may have been cut short: it is marked
    incomplete, and only an evidence ledger entry can complete it.
    """
    out = {}
    for event in events:
        if not isinstance(event, dict) or event.get("command") != "propose" or not isinstance(event.get("input"), dict):
            continue
        data = event["input"]
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        packet = data.get("packet") if isinstance(data.get("packet"), dict) else {}
        rev, refs = source.get("work_revision"), packet.get("evidence_refs")
        if source.get("work_item_id") != oid or not isinstance(rev, str) or not isinstance(refs, list):
            continue
        ids = frozenset(r["id"] for r in refs if isinstance(r, dict) and isinstance(r.get("id"), str))
        complete = len(refs) < MAX_EVIDENCE_REFS
        if rev in out:
            ids, complete = ids | out[rev][0], complete or out[rev][1]
        out[rev] = (ids, complete)
    return out


def suppression(c, docket, now=None, ledger=None):
    """Classify a candidate against raw docket events for the same opportunity id.

    A declined, answered or followed-through opportunity becomes eligible again only
    when the candidate cites at least one evidence identity that none of those
    resolved proposals cited. Removing identities, reordering them, rewording, or
    swapping external evidence for an external gap never re-surfaces it. A resolved
    proposal's identities come from ``ledger`` ({work_revision: [tokens]}, written
    by the host beside the outbox) or else its docket ``packet.evidence_refs``; when
    they are unknown or possibly cut short, the candidate stays suppressed because a
    new identity cannot be shown. Whether a new identity is MATERIAL stays with the
    pass, the challenger and Anthony.
    """
    now = instant(now or dt.datetime.now(dt.timezone.utc))
    oid, revision = opportunity_id(c["dedupe_key"]), evidence_revision(c)
    mine = frozenset(evidence_tokens(c))
    events = docket_events(docket)
    proposed = proposed_identities(events, oid)
    resolved, snoozed_until = [], None
    for event in events:
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
    prior, seen, unknown = [], set(), False
    for disposition, rev in resolved:
        stored = ledger.get(rev) if isinstance(ledger, dict) else None
        if rev == revision:
            ids, complete = mine, True  # the same identity set
        elif isinstance(stored, list) and all(isinstance(t, str) for t in stored):
            ids, complete = frozenset(stored), True
        else:
            ids, complete = proposed.get(rev, (frozenset(), False))
        seen |= ids
        unknown = unknown or not complete
        prior.append({"disposition": disposition, "same_evidence": rev == revision, "identities_known": complete})
    new = sorted(mine - seen) if resolved else []
    base = {"opportunity_id": oid, "evidence_revision": revision, "prior": prior, "new_identities": new}
    if any(p["same_evidence"] for p in prior):
        return dict(base, status="suppressed", reason="same_evidence_as_resolved_opportunity")
    if resolved and not new:
        return dict(base, status="suppressed", reason="no_new_evidence_identity")
    if resolved and unknown:
        return dict(base, status="suppressed", reason="prior_evidence_identities_unknown")
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
    if not isinstance(refs, list) or len(refs) > MAX_EVIDENCE_REFS:
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
    if not c["proof"].get("ref"):
        raise ValueError("proof_missing")
    now = instant(now)
    oid, revision = opportunity_id(c["dedupe_key"]), evidence_revision(c)
    proof_ref = {"kind": "artifact", "id": short_token(c["proof"]["ref"])}
    refs = evidence_tokens(c)
    check_at = next_check(c, now, policy)
    discovered = now
    try:
        claimed = instant(c.get("discovered_at"))
        if dt.timedelta(0) <= now - claimed <= dt.timedelta(days=30):
            discovered = claimed  # the candidate's own time, when it is valid
    except ValueError:
        pass
    counter = clip("; ".join(prose(t) for t in c["counterevidence"]), 1000)
    verdict = c["challenge"]["verdict"]
    proposal = {
        "event_id": event_id,
        "action_class": ACTION_CLASS,
        "source": {"work_item_id": oid, "work_revision": revision, "verified_at": stamp(verified_at),
                   "reference": proof_ref, "terminal": False},
        "packet": {
            "why_now": clip("Independent discovery: " + prose(c["why_now"]), 1000),
            "recommendation": prose(c["recommendation"]),
            "question": prose(c["decision_question"]),
            "options": [{"id": i, "label": label, "tradeoff": tradeoff} for i, label, tradeoff in OPTIONS],
            "authority_needed": "Your choice only. This proposal grants no authority and commits no work.",
            "scope": clip("Perspective %s; prepared proof: %s; effort: %s; confidence %.2f; independent "
                          "challenge verdict: %s." % (c["perspective"], c["proof"]["kind"], prose(c["effort"]),
                                                      float(c["confidence"]), verdict), 1000),
            "next_owner": "Fully Aware intelligence pass prepares; Anthony decides.",
            "follow_through": "Rechecked on %s. A decline stays suppressed until new evidence is cited."
                              % check_at.date().isoformat(),
            "evidence_refs": [{"kind": "artifact", "id": token} for token in refs[:MAX_EVIDENCE_REFS]],
        },
        "next_check_at": stamp(check_at),
        "origin": {"kind": "independent_discovery", "question": prose(c["question"]),
                   "goal_link": prose(c["goal_link"]), "novelty": prose(c["novelty"]),
                   "counterevidence": counter, "proof_ref": proof_ref, "discovered_at": stamp(discovered)},
    }
    return check_propose(proposal, now)


# --------------------------------------------------------------------------- #
# receipts
# --------------------------------------------------------------------------- #
def _count(value):
    """A finite, non-negative int or float (never a bool)."""
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def finalize_receipt(receipt, policy=None):
    """Validate a pass receipt against its closed shape and accounting rules."""
    policy = merged(policy)
    if not isinstance(receipt, dict):
        raise ValueError("receipt_not_object")
    r = dict(receipt)
    r.setdefault("schema", SCHEMA)
    r.setdefault("gap_dates", [])
    r.setdefault("allocation_exceeded_reason", None)
    r.setdefault("corpus_build_seconds", None)
    r.setdefault("refusal_reasons", [])
    r["allocation_exceeded"] = None  # always computed here, never taken from the input
    if set(r) != RECEIPT_KEYS or r["schema"] != SCHEMA:
        raise ValueError("invalid_receipt_fields")
    day, covers = as_date(r["date"]), as_date(r["covers_date"])
    if r["mode"] not in MODES or r["outcome"] not in OUTCOMES:
        raise ValueError("invalid_mode_or_outcome")
    if covers != day:
        # Every pass covers its own day; a missed day is listed, never backdated.
        raise ValueError("pass_must_cover_its_own_day")
    gaps = r["gap_dates"]
    if not isinstance(gaps, list) or any(not isinstance(g, str) for g in gaps):
        raise ValueError("invalid_gap_dates")
    if any(as_date(g) >= day for g in gaps) or len(set(gaps)) != len(gaps):
        raise ValueError("invalid_gap_dates")
    if (r["mode"] == "scheduled_after_gap") != bool(gaps):
        raise ValueError("gap_dates_do_not_match_mode")
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
            or any(not _count(used[k]) for k in used)):
        raise ValueError("invalid_used")
    if r["corpus_build_seconds"] is not None and not _count(r["corpus_build_seconds"]):
        # Timed separately: building the corpus is not part of the pass's wall clock.
        raise ValueError("invalid_corpus_build_seconds")
    refusals = r["refusal_reasons"]
    if not isinstance(refusals, list) or any(not isinstance(x, str) or not prose(x) for x in refusals):
        raise ValueError("invalid_refusal_reasons")
    if refusals and r["outcome"] != "failed":
        raise ValueError("refusal_reasons_require_failed_outcome")
    exceeded = []
    for used_key, alloc_key, unit in ALLOCATION_LIMITS:
        limit = r["allocation"].get(alloc_key)
        if type(limit) in (int, float) and used[used_key] > limit * unit:
            exceeded.append(used_key)
    reason = r["allocation_exceeded_reason"]
    if reason is not None and (not isinstance(reason, str) or not prose(reason) or len(reason) > 500):
        raise ValueError("invalid_allocation_exceeded_reason")
    if exceeded and reason is None:
        # Going over budget is allowed only with a stated reason, never silently.
        raise ValueError("allocation_exceeded_without_reason:" + ",".join(exceeded))
    r["allocation_exceeded"] = bool(exceeded)
    if r["perspective"] not in PERSPECTIVES and not (r["perspective"] is None and r["outcome"] in NOT_COMPLETED):
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
        anonymous_hold = row["status"] == "held" and row["dedupe_key"] is None and row["opportunity_id"] is None
        if row["status"] != "rejected" and not anonymous_hold and (
                not isinstance(row["dedupe_key"], str) or row["opportunity_id"] != opportunity_id(row["dedupe_key"])):
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
    return _write_bytes(path, data, replace=replace)


def _write_bytes(path, data, *, replace):
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


def write_proof(directory, oid, body):
    """Atomically write ``<dir>/proofs/<opportunity_id>.md`` (0600, dir 0700).

    The proof is a local file for Anthony; it never enters the docket, so only its
    type and size are checked. A later pass with changed evidence replaces it.
    """
    if not isinstance(oid, str) or not OPPORTUNITY.fullmatch(oid):
        raise ValueError("opportunity_identity_mismatch")
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > MAX_PROOF_BYTES:
        raise ValueError("invalid_proof_body")
    proofs = private_dir(Path(private_dir(directory)) / "proofs")
    return _write_bytes(proofs / (oid + ".md"), body.encode("utf-8"), replace=True)


def write_ledger(directory, oid, revision, tokens):
    """Record a presented proposal's FULL evidence token set at ``evidence/<oid>.json``.

    The docket packet keeps at most five evidence refs; this ledger keeps them all,
    keyed by work revision, so a later decline can be compared identity by identity.
    """
    if not isinstance(oid, str) or not OPPORTUNITY.fullmatch(oid):
        raise ValueError("opportunity_identity_mismatch")
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise ValueError("invalid_work_revision")
    path = private_dir(Path(private_dir(directory)) / "evidence") / (oid + ".json")
    try:
        current = read_json_file(path)
    except (OSError, ValueError):
        current = {}
    current = current if isinstance(current, dict) else {}
    current[revision] = [t for t in tokens if isinstance(t, str)]
    return _write_json(path, current, replace=True)


def load_ledger(directory):
    """{work_revision: [evidence tokens]} from ``evidence/*.json``; unreadable files are skipped."""
    base, out = Path(directory) / "evidence", {}
    if not base.is_dir():
        return out
    for path in sorted(base.glob("opportunity-*.json")):
        try:
            value = read_json_file(path)
        except (OSError, ValueError):
            continue
        for rev, tokens in (value.items() if isinstance(value, dict) else ()):
            if (isinstance(rev, str) and SHA.fullmatch(rev) and isinstance(tokens, list)
                    and all(isinstance(t, str) for t in tokens)):
                out[rev] = tokens
    return out


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


def _entry(entry_id, kind, text):
    text = prose(text) if isinstance(text, str) else ""
    return {"id": entry_id, "kind": kind, "text": clip(text, 1000)} if text else None


def _read_text(path, limit=1024 * 1024):
    data = Path(path).read_bytes()
    if len(data) > limit:
        raise ValueError("file_too_large")
    return data.decode("utf-8", "replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("board_redirect_refused")


def _board_entries(url):
    if not LOOPBACK.match(url or ""):
        raise ValueError("board_url_not_loopback")
    opener = urllib.request.build_opener(_NoRedirect)  # a redirect could leave loopback
    with opener.open(url, timeout=5) as response:
        data = response.read(MAX_BOARD_BYTES + 1)
    if len(data) > MAX_BOARD_BYTES:
        raise ValueError("board_too_large")
    value = json.loads(data.decode("utf-8"))
    items = value.get("items") if isinstance(value, dict) else None
    if not isinstance(items, list):
        raise ValueError("board_items_missing")
    out = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("project") or item.get("summary")
        text = " ".join(t for t in (title, item.get("next_action")) if isinstance(t, str))
        out.append(_entry("board:%s" % (item.get("id") if isinstance(item.get("id"), str) else index), "backlog", text))
    return out


def _plans_entries(path):
    value = read_json_file(path)
    lanes = value.get("lanes") if isinstance(value, dict) else None
    if not isinstance(lanes, list):
        raise ValueError("plans_lanes_missing")
    out = []
    for lane in lanes:
        if not isinstance(lane, dict) or not isinstance(lane.get("waiting_on_anthony"), list):
            continue
        name = lane.get("name") if isinstance(lane.get("name"), str) else "lane"
        for index, text in enumerate(lane["waiting_on_anthony"]):
            out.append(_entry("plans:%s:waiting-%d" % (name, index), "backlog", text))
    return out


TABLE_ROW = re.compile(r"^\|(.+)\|\s*$")
BOLD_LEAD = re.compile(r"^\*\*([A-Z][A-Z0-9-]*\d)\b(.*)$")


def _table_rows(text):
    for line in text.splitlines():
        match = TABLE_ROW.match(line.strip())
        if not match:
            continue
        cells = [c.strip() for c in match.group(1).split("|")]
        if not cells or not cells[0] or set(cells[0]) <= set("-: ") or cells[0].lower() in {"id", "id / dedup key"}:
            continue
        yield cells


def _radar_entries(radar_dir):
    out = []
    topics = _read_text(Path(radar_dir) / "TOPICS.md")
    for cells in _table_rows(topics):
        out.append(_entry("radar-topic:" + cells[0].split()[0], "radar", " ".join(cells[1:3])))
    decisions = _read_text(Path(radar_dir) / "DECISIONS.md")
    for cells in _table_rows(decisions):
        out.append(_entry("radar-decision:" + cells[0].split()[0], "radar", " ".join(cells[:3])))
    for line in decisions.splitlines():
        match = BOLD_LEAD.match(line.strip())
        if match:
            out.append(_entry("radar-decision:" + match.group(1), "radar", line.replace("*", "")))
    return out


def _docket_entries(path):
    out = []
    for event in docket_events(read_json_file(path)):
        data = event.get("input") if isinstance(event, dict) else None
        if not isinstance(data, dict) or event.get("command") != "propose":
            continue
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        packet = data.get("packet") if isinstance(data.get("packet"), dict) else {}
        work = source.get("work_item_id")
        if not isinstance(work, str):
            continue
        # The id is the work item itself, so a candidate's own earlier proposal is
        # excluded from its novelty check (suppression judges it instead).
        text = " ".join(t for t in (packet.get("question"), packet.get("recommendation")) if isinstance(t, str))
        out.append(_entry(work, "docket", text))
    return out


def default_corpus_config(home=None):
    home = Path(home or Path.home())
    return {"board_url": BOARD_URL,
            "plans_snapshot": str(home / "code" / "state" / "plans-snapshot.json"),
            "radar_dir": str(home / "Documents" / "ChatGPT" / "ai-radar-research"),
            "docket": None, "intelligence_dir": None}


def build_corpus(config=None):
    """The novelty corpus: {entries: [{id, kind, text}], gaps: [...]}.

    Reads, read-only and tolerating absence: board titles and next actions (a
    loopback GET of the local board, 5 s timeout), items waiting on Anthony in the
    plans snapshot, Radar topics and decisions, earlier docket packet questions and
    recommendations, and earlier outbox proposals. Every source that is not
    configured or cannot be read becomes a gap; nothing is silently skipped.
    """
    cfg = dict(default_corpus_config(), **(config or {}))
    entries, gaps, counts = [], [], {}
    sources = (("board", cfg.get("board_url"), _board_entries),
               ("plans_snapshot", cfg.get("plans_snapshot"), _plans_entries),
               ("radar", cfg.get("radar_dir"), _radar_entries),
               ("docket", cfg.get("docket"), _docket_entries),
               ("outbox", cfg.get("intelligence_dir"), outbox_corpus))
    for name, where, reader in sources:
        if not where:
            gaps.append("%s: not configured" % name)
            continue
        try:
            found = [e for e in reader(where) if e]
        except FileNotFoundError:
            gaps.append("%s: missing" % name)
            continue
        except Exception as exc:  # noqa: BLE001 -- any read failure is a recorded gap
            gaps.append(clip("%s: unavailable (%s)" % (name, type(exc).__name__), 300))
            continue
        counts[name] = len(found)
        entries.extend(found)
    unique = list({(e["id"], e["kind"], e["text"]): e for e in entries}.values())
    return {"schema": "fa-intelligence-corpus/v1", "entries": unique, "gaps": gaps, "counts": counts}


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
                   model_launches, failed=None, exceeded_reason=None, corpus_build_seconds=None):
    """Join the plan, the generator's output and the fresh challenges into a draft.

    The host, not the model, sets ``generated_by`` and ``challenge.by``; any challenge
    the generator wrote about its own candidate is discarded. ``exceeded_reason`` is
    the host's explanation for running over the allocation (for example a watchdog);
    otherwise the model's own ``allocation_exceeded_reason`` is used, if it gave one.
    ``started_at`` is taken after the novelty corpus is built; the build is timed
    separately as ``corpus_build_seconds`` and never counts against the wall budget.
    """
    now, started = instant(now), instant(started_at)
    draft = {"schema": DRAFT_SCHEMA, "date": plan["date"], "mode": plan["mode"],
             "covers_date": plan["covers_date"], "gap_dates": list(plan.get("gap_dates") or []),
             "started_at": stamp(started), "finished_at": stamp(now),
             "allocation": plan["reserved"],
             "used": {"wall_seconds": max(0, int((now - started).total_seconds())),
                      "model_launches": model_launches, "source_opens": 0},
             "allocation_exceeded_reason": clip(exceeded_reason, 500) if isinstance(exceeded_reason, str)
             and prose(exceeded_reason) else None,
             "perspective": plan["perspective"], "questions": [],
             "coverage": {"internal": "unavailable", "external": "unavailable", "gaps": []},
             "candidates": [], "outcome": None, "preempted_by": None, "recover_by": None,
             "corpus_build_seconds": corpus_build_seconds if _count(corpus_build_seconds) else None}
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
    stated = value.get("allocation_exceeded_reason")
    if draft["allocation_exceeded_reason"] is None and isinstance(stated, str) and prose(stated):
        draft["allocation_exceeded_reason"] = clip("model: " + stated, 500)
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


def settle(draft, *, now, corpus=None, corpus_gaps=None, docket=None, docket_problem=None, policy=None,
           ledger=None):
    """Turn a draft into (receipt, proposals) deterministically.

    Order: shape/challenge validation -> suppression -> novelty -> saved proof ->
    allocation caps. ``docket_problem`` (the docket was named but unreadable) holds
    every otherwise eligible candidate, because a declined idea must not be
    re-presented unseen. A candidate without ``proof_body`` is held, never presented.
    ``corpus=None`` (no corpus built) and any ``corpus_gaps`` lower internal coverage.
    A presented proposal's proof ref is the opportunity id: the host saves the proof
    at ``proofs/<opportunity_id>.md``. ``ledger`` ({work_revision: [evidence tokens]})
    holds the full identity sets of earlier proposals for suppression.
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
    corpus_short = corpus is None or bool(corpus_gaps)
    if corpus is None:
        gaps.append("novelty_corpus_not_built")
    gaps.extend(clip("novelty_corpus_gap: " + str(g), 300) for g in corpus_gaps or [])
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
        verdict = suppression(c, docket or [], now, ledger)
        if verdict["status"] == "suppressed":
            rows.append(_row(c, "suppressed", [verdict["reason"]]))
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
        if "proof_body" not in c:
            rows.append(_row(c, "held", ["proof_missing"]))
            continue
        if any(p["source"]["work_item_id"] == oid for p in proposals):
            rows.append(_row(c, "held", ["duplicate_opportunity_in_pass"]))
            continue
        if len(proposals) >= cap:
            rows.append(_row(c, "held", ["present_max_reached"]))
            continue
        event_id = "fa-intelligence-%s-%s" % (draft["date"], oid[-12:])
        saved = dict(c, proof=dict(c["proof"], ref=oid))
        try:
            proposals.append(to_docket_propose(saved, now=now, event_id=event_id, verified_at=now, policy=policy))
        except ValueError as exc:
            rows.append(_row(c, "rejected", ["docket_shape:" + str(exc)]))
            continue
        extra = [verdict["status"]] if verdict["status"] != "eligible" else []
        rows.append(_row(c, "presented_to_outbox", extra))
    receipt = {k: draft.get(k) for k in RECEIPT_KEYS - {"schema", "candidates", "coverage", "outcome",
                                                         "allocation_exceeded"}}
    receipt["gap_dates"] = receipt["gap_dates"] or []
    receipt["refusal_reasons"] = []
    coverage = dict(draft["coverage"], gaps=gaps)
    if (docket is None or docket_problem or corpus_short) and coverage["internal"] == "complete":
        coverage["internal"] = "partial"  # suppression or novelty was not checked against everything
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
            "gap_dates": [], "started_at": now, "finished_at": now, "allocation": {},
            "used": {"wall_seconds": 0, "model_launches": 0, "source_opens": 0},
            "allocation_exceeded_reason": None, "perspective": None,
            "questions": [], "coverage": {"internal": "unavailable", "external": "unavailable",
                                          "gaps": ["preempted_by_incident"]},
            "candidates": [], "outcome": "preempted", "preempted_by": plan["preempted_by"],
            "recover_by": plan["recover_by"], "corpus_build_seconds": None, "refusal_reasons": []}


def refusal_receipt(value, reason, *, now, policy=None):
    """The ``failed`` receipt written when finalize refuses a draft or receipt.

    A refusal is never silent: the day is still recorded, so ``plan_pass`` treats it
    as ran-and-failed (``already_ran_today``, then listed in the next pass's
    ``gap_dates``) rather than missed. It keeps what can be salvaged from ``value``
    (allocation, used, perspective, questions, coverage), records the refusal in
    ``refusal_reasons`` and the coverage gaps, and marks every candidate ``held``:
    nothing refused is ever presented. When ``used`` exceeds the allocation with no
    stated reason, the excess is recorded as unexplained rather than refused again.
    """
    policy = merged(policy)
    now = instant(now)
    v = value if isinstance(value, dict) else {}
    reason = clip(str(reason) or "unknown_refusal", 300)
    try:
        day = as_date(v.get("date"))
    except ValueError:
        day = local_date(now, policy)
    gaps = []
    for raw in v.get("gap_dates") if isinstance(v.get("gap_dates"), list) else []:
        try:
            gap = as_date(raw)
        except ValueError:
            continue
        if gap < day and gap.isoformat() not in gaps:
            gaps.append(gap.isoformat())

    def moment(key, default):
        try:
            return instant(v.get(key))
        except ValueError:
            return default
    finished = moment("finished_at", now)
    started = min(moment("started_at", finished), finished)
    alloc = v.get("allocation") if isinstance(v.get("allocation"), dict) else {}
    raw_used = v.get("used") if isinstance(v.get("used"), dict) else {}
    used = {k: raw_used.get(k) if _count(raw_used.get(k)) else 0
            for k in ("wall_seconds", "model_launches", "source_opens")}
    exceeded = [u for u, a, unit in ALLOCATION_LIMITS
                if type(alloc.get(a)) in (int, float) and used[u] > alloc[a] * unit]
    stated = v.get("allocation_exceeded_reason")
    if isinstance(stated, str) and prose(stated):
        stated = clip(stated, 500)
    elif exceeded:
        stated = "not stated: the allocation was exceeded (%s) and finalize refused the receipt" % ",".join(exceeded)
    else:
        stated = None
    cov = v.get("coverage") if isinstance(v.get("coverage"), dict) else {}
    cov_gaps = [clip(g, 300) for g in cov.get("gaps") or [] if isinstance(g, str) and prose(g)][:10] \
        if isinstance(cov.get("gaps"), list) else []
    rows = []
    for c in v.get("candidates") if isinstance(v.get("candidates"), list) else []:
        key = c.get("dedupe_key") if isinstance(c, dict) else None
        if isinstance(key, str) and len(key) <= 80 and SLUG.fullmatch(key):
            rows.append({"dedupe_key": key, "opportunity_id": opportunity_id(key), "status": "held",
                         "reasons": ["receipt_refused"]})
        else:
            rows.append({"dedupe_key": None, "opportunity_id": None, "status": "held",
                         "reasons": ["receipt_refused"]})
    questions = v.get("questions") if isinstance(v.get("questions"), list) else []
    receipt = {
        "schema": SCHEMA, "date": day.isoformat(), "mode": "scheduled_after_gap" if gaps else "scheduled",
        "covers_date": day.isoformat(), "gap_dates": sorted(gaps), "started_at": stamp(started),
        "finished_at": stamp(finished), "allocation": alloc, "used": used, "allocation_exceeded_reason": stated,
        "perspective": v.get("perspective") if v.get("perspective") in PERSPECTIVES else None,
        "questions": [clip(q, 500) for q in questions if isinstance(q, str) and prose(q)][:5],
        "coverage": {"internal": cov.get("internal") if cov.get("internal") in COVERAGE else "unavailable",
                     "external": cov.get("external") if cov.get("external") in COVERAGE else "unavailable",
                     "gaps": cov_gaps + ["receipt_refused: " + reason]},
        "candidates": rows, "outcome": "failed", "preempted_by": None, "recover_by": None,
        "corpus_build_seconds": v.get("corpus_build_seconds") if _count(v.get("corpus_build_seconds")) else None,
        "refusal_reasons": [reason],
    }
    return finalize_receipt(receipt, policy)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def read_json_file(path, limit=MAX_JSON):
    data = Path(path).read_bytes()
    if len(data) > limit:
        raise ValueError("file_too_large")
    return json.loads(data.decode("utf-8"))


def load_corpus(path):
    return load_corpus_file(path)[0]


def load_corpus_file(path):
    """(entries, gaps) from a corpus file: a bare list or ``build_corpus`` output."""
    if not path:
        return [], []
    value = read_json_file(path)
    gaps = value.get("gaps", []) if isinstance(value, dict) else []
    value = value.get("entries") if isinstance(value, dict) else value
    if not isinstance(value, list) or not isinstance(gaps, list):
        raise ValueError("invalid_corpus")
    return value, [g for g in gaps if isinstance(g, str)]


def presented_candidates(draft, receipt):
    """{opportunity_id: candidate} for the presented candidates (rows follow candidate order)."""
    out = {}
    for c, row in zip(draft.get("candidates") or [], receipt["candidates"]):
        if row["status"] == "presented_to_outbox":
            out[row["opportunity_id"]] = c
    return out


def proof_bodies(draft, receipt):
    """{opportunity_id: proof_body} for the presented candidates."""
    return {oid: c["proof_body"] for oid, c in presented_candidates(draft, receipt).items()}


def emit(value):
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def finalize_command(a, now):
    """Settle a draft (or check a receipt) and, with ``--dir``, write it.

    With ``--dir`` a refusal is never silent: a ``failed`` receipt recording the
    refusal is written instead (no proof, outbox or ledger file) and the exit code
    is 5. Without ``--dir`` it only validates, and a refusal exits 1.
    """
    proposals, presented, refused, value = [], {}, None, None
    try:
        value = read_json_file(a.receipt)
        if isinstance(value, dict) and value.get("schema") == DRAFT_SCHEMA:
            docket, problem = None, None
            if a.docket:
                try:
                    docket = docket_events(read_json_file(a.docket))
                except FileNotFoundError:
                    problem = "missing"
                except (OSError, ValueError):
                    problem = "unreadable"
            corpus, corpus_gaps = None, []
            if a.corpus:
                try:
                    corpus, corpus_gaps = load_corpus_file(a.corpus)
                except FileNotFoundError:
                    corpus, corpus_gaps = [], ["corpus_file_missing"]
                except (OSError, ValueError):
                    corpus, corpus_gaps = [], ["corpus_file_unreadable"]
            elif a.dir:
                corpus, corpus_gaps = [], ["corpus_not_built: only earlier outbox files checked"]
            if corpus is not None and a.dir:
                pool = [e for e in corpus if isinstance(e, dict)] + outbox_corpus(a.dir)
                corpus = list({(e.get("id"), e.get("text")): e for e in pool}.values())
            receipt, proposals = settle(value, now=now, corpus=corpus, corpus_gaps=corpus_gaps, docket=docket,
                                        docket_problem=problem, ledger=load_ledger(a.dir) if a.dir else None)
            presented = presented_candidates(value, receipt)
        else:
            receipt = finalize_receipt(value)
    except (OSError, ValueError) as exc:
        if not a.dir:
            raise ValueError(str(exc)) from None
        refused = (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)[:200] or "unreadable_input"
        receipt, proposals, presented = refusal_receipt(value, refused, now=now), [], {}
    result = {"outcome": receipt["outcome"], "candidates": receipt["candidates"], "outbox": [], "proofs": []}
    if refused:
        result["refused"] = refused
    if a.dir:
        if (Path(a.dir) / (receipt["date"] + ".json")).exists():
            raise ValueError("receipt_exists")  # the day is already recorded; before any outbox write
        for proposal in proposals:
            oid = proposal["source"]["work_item_id"]
            c = presented[oid]
            # The proof lands first, so an outbox file never names a missing proof.
            result["proofs"].append(str(write_proof(a.dir, oid, c["proof_body"])))
            write_ledger(a.dir, oid, proposal["source"]["work_revision"], evidence_tokens(c))
            result["outbox"].append(str(write_outbox(a.dir, proposal)))
        result["receipt_path"] = str(write_receipt(a.dir, receipt))
    emit(result)
    return 5 if refused else 0


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
    draft.add_argument("--allocation-exceeded-reason", help="host's reason for running over the allocation")
    draft.add_argument("--corpus-build-seconds", type=float,
                       help="time spent building the corpus (recorded, never counted as pass wall time)")
    fin = sub.add_parser("finalize", help="settle a draft (or check a receipt) and write it; with --dir a "
                                          "refusal still writes a failed receipt and exits 5")
    fin.add_argument("--receipt", required=True)
    fin.add_argument("--dir", help="receipt directory; omit to validate only")
    fin.add_argument("--corpus", help="corpus JSON (build_corpus output); omitted = recorded as a gap")
    fin.add_argument("--docket", help="raw IRIS docket JSON, read-only")
    corp = sub.add_parser("corpus", help="build the novelty corpus (read-only sources; gaps recorded)")
    corp.add_argument("--dir", required=True, help="receipt directory (earlier outbox files)")
    corp.add_argument("--out", help="write the corpus here (0600) instead of printing it")
    corp.add_argument("--docket", help="raw IRIS docket JSON, read-only")
    corp.add_argument("--board-url", default=BOARD_URL, help="loopback board URL; empty = not read")
    corp.add_argument("--plans", help="plans snapshot JSON (default ~/code/state/plans-snapshot.json)")
    corp.add_argument("--radar-dir", help="Radar research dir (default ~/Documents/ChatGPT/ai-radar-research)")
    for parser in (plan, todo, draft, fin, corp):
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
        if a.command == "corpus":
            config = {"board_url": a.board_url or None, "docket": a.docket, "intelligence_dir": a.dir}
            if a.plans:
                config["plans_snapshot"] = a.plans
            if a.radar_dir:
                config["radar_dir"] = a.radar_dir
            corpus = dict(build_corpus(config), built_at=stamp(now))
            if a.out:
                out = Path(a.out)
                private_dir(a.dir)
                private_dir(out.parent)
                _write_json(out, corpus, replace=True)
                emit({"path": str(out), "entries": len(corpus["entries"]), "gaps": corpus["gaps"],
                      "counts": corpus["counts"]})
            else:
                emit(corpus)
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
                                started_at=a.started_at, now=now, model_launches=a.model_launches, failed=failed,
                                exceeded_reason=a.allocation_exceeded_reason,
                                corpus_build_seconds=a.corpus_build_seconds))
            return 0
        if a.command == "finalize":
            return finalize_command(a, now)
    except ValueError as exc:
        emit({"schema": SCHEMA, "refused": str(exc)[:200]})
        return 1
    except Exception as exc:  # noqa: BLE001 -- anything else is a hard error
        emit({"schema": SCHEMA, "error": type(exc).__name__ + ": " + str(exc)[:200]})
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
