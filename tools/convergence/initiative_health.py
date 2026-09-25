#!/usr/bin/env python3
"""Read-only initiative health: can anything currently originate and advance work?

Answers, at session start and in the morning digest, whether a driver exists that
can wake IRIS, when it last produced a sweep outcome, and what would wake it next.
A stopped initiative loop must never read as a quiet day, so every absent or
unreadable input is reported as ``unavailable`` with a reason, never as healthy.

This module never schedules, resumes, retries, repairs, notifies or writes. SQLite
stores are opened with ``mode=ro``; the docket is parsed as JSON without importing
IRIS code, so its figures are an event-level summary, not an IRIS replay.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import urllib.parse

SCHEMA = "fa-initiative-health/v1"
SNAPSHOT_SCHEMA = "fa-initiative-health-snapshot/v1"
IRIS_AUTOMATION = "iris-proactive-work-sweep"
RADAR_AUTOMATION = "ai-radar-daily-research"
DAILY_SCAN_LABEL = "com.anthonyflores.fully-aware.daily-scan"
OUTCOME_SCHEMA = "iris-sweep-outcome/v1"
ATTEMPT_SCHEMA = "iris-sweep-attempt/v1"
DOCKET_SCHEMA = "iris-initiative-docket/v1"
HOLD_SCHEMA = "iris-platform-block-hold/v1"
WORKER_SCHEMA = "clayton-local-receipt/v1"
MISSED_ATTEMPTS = {"missed_before_start", "unknown"}
FAILED_OUTCOMES = {"failed", "error", "refused", "aborted", "cancelled", "blocked"}
POLICY = {"sweep_stale_hours": 26, "worker_window_days": 7}
MAX_JSON = 2 * 1024 * 1024
MAX_WORKER_RUNS = 1000
RUN_NAME = re.compile(r"(\d{8}T\d{6}Z)")
DATE_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")
EXIT = {"operating": 0, "stopped": 1, "degraded": 1, "unknown": 3}
LIMITS = [
    "Read-only observation: nothing is scheduled, resumed, retried, repaired or notified.",
    "Automation presence reflects this Mac's local Codex stores only; server-side state is not read.",
    "A recorded sweep outcome is not proof of accepted work, source freshness or human delivery.",
    "Docket figures are an event-level summary (not IRIS replay).",
    "The daily scan is judged by brief-file presence only; launchd state is not inspected.",
]


def default_config(home=None):
    home = Path(home or Path.home())
    support = home / "Library" / "Application Support" / "IRIS" / "orchestrator"
    return {
        "codex_automations_dir": str(home / ".codex" / "automations"),
        "codex_dev_db": str(home / ".codex" / "sqlite" / "codex-dev.db"),
        "codex_state_db": str(home / ".codex" / "state_5.sqlite"),
        "iris_next_session": str(home / "code" / "iris-ios" / "NEXT_SESSION.json"),
        "sweep_runs_dir": str(support / "sweep-runs"),
        "docket": str(support / "initiative" / "docket.json"),
        "local_worker_dir": str(home / ".local" / "share" / "fully-aware-control" / "state" / "local-worker"),
        "hold_receipts": [str(home / "Documents" / "ChatGPT" / "fully-aware-system" / "outputs"
                              / "20260923-post-merge" / "heartbeat-hold-receipt.json")],
        "daily_scan_dir": str(home / "code" / "fully-aware" / "state" / "daily-scan"),
        "intelligence_dir": str(home / "code" / "fully-aware" / "state" / "intelligence"),
    }


# --------------------------------------------------------------------------- #
# small readers (every failure becomes a reason code, never an exception)
# --------------------------------------------------------------------------- #
def when(value):
    """Aware UTC datetime from an ISO string or an epoch (s or ms); else None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, str) and re.fullmatch(r"\d{9,14}(\.\d+)?", value.strip()):
            value = float(value)
        if isinstance(value, (int, float)):
            seconds = value / 1000.0 if value > 1e11 else float(value)
            return dt.datetime.fromtimestamp(seconds, dt.timezone.utc)
        if isinstance(value, str) and len(value) <= 64:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            return parsed.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def stamp(value):
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def unavailable(reason, **extra):
    return dict({"availability": "unavailable", "reason": reason}, **extra)


def read_bytes(path, limit=MAX_JSON):
    """Bounded regular-file read; returns (bytes, None) or (None, reason)."""
    try:
        target = Path(path)
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            return None, "not_a_regular_file"
        if info.st_size > limit:
            return None, "file_too_large"
        with open(target, "rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            return None, "file_too_large"
        return raw, None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, TypeError, ValueError):
        return None, "unreadable"


def read_json(path, limit=MAX_JSON):
    raw, reason = read_bytes(path, limit)
    if reason:
        return None, None, reason
    try:
        return json.loads(raw.decode("utf-8")), raw, None
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, raw, "malformed_json"


