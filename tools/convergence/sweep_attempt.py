#!/usr/bin/env python3
"""Bind the latest sweep attempt without replacing the last useful source outcome."""
import argparse
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo
try:
    from . import sweep_clock as clock
except ImportError:
    import sweep_clock as clock

AUTOMATION = 'iris-proactive-work-sweep'
OWNER = '01a0cf00-be7a-7263-ac37-4d175f09b546'
LEGACY_OWNER = '01a08366-bd65-72a3-b7a8-ae0e5ab5bb20'
LEGACY_RETIRED_AT = dt.datetime(2026, 9, 23, 16, 8, 12, tzinfo=dt.timezone.utc)
ROOT = Path('/Users/anthonyflores/Library/Application Support/IRIS/orchestrator/sweep-runs')
SCHEMA = 'iris-sweep-attempt/v1'
KEYS = {'schema', 'automation_id', 'owner_thread_id', 'run_id', 'trigger_at', 'intended_slot',
        'closed_at', 'recorded_at', 'status', 'receipt'}


def receipt_facts(receipt, run_id, *, owner=OWNER):
    if not isinstance(receipt, dict): raise ValueError('invalid_receipt')
    trigger = receipt.get('trigger_at')
    if receipt.get('schema') == 'iris-sweep-outcome/v1':
        if receipt.get('run_id') != run_id: raise ValueError('receipt_run_mismatch')
        closed = receipt.get('ended_at'); status = 'outcome_recorded'
        if not clock.instant(trigger) <= clock.instant(receipt.get('started_at')) <= clock.instant(closed):
            raise ValueError('receipt_time_order')
    elif receipt.get('schema') == 'iris-sweep-late-closure/v1':
        if receipt.get('automation_id') != AUTOMATION: raise ValueError('receipt_automation_mismatch')
        if receipt.get('owner') != 'IRIS existing task ' + owner:
            raise ValueError('receipt_owner_mismatch')
        closed = receipt.get('late_closure_at'); status = 'unknown'
        if (receipt.get('original_start_monotonic') == 'not_recorded'
                and isinstance(receipt.get('work_admission'), str)
                and receipt['work_admission'].startswith('refused;')
                and (clock.instant(closed) - clock.instant(trigger)).total_seconds() >= 900):
            status = 'missed_before_start'
    else:
        raise ValueError('unsupported_receipt_schema')
    if clock.instant(closed) < clock.instant(trigger): raise ValueError('receipt_time_order')
    return trigger, closed, status