def sqlite_rows(path, table, keep, where=None, params=()):
    """Read-only rows of ``table`` restricted to columns ``keep(name)`` accepts.

    Columns are discovered with PRAGMA table_info so an added, renamed or
    dropped column degrades to a missing field instead of a failure.
    """
    target = Path(path)
    if not target.exists():
        return None, None, "missing"
    try:
        uri = "file:%s?mode=ro" % urllib.parse.quote(str(target))
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            columns = [row[1] for row in con.execute('PRAGMA table_info("%s")' % table)]
            if not columns:
                return None, None, "table_missing"
            chosen = [c for c in columns if keep(c)]
            if "id" not in chosen:
                return None, columns, "schema_drift_no_id"
            sql = "SELECT %s FROM \"%s\"" % (", ".join('"%s"' % c for c in chosen), table)
            if where:
                sql += " WHERE " + where
            rows = [dict(zip(chosen, row)) for row in con.execute(sql + " LIMIT 500", params)]
            return rows, columns, None
        finally:
            con.close()
    except sqlite3.Error:
        return None, None, "unreadable"


def column_like(row, fragment):
    for name, value in row.items():
        if fragment in name.lower():
            return value
    return None


# --------------------------------------------------------------------------- #
# collect
# --------------------------------------------------------------------------- #
TOML_LINE = re.compile(r'^\s*(id|kind|status|rrule|target_thread_id)\s*=\s*"([^"\n]*)"\s*$')


def parse_automation_toml(text):
    """Tiny line parser for the few scalar keys needed; prompts are never read out."""
    found = {}
    for line in text.splitlines():
        match = TOML_LINE.match(line)
        if match and match.group(1) not in found:
            found[match.group(1)] = match.group(2)
    return found


def collect_automation_dir(base):
    base = Path(base)
    if not base.is_dir():
        return unavailable("missing" if not base.exists() else "not_a_directory")
    items = {}
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return unavailable("unreadable")
    for entry in entries[:200]:
        path = entry / "automation.toml"
        if entry.name.startswith(".") or not path.is_file():
            continue
        raw, reason = read_bytes(path, 256 * 1024)
        if reason:
            items[entry.name] = {"readable": False, "reason": reason}
            continue
        facts = parse_automation_toml(raw.decode("utf-8", "replace"))
        items[facts.get("id") or entry.name] = {
            "readable": True, "status": (facts.get("status") or "").upper() or None,
            "kind": facts.get("kind"), "rrule": facts.get("rrule"),
            "target_thread_id": facts.get("target_thread_id")}
    return {"availability": "available", "items": items}


def collect_automation_db(path):
    def keep(name):
        low = name.lower()
        return low in {"id", "status", "kind", "rrule", "target_thread_id"} or "last_run" in low or "next_run" in low
    rows, columns, reason = sqlite_rows(path, "automations", keep)
    if reason:
        return unavailable(reason)
    items = {}
    for row in rows:
        if not isinstance(row.get("id"), str):
            continue
        items[row["id"]] = {
            "status": row["status"].upper() if isinstance(row.get("status"), str) else None,
            "kind": row.get("kind") if isinstance(row.get("kind"), str) else None,
            "rrule": row.get("rrule") if isinstance(row.get("rrule"), str) else None,
            "last_run_at": stamp(when(column_like(row, "last_run"))),
            "next_run_at": stamp(when(column_like(row, "next_run"))),
        }
    return {"availability": "available", "columns": sorted(columns), "items": items}


def outcome_facts(path, now):
    value, _, reason = read_json(path, 1024 * 1024)
    if reason:
        return unavailable(reason)
    if not isinstance(value, dict) or value.get("schema") != OUTCOME_SCHEMA:
        return unavailable("outcome_schema_invalid")
    ended = when(value.get("ended_at"))
    if ended is None:
        return unavailable("outcome_ended_at_invalid")
    if ended > now:
        return unavailable("outcome_ended_at_future")
    status = value.get("status") if isinstance(value.get("status"), str) else None
    return {"availability": "available", "run_id": value.get("run_id") if isinstance(value.get("run_id"), str) else None,
            "ended_at": stamp(ended), "status": status,
            "success": (status or "").lower() not in FAILED_OUTCOMES}


def resolve_pointer(pointer, next_session, sweep_root, filename):
    """Accept {path, sha256}, {outcome: <path|{path}>}, a path string or a bare run id."""
    expected = None
    for _ in range(2):
        if isinstance(pointer, dict):
            expected = pointer.get("sha256") if isinstance(pointer.get("sha256"), str) else expected
            pointer = pointer.get("path") if "path" in pointer else pointer.get("outcome")
    if not isinstance(pointer, str) or not pointer.strip() or len(pointer) > 1024:
        return None, expected
    candidate = Path(pointer).expanduser()
    if candidate.is_absolute():
        return candidate, expected
    if "/" not in pointer and not pointer.endswith(".json"):
        return Path(sweep_root) / pointer / filename, expected
    for base in (Path(sweep_root).parent, Path(next_session).parent):
        if (base / candidate).exists():
            return base / candidate, expected
    return Path(sweep_root).parent / candidate, expected


def collect_sweeps(next_session, sweep_root, now):
    pointer = {"availability": "unavailable", "reason": "not_read"}
    context, _, reason = read_json(next_session, 1024 * 1024)
    if reason:
        pointer = unavailable(reason)
    elif not isinstance(context, dict):
        pointer = unavailable("next_session_not_object")
    else:
        pointer = {"availability": "available"}
        path, _ = resolve_pointer(context.get("latest_sweep"), next_session, sweep_root, "outcome.json")
        pointer["latest_sweep"] = outcome_facts(path, now) if path else unavailable("pointer_missing")
        path, expected = resolve_pointer(context.get("latest_sweep_attempt"), next_session, sweep_root, "attempt.json")
        pointer["latest_attempt"] = attempt_facts(path, expected, now) if path else unavailable("pointer_missing")
    root = Path(sweep_root)
    if not root.is_dir():
        runs = unavailable("missing" if not root.exists() else "not_a_directory")
    else:
        try:
            names = [p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
        except OSError:
            names = None
        if names is None:
            runs = unavailable("unreadable")
        elif not names:
            runs = {"availability": "available", "count": 0, "newest": None}
        else:
            def key(name):
                match = RUN_NAME.match(name)
                return (match.group(1) if match else "", name)
            newest = max(names, key=key)
            match = RUN_NAME.match(newest)
            started = dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=dt.timezone.utc) if match else None
            run_dir = root / newest
            outcome = outcome_facts(run_dir / "outcome.json", now) if (run_dir / "outcome.json").exists() else None
            runs = {"availability": "available", "count": len(names),
                    "newest": {"run_id": newest, "started_at": stamp(started), "outcome": outcome,
                               "late_closure": (run_dir / "late-closure.json").exists()}}
    return {"pointer": pointer, "runs": runs}


def attempt_facts(path, expected, now):
    value, raw, reason = read_json(path, 64 * 1024)
    if reason:
        return unavailable(reason)
    if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
        return unavailable("attempt_hash_mismatch")
    if not isinstance(value, dict) or value.get("schema") != ATTEMPT_SCHEMA:
        return unavailable("attempt_schema_invalid")
    trigger, closed = when(value.get("trigger_at")), when(value.get("closed_at"))
    if trigger is None or closed is None or closed < trigger or closed > now:
        return unavailable("attempt_times_invalid")
    status = value.get("status") if isinstance(value.get("status"), str) else None
    return {"availability": "available", "run_id": value.get("run_id") if isinstance(value.get("run_id"), str) else None,
            "status": status, "trigger_at": stamp(trigger), "closed_at": stamp(closed),
            "automation_id": value.get("automation_id") if isinstance(value.get("automation_id"), str) else None}


def action_key(work_id, action_class):
    # Same derivation as the docket's action_key(); re-implemented, not imported.
    canonical = json.dumps({"work_item_id": work_id, "action_class": action_class}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "initiative-" + hashlib.sha256(canonical).hexdigest()[:32]


def collect_docket(path):
    value, _, reason = read_json(path)
    if reason:
        return unavailable(reason)
    if not isinstance(value, dict) or value.get("schema") != DOCKET_SCHEMA or not isinstance(value.get("events"), list):
        return unavailable("docket_schema_invalid")
    events = []
    for event in value["events"][:4096]:
        if not isinstance(event, dict) or not isinstance(event.get("input"), dict):
            continue
        data, command = event["input"], event.get("command")
        at = when(event.get("at"))
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        work_id = source.get("work_item_id") if isinstance(source.get("work_item_id"), str) else None
        if command == "propose":
            key = action_key(work_id, data.get("action_class")) if work_id else None
        else:
            key = data.get("action_key") if isinstance(data.get("action_key"), str) else None
        if key is None or at is None or not isinstance(command, str):
            continue
        row = {"sequence": event.get("sequence") if type(event.get("sequence")) is int else len(events) + 1,
               "at": stamp(at), "command": command, "action_key": key, "work_item_id": work_id,
               "terminal": source.get("terminal") is True}
        if command == "propose":
            row["next_check_at"] = stamp(when(data.get("next_check_at")))
        if command == "respond":
            row["disposition"] = data.get("disposition") if isinstance(data.get("disposition"), str) else None
        if command == "follow-through":
            row["outcome"] = data.get("outcome") if isinstance(data.get("outcome"), str) else None
        events.append(row)
    stamps = [when(e.get("at")) for e in value["events"] if isinstance(e, dict)]
    stamps = [s for s in stamps if s is not None]
    return {"availability": "available", "event_count": len(value["events"]),
            "last_event_at": stamp(max(stamps)) if stamps else None, "events": events}


def collect_worker(base):
    base = Path(base)
    if not base.is_dir():
        return unavailable("missing" if not base.exists() else "not_a_directory")
    runs = []
    try:
        dirs = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)[-MAX_WORKER_RUNS:]
    except OSError:
        return unavailable("unreadable")
    for run_dir in dirs:
        record, _, reason = read_json(run_dir / "record.json", 1024 * 1024)
        if reason or not isinstance(record, dict) or record.get("schema") != WORKER_SCHEMA:
            continue
        request, _, _ = read_json(run_dir / "request.json", 256 * 1024)
        lane = request.get("lane") if isinstance(request, dict) and isinstance(request.get("lane"), str) else None
        runs.append({"run_id": run_dir.name, "state": record.get("state") if isinstance(record.get("state"), str) else None,
                     "started_at": stamp(when(record.get("started_at"))),
                     "updated_at": stamp(when(record.get("updated_at"))),
                     "lane": lane or (record.get("lane") if isinstance(record.get("lane"), str) else "unknown")})
    return {"availability": "available", "runs": runs}