def validate(value, envelope_path, *, root=ROOT, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(value, dict) or set(value) != KEYS or value.get('schema') != SCHEMA:
        raise ValueError('invalid_attempt_envelope')
    path = Path(envelope_path); root = Path(root)
    if path.name != 'attempt.json' or not path.is_absolute() or path.resolve() != path or path.parent.parent != root.resolve():
        raise ValueError('attempt_outside_run_root')
    clock.directory(path.parent)
    owner = value['owner_thread_id']
    if (value['run_id'] != path.parent.name or value['automation_id'] != AUTOMATION
            or owner not in (OWNER, LEGACY_OWNER)):
        raise ValueError('attempt_identity_mismatch')
    slot = value['intended_slot']
    if (not isinstance(slot, dict) or set(slot) != {'local_date', 'hour', 'timezone'}
            or slot['timezone'] != 'America/Los_Angeles' or type(slot['hour']) is not int
            or slot['hour'] not in (9, 13, 17)):
        raise ValueError('invalid_schedule_slot')
    day = dt.date.fromisoformat(slot['local_date'])
    if day.isoformat() != slot['local_date']: raise ValueError('invalid_schedule_date')
    scheduled = dt.datetime.combine(day, dt.time(slot['hour']), ZoneInfo(slot['timezone']))
    trigger = clock.instant(value['trigger_at']); closed = clock.instant(value['closed_at'])
    # Retained attempts keep their original owner; retirement never rewrites history.
    if owner == LEGACY_OWNER and trigger >= LEGACY_RETIRED_AT:
        raise ValueError('retired_attempt_owner')
    recorded = clock.instant(value['recorded_at'])
    if not 0 <= (trigger - scheduled).total_seconds() <= 900:
        raise ValueError('trigger_outside_declared_slot')
    if not trigger <= closed <= recorded <= now: raise ValueError('attempt_time_order')
    ref = value['receipt']
    if not isinstance(ref, dict) or set(ref) != {'path', 'sha256'}: raise ValueError('invalid_receipt_reference')
    target = Path(ref['path'])
    if target.parent != path.parent or target.name not in {'outcome.json', 'late-closure.json'}:
        raise ValueError('receipt_outside_same_run')
    raw, receipt = clock.read(target)
    if clock.sha(raw) != ref['sha256']: raise ValueError('receipt_hash_mismatch')
    expected_schema = 'iris-sweep-outcome/v1' if target.name == 'outcome.json' else 'iris-sweep-late-closure/v1'
    if receipt.get('schema') != expected_schema: raise ValueError('receipt_filename_schema_mismatch')
    r_trigger, r_closed, status = receipt_facts(receipt, value['run_id'], owner=owner)
    if value['trigger_at'] != r_trigger or value['closed_at'] != r_closed or value['status'] != status:
        raise ValueError('receipt_facts_mismatch')
    return value


def create(run_dir, receipt_path, receipt_sha256, local_date, hour, *, root=ROOT, now=None):
    run = clock.directory(run_dir); now = now or dt.datetime.now(dt.timezone.utc)
    raw, receipt = clock.read(receipt_path)
    if clock.sha(raw) != receipt_sha256: raise ValueError('receipt_hash_mismatch')
    trigger, closed, status = receipt_facts(receipt, run.name)
    value = {'schema': SCHEMA, 'automation_id': AUTOMATION, 'owner_thread_id': OWNER,
             'run_id': run.name, 'trigger_at': trigger,
             'intended_slot': {'local_date': local_date, 'hour': hour, 'timezone': 'America/Los_Angeles'},
             'closed_at': closed, 'recorded_at': now.isoformat(), 'status': status,
             'receipt': {'path': str(receipt_path), 'sha256': receipt_sha256}}
    target = run / 'attempt.json'; validate(value, target, root=root, now=now)
    raw = clock.encode(value); clock.publish(target, raw)
    return {'path': str(target), 'sha256': clock.sha(raw)}


def read_attempt(path, expected, *, root=ROOT, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if path is None and expected is None:
        return {'availability': 'unavailable', 'reason': 'not_configured', 'authority': 'none'}
    try:
        raw, value = clock.read(path)
        if clock.sha(raw) != expected: raise ValueError('attempt_hash_mismatch')
        validate(value, path, root=root, now=now)
        return {'availability': 'available', 'sha256': clock.sha(raw), 'run_id': value['run_id'],
                'status': value['status'], 'trigger_at': value['trigger_at'], 'closed_at': value['closed_at'],
                'recorded_at': value['recorded_at'], 'intended_slot': value['intended_slot'],
                'trigger_to_close_seconds': (clock.instant(value['closed_at']) - clock.instant(value['trigger_at'])).total_seconds(),
                'age_seconds': int((now - clock.instant(value['closed_at'])).total_seconds()),
                'authority': 'none', 'source_freshness': 'not_established', 'human_delivery': 'unverified'}
    except (ValueError, OSError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return {'availability': 'unavailable', 'reason': 'attempt_validation_failed', 'authority': 'none'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir', required=True); p.add_argument('--receipt', required=True)
    p.add_argument('--receipt-sha256', required=True); p.add_argument('--local-date', required=True)
    p.add_argument('--hour', type=int, choices=(9, 13, 17), required=True)
    a = p.parse_args()
    try:
        result = create(a.run_dir, a.receipt, a.receipt_sha256, a.local_date, a.hour)
        print(json.dumps(result)); return 0
    except (ValueError, OSError, TypeError, KeyError, AttributeError, OverflowError):
        print(json.dumps({'status': 'refused', 'reason': 'attempt_validation_or_publication_failed'})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