def collect_thread(db, thread_id):
    if not isinstance(thread_id, str) or not thread_id:
        return unavailable("no_target_thread")
    def keep(name):
        return name.lower() in {"id", "archived", "archived_at"}
    rows, _, reason = sqlite_rows(db, "threads", keep, where="id = ?", params=(thread_id,))
    if reason:
        return unavailable(reason)
    if not rows:
        return {"availability": "available", "exists": False, "archived": None}
    row = rows[0]
    archived = row.get("archived")
    return {"availability": "available", "exists": True,
            "archived": bool(archived) if archived is not None else None,
            "archived_at": stamp(when(row.get("archived_at")))}


def collect_holds(paths, state_db):
    holds = []
    for path in paths or []:
        value, _, reason = read_json(path, 64 * 1024)
        label = Path(path).name
        if reason:
            holds.append(unavailable(reason, receipt=label))
            continue
        if not isinstance(value, dict) or value.get("schema") != HOLD_SCHEMA:
            holds.append(unavailable("hold_schema_invalid", receipt=label))
            continue
        text = value.get("resume_condition")
        holds.append({"availability": "available", "receipt": label,
                      "automation_id": value.get("automation_id"),
                      "target_thread_id": value.get("target_thread_id"),
                      "status": value.get("after_status"),
                      "recorded_at": stamp(when(value.get("recorded_at"))),
                      "resume_condition": text[:300] if isinstance(text, str) else None,
                      "thread": collect_thread(state_db, value.get("target_thread_id"))})
    return holds


def latest_dated_file(base, pattern):
    base = Path(base)
    if not base.is_dir():
        return unavailable("missing" if not base.exists() else "not_a_directory")
    try:
        found = sorted(p.name for p in base.glob(pattern) if p.is_file())
    except OSError:
        return unavailable("unreadable")
    dated = [(m.group(1), name) for name in found for m in [DATE_NAME.search(name)] if m]
    if not dated:
        return {"availability": "available", "latest_date": None, "count": 0}
    date, name = max(dated)
    return {"availability": "available", "latest_date": date, "latest_name": name, "count": len(dated)}


def collect(config, now):
    """Gather every input read-only; absent or unreadable inputs stay explicit."""
    cfg = dict(default_config())
    cfg.update(config or {})
    return {
        "schema": SNAPSHOT_SCHEMA,
        "observed_at": stamp(now),
        "automations": {"config_dir": collect_automation_dir(cfg["codex_automations_dir"]),
                        "scheduler_db": collect_automation_db(cfg["codex_dev_db"])},
        "sweeps": collect_sweeps(cfg["iris_next_session"], cfg["sweep_runs_dir"], now),
        "docket": collect_docket(cfg["docket"]),
        "worker": collect_worker(cfg["local_worker_dir"]),
        "holds": collect_holds(cfg["hold_receipts"], cfg["codex_state_db"]),
        "daily_scan": latest_dated_file(cfg["daily_scan_dir"], "*-brief.md"),
        "intelligence": latest_dated_file(cfg["intelligence_dir"], "*.json"),
    }


# --------------------------------------------------------------------------- #
# assess (pure)
# --------------------------------------------------------------------------- #
def automation_driver(snapshot, automation_id):
    config = snapshot["automations"]["config_dir"]
    db = snapshot["automations"]["scheduler_db"]
    in_db = db.get("items", {}).get(automation_id) if db["availability"] == "available" else None
    in_config = config.get("items", {}).get(automation_id) if config["availability"] == "available" else None
    evidence = ["scheduler_db:" + ("row" if in_db else db.get("reason", "no_row")),
                "config_dir:" + ("file" if in_config else config.get("reason", "no_file"))]
    if db["availability"] != "available":
        # Without the scheduler store, absence cannot be established either way.
        present = True if in_config else None
        presence = "config_only_store_unreadable" if in_config else "unknown"
    else:
        # The scheduler fires only what its store holds: a config file alone is not a driver.
        present = bool(in_db)
        presence = "scheduler_row" if in_db else "present_in_config_only" if in_config else "absent"
    if presence == "present_in_config_only":
        status = "present_in_config_only"
    elif in_db is not None:
        # The scheduler row is the only authority on a driver's status. A NULL or
        # missing status there is unknown; automation.toml never fills the gap.
        status = in_db.get("status") or "unknown_status"
    else:
        status = (in_config or {}).get("status") or ("ABSENT" if present is False else None)
    return {"present": present, "presence": presence, "status": status,
            "last_run_at": (in_db or {}).get("last_run_at"),
            "next_run_at": (in_db or {}).get("next_run_at"), "evidence": evidence}


def sweep_success(snapshot, now):
    """Latest successful outcome and whether a missed attempt followed it."""
    sweeps = snapshot["sweeps"]
    outcomes, evidence = [], []
    pointer = sweeps["pointer"]
    if pointer["availability"] == "available":
        latest = pointer["latest_sweep"]
        evidence.append("latest_sweep:" + (latest.get("reason") or latest.get("run_id") or "outcome"))
        if latest["availability"] == "available" and latest["success"]:
            outcomes.append(when(latest["ended_at"]))
    else:
        evidence.append("next_session:" + pointer["reason"])
    runs = sweeps["runs"]
    newest = runs.get("newest") if runs["availability"] == "available" else None
    if runs["availability"] != "available":
        evidence.append("sweep_runs:" + runs["reason"])
    elif newest:
        evidence.append("newest_run:" + newest["run_id"])
        outcome = newest.get("outcome")
        if outcome and outcome["availability"] == "available" and outcome["success"]:
            outcomes.append(when(outcome["ended_at"]))
    last = max(outcomes) if outcomes else None
    missed = []
    attempt = pointer.get("latest_attempt") if pointer["availability"] == "available" else None
    if attempt and attempt["availability"] == "available" and attempt["status"] in MISSED_ATTEMPTS:
        if last is None or when(attempt["trigger_at"]) > last:
            missed.append({"run_id": attempt["run_id"], "status": attempt["status"], "trigger_at": attempt["trigger_at"]})
    if newest and newest.get("started_at") and not newest.get("outcome"):
        started = when(newest["started_at"])
        if (last is None or started > last) and not any(m["run_id"] == newest["run_id"] for m in missed):
            missed.append({"run_id": newest["run_id"], "status": "no_outcome_recorded", "trigger_at": newest["started_at"]})
    known = pointer["availability"] == "available" or runs["availability"] == "available"
    return last, missed, evidence, known


def docket_summary(docket, now):
    if docket["availability"] != "available":
        return {"availability": "unavailable", "reason": docket["reason"],
                "label": "event-level summary (not IRIS replay)"}
    keys = {}
    for event in sorted(docket["events"], key=lambda e: e["sequence"]):
        slot = keys.setdefault(event["action_key"], {"work_item_id": None, "latest": {}, "next_check_at": None,
                                                     "answered_at": None, "answered_seq": None, "terminal_seq": None,
                                                     "resolved_seq": 0})
        slot["latest"][event["command"]] = event["sequence"]
        if event.get("work_item_id"):
            slot["work_item_id"] = event["work_item_id"]
        if event["terminal"]:
            slot["terminal_seq"] = event["sequence"]
        if event["command"] == "propose":
            slot["next_check_at"] = event.get("next_check_at")
            slot["proposed_at"] = event["at"]
        if event["command"] == "respond" and event.get("disposition") in {"answered", "declined"}:
            slot["resolved_seq"] = event["sequence"]
        if event["command"] == "respond" and event.get("disposition") == "answered":
            slot["answered_at"], slot["answered_seq"] = event["at"], event["sequence"]
    gaps, overdue, unpresented, open_requests = [], 0, [], 0
    for key, slot in sorted(keys.items()):
        latest = slot["latest"]
        proposed = latest.get("propose")
        if proposed is None:
            continue
        after = [latest.get(c, 0) for c in ("present", "respond")]
        if max(after) < proposed and (slot["terminal_seq"] or 0) < proposed:
            unpresented.append(key)
        if max(slot["resolved_seq"], slot["terminal_seq"] or 0) < proposed:
            open_requests += 1
        if slot["answered_seq"] and latest.get("follow-through", 0) < slot["answered_seq"]:
            answered = when(slot["answered_at"])
            recheck = when(slot["next_check_at"])
            is_overdue = recheck is not None and recheck < now
            overdue += 1 if is_overdue else 0
            gaps.append({"action_key": key, "work_item_id": slot["work_item_id"], "answered_at": slot["answered_at"],
                         "age_hours": round((now - answered).total_seconds() / 3600, 1),
                         "next_check_at": slot["next_check_at"], "recheck_overdue": is_overdue})
    return {"availability": "available", "label": "event-level summary (not IRIS replay)",
            "event_count": docket["event_count"], "last_event_at": docket["last_event_at"],
            "follow_through_gaps": gaps, "overdue_rechecks": overdue, "open_requests": open_requests,
            "unpresented_proposals": len(unpresented)}


def worker_summary(worker, now, policy):
    if worker["availability"] != "available":
        return {"availability": "unavailable", "reason": worker["reason"]}
    window = now - dt.timedelta(days=policy["worker_window_days"])
    times = [when(r["updated_at"] or r["started_at"]) for r in worker["runs"]]
    newest = max((t for t in times if t), default=None)
    recent = [r for r in worker["runs"] if when(r["started_at"]) and when(r["started_at"]) >= window]
    lanes = {}
    for run in recent:
        lanes[run["lane"]] = lanes.get(run["lane"], 0) + 1
    share = {lane: round(count / len(recent), 2) for lane, count in sorted(lanes.items())} if recent else {}
    return {"availability": "available", "newest_run_at": stamp(newest), "runs_7d": len(recent),
            "accepted_7d": sum(1 for r in recent if r["state"] == "accepted"),
            "lane_counts_7d": dict(sorted(lanes.items())), "lane_share_7d": share}


def hold_summary(holds, iris_present):
    declared = [h for h in holds if h["availability"] == "available"]
    if not declared:
        reasons = sorted({h["reason"] for h in holds})
        return {"declared": False, "reason": reasons[0] if reasons else "no_hold_receipt_configured"}
    hold = max(declared, key=lambda h: h.get("recorded_at") or "")
    thread = hold["thread"]
    satisfiable, reason = None, "thread_state_unavailable"
    if thread["availability"] == "available":
        if thread["exists"] and thread["archived"] is False:
            satisfiable, reason = True, "hold_target_current"
        elif thread["exists"] and thread["archived"]:
            satisfiable, reason = False, "hold_target_archived"
        elif not thread["exists"]:
            satisfiable, reason = False, "hold_target_missing"
    reasons = [reason]
    if hold.get("automation_id") == IRIS_AUTOMATION and iris_present is False:
        # A hold pauses an automation; there is nothing left to resume.
        satisfiable = False
        reasons.append("hold_automation_absent")
    return {"declared": True, "automation_id": hold.get("automation_id"), "status": hold.get("status"),
            "recorded_at": hold.get("recorded_at"), "target_thread_id": hold.get("target_thread_id"),
            "target_thread": ("unknown" if thread["availability"] != "available" else
                              "missing" if not thread["exists"] else
                              "archived" if thread["archived"] else "current"),
            "resume_condition": hold.get("resume_condition"),
            "resume_condition_satisfiable": satisfiable, "reason": reason, "reasons": reasons,
            "holds_declared": len(declared)}


def assess(snapshot, now, policy=None):
    """Pure judgment over a snapshot; deterministic given its inputs."""
    policy = dict(POLICY, **(policy or {}))
    stale_after = dt.timedelta(hours=policy["sweep_stale_hours"])
    iris = automation_driver(snapshot, IRIS_AUTOMATION)
    radar = automation_driver(snapshot, RADAR_AUTOMATION)
    last_success, missed, sweep_evidence, sweep_known = sweep_success(snapshot, now)
    recent = last_success is not None and now - last_success <= stale_after
    iris_row = {"driver": "iris_sweep_heartbeat", "automation_id": IRIS_AUTOMATION, "present": iris["present"],
                "presence": iris["presence"], "status": iris["status"], "last_success_at": stamp(last_success),
                "next_run_at": iris["next_run_at"], "evidence": iris["evidence"] + sweep_evidence}
    scan = snapshot["daily_scan"]
    intel = snapshot["intelligence"]
    drivers = [
        iris_row,
        {"driver": "fa_daily_scan", "launchd_label": DAILY_SCAN_LABEL,
         "present": bool(scan.get("latest_date")) if scan["availability"] == "available" else None,
         "status": ("brief_observed" if scan.get("latest_date") else "no_brief") if scan["availability"] == "available"
         else "unavailable", "last_success_at": scan.get("latest_date"), "next_run_at": None,
         "evidence": ["brief_file_presence_only", "daily_scan_dir:" + scan.get("reason", "read")]},
        {"driver": "radar_daily", "automation_id": RADAR_AUTOMATION, "present": radar["present"],
         "presence": radar["presence"], "status": radar["status"], "last_success_at": radar["last_run_at"], "next_run_at": radar["next_run_at"],
         "evidence": radar["evidence"] + ["last_run_is_not_success_proof"]},
        {"driver": "intelligence_pass",
         "present": bool(intel.get("latest_date")) if intel["availability"] == "available" else False,
         "status": ("receipt_observed" if intel.get("latest_date") else "no_receipts") if intel["availability"] == "available"
         else "not_installed", "last_success_at": intel.get("latest_date"), "next_run_at": None,
         "evidence": ["receipt_dir:" + intel.get("reason", "read")]},
    ]
    hold = hold_summary(snapshot["holds"], iris["present"])
    db_available = snapshot["automations"]["scheduler_db"]["availability"] == "available"
    if not db_available or not sweep_known:
        state = "unknown"
        reason = "scheduler_store_unavailable" if not db_available else "sweep_evidence_unavailable"
    elif iris["present"] and iris["status"] == "ACTIVE" and recent and not missed:
        state, reason = "operating", "driver_active_and_recent_success"
    elif iris["presence"] == "present_in_config_only":
        state, reason = "degraded", "scheduler_row_missing"
    elif not iris["present"] and not recent:
        state, reason = "stopped", "no_iris_sweep_driver_and_no_recent_success"
    elif not iris["present"]:
        state, reason = "degraded", "no_iris_sweep_driver_but_recent_success"
    elif iris["status"] == "unknown_status":
        state, reason = "degraded", "driver_status_unknown"
    elif iris["status"] != "ACTIVE":
        state, reason = "degraded", "driver_not_active"
    elif not recent:
        state, reason = "degraded", "last_success_stale"
    else:
        state, reason = "degraded", "missed_attempts"
    if last_success is None:
        since = None  # rendered as "since unknown (no successful sweep observed)"
    else:
        since = snapshot["observed_at"] if state == "unknown" else stamp(last_success)
    next_run = when(iris["next_run_at"])
    # Only an ACTIVE driver with a scheduler row can make idling legitimate; a paused
    # driver's next_run_at is not a wake.
    if (state in {"operating", "degraded"} and iris["present"] and iris["status"] == "ACTIVE"
            and next_run and next_run > now):
        idle = {"legitimate": True, "reason": "driver_scheduled",
                "next_wake": {"kind": "iris_sweep_heartbeat", "at": stamp(next_run)}}
    elif state != "unknown" and hold.get("declared") and hold.get("resume_condition_satisfiable") is True:
        idle = {"legitimate": True, "reason": "declared_hold_resumable",
                "next_wake": {"kind": "hold_resume", "at": None}}
    else:
        idle = {"legitimate": False,
                "reason": {"stopped": "no_driver_can_wake_iris", "unknown": "inputs_unavailable"}.get(
                    state, "no_future_run_known"),
                "next_wake": {"kind": "none", "at": None}}
    decision = None
    if reason == "scheduler_row_missing":
        decision = ("The IRIS sweep automation exists only as a config file; the scheduler has no row for it, "
                    "so nothing will fire it. Anthony must decide how IRIS gets a heartbeat.")
    if state == "stopped":
        decision = "No scheduler can wake IRIS; Anthony must decide how IRIS gets a heartbeat."
        if hold.get("declared") and hold.get("resume_condition_satisfiable") is False:
            decision += " The declared hold cannot resume as written (%s)." % ", ".join(hold["reasons"])
    docket = docket_summary(snapshot["docket"], now)
    return {
        "schema": SCHEMA, "observed_at": snapshot["observed_at"], "state": state, "reason": reason,
        "since": since, "policy": policy, "drivers": drivers, "missed_attempts": missed, "hold": hold,
        "follow_through_gaps": docket.get("follow_through_gaps", []),
        "overdue_rechecks": docket.get("overdue_rechecks"), "open_requests": docket.get("open_requests"),
        "docket": {k: v for k, v in docket.items() if k != "follow_through_gaps"},
        "worker": worker_summary(snapshot["worker"], now, policy),
        "idle": idle, "required_decision": decision, "limits": list(LIMITS),
    }


# --------------------------------------------------------------------------- #
# render + CLI
# --------------------------------------------------------------------------- #
PLAIN = {
    "driver_active_and_recent_success": "the IRIS sweep driver is active and produced a recent outcome",
    "no_iris_sweep_driver_and_no_recent_success": "no IRIS sweep driver exists and no sweep succeeded recently",
    "no_iris_sweep_driver_but_recent_success": "no IRIS sweep driver exists, though a sweep succeeded recently",
    "driver_not_active": "the IRIS sweep driver exists but is not active",
    "driver_status_unknown": "the IRIS sweep driver's scheduler row has no status",
    "last_success_stale": "the IRIS sweep driver exists but its last success is stale",
    "missed_attempts": "the IRIS sweep driver missed its latest attempt",
    "scheduler_row_missing": "an IRIS sweep automation file exists but the scheduler store has no row for it",
    "scheduler_store_unavailable": "the Codex scheduler store could not be read",
    "sweep_evidence_unavailable": "no IRIS sweep evidence could be read",
}


def render_markdown(report):
    def cell(value):
        return "-" if value is None else str(value).replace("|", "/")
    since = report["since"] or "unknown (no successful sweep observed)"
    lines = ["Initiative: %s since %s — %s" % (report["state"].upper(), since,
                                               PLAIN.get(report["reason"], report["reason"]))]
    wake = report["idle"]["next_wake"]
    lines.append("Next wake: %s%s (idle %s: %s)" % (
        wake["kind"], " at " + wake["at"] if wake["at"] else "",
        "legitimate" if report["idle"]["legitimate"] else "NOT legitimate", report["idle"]["reason"]))
    lines.append("Required decision: %s" % (report["required_decision"] or "none"))
    hold = report["hold"]
    if hold.get("declared"):
        lines.append("Hold: %s %s since %s; target thread %s; resume condition satisfiable: %s (%s)" % (
            cell(hold["automation_id"]), cell(hold["status"]), cell(hold["recorded_at"]), hold["target_thread"],
            {True: "yes", False: "no", None: "unknown"}[hold["resume_condition_satisfiable"]],
            ", ".join(hold["reasons"])))
    else:
        lines.append("Hold: none declared (%s)" % hold["reason"])
    for miss in report["missed_attempts"][:2]:
        lines.append("Missed attempt: %s %s at %s" % (miss["run_id"], miss["status"], miss["trigger_at"]))
    docket = report["docket"]
    if docket["availability"] == "available":
        gaps = report["follow_through_gaps"]
        detail = "; ".join("%s answered %s, %.0fh ago%s" % (
            g["work_item_id"] or g["action_key"], g["answered_at"], g["age_hours"],
            ", recheck overdue" if g["recheck_overdue"] else "") for g in gaps[:3])
        lines.append("Follow-through gaps: %d%s" % (len(gaps), (" — " + detail) if detail else ""))
        lines.append("Docket (%s): %d events, last %s; open requests %d; overdue rechecks %d" % (
            docket["label"], docket["event_count"], cell(docket["last_event_at"]),
            report["open_requests"], report["overdue_rechecks"]))
    else:
        lines.append("Follow-through gaps: unknown (docket unavailable: %s)" % docket["reason"])
    worker = report["worker"]
    if worker["availability"] == "available":
        lanes = ", ".join("%s %d" % item for item in worker["lane_counts_7d"].items()) or "none"
        lines.append("Local worker: newest run %s; 7-day runs %d, accepted %d; lanes: %s" % (
            cell(worker["newest_run_at"]), worker["runs_7d"], worker["accepted_7d"], lanes))
    else:
        lines.append("Local worker: unavailable (%s)" % worker["reason"])
    lines.extend(["", "| driver | present | status | last success | next run |", "|---|---|---|---|---|"])
    for row in report["drivers"]:
        present = {True: "yes", False: "no", None: "unknown"}[row["present"]]
        lines.append("| %s | %s | %s | %s | %s |" % (row["driver"], present, cell(row["status"]),
                                                     cell(row["last_success_at"]), cell(row["next_run_at"])))
    lines.append("Limits: read-only; local stores only; outcomes are not proof of accepted work.")
    return "\n".join(lines) + "\n"


def load_config(path, overrides):
    config = {}
    if path:
        with open(path, "r", encoding="utf-8") as stream:
            loaded = json.load(stream)
        if not isinstance(loaded, dict):
            raise ValueError("config_not_object")
        config.update(loaded)
    for item in overrides or []:
        key, sep, value = item.partition("=")
        if not sep or key not in default_config():
            raise ValueError("invalid_override")
        config[key] = value.split(os.pathsep) if key == "hold_receipts" else value
    return config


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="print the report as JSON (default)")
    fmt.add_argument("--markdown", action="store_true", help="print a short plain-English summary")
    p.add_argument("--check", action="store_true",
                   help="exit 0 operating, 1 stopped or degraded, 3 unknown, 2 hard error")
    p.add_argument("--config", help="JSON file overriding default input paths")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="override one input path")
    p.add_argument("--now", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    try:
        now = when(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
        if now is None:
            raise ValueError("invalid_now")
        report = assess(collect(load_config(a.config, a.set), now), now)
        if a.markdown:
            sys.stdout.write(render_markdown(report))
        else:
            print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        return EXIT[report["state"]] if a.check else 0
    except Exception as exc:  # noqa: BLE001 -- any failure is a hard error, never "operating"
        print(json.dumps({"schema": SCHEMA, "state": "error", "reason": type(exc).__name__ + ": " + str(exc)[:200]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
