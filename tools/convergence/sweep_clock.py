#!/usr/bin/env python3
"""Immutable clocks and time-only admission for the existing IRIS sweep.

Does not execute commands, grant authority, schedule work, or certify completion.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import stat
import subprocess
import time
import uuid

SCHEMA = 'iris-sweep-clock/v1'
TOTAL = 900
PHASES = {'collection', 'preparation', 'dispatch', 'feed_publish', 'priority', 'finalization'}
CAPTURES = {'notion', 'slack'}


def instant(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('aware_timestamp_required')
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError('aware_timestamp_required') from None


def number(value, *, positive=False):
    if type(value) not in (int, float):
        raise ValueError('finite_duration_required')
    try:
        valid = math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        valid = False
    if not valid or value > 10**18:
        raise ValueError('finite_duration_required')
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}', value):
        raise ValueError('invalid_identity')
    return value


def sample():
    if platform.system() == 'Darwin':
        boot = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'],
                              capture_output=True, text=True, check=True, timeout=3).stdout.strip()
    elif platform.system() == 'Linux':
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    else:
        raise ValueError('boot_identity_unavailable')
    return {'utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'monotonic_ns': time.monotonic_ns(), 'boot_id': identifier(boot),
            'host': identifier(socket.gethostname())}


def validate_sample(value):
    if not isinstance(value, dict) or set(value) != {'utc', 'monotonic_ns', 'boot_id', 'host'}:
        raise ValueError('invalid_clock_sample')
    instant(value['utc'])
    if type(value['monotonic_ns']) is not int or not 0 <= value['monotonic_ns'] <= 10**20:
        raise ValueError('invalid_monotonic_sample')
    identifier(value['boot_id']); identifier(value['host'])


def elapsed_since(original, current):
    validate_sample(original); validate_sample(current)
    if original['host'] != current['host'] or original['boot_id'] != current['boot_id']:
        raise ValueError('clock_continuity_unknown')
    mono = (current['monotonic_ns'] - original['monotonic_ns']) / 1e9
    wall = (instant(current['utc']) - instant(original['utc'])).total_seconds()
    if mono < 0 or wall < 0 or wall < mono - 2:
        raise ValueError('clock_regression')
    # Wall time includes suspension; monotonic protects against a small wall rollback.
    return max(mono, wall)


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def directory(path):
    p = Path(path)
    if not p.is_absolute() or p.resolve() != p:
        raise ValueError('physical_absolute_run_directory_required')
    st = p.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ValueError('private_owned_run_directory_required')
    identifier(p.name)
    return p


def read(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('physical_absolute_file_required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        a = os.fstat(fd)
        if not stat.S_ISREG(a.st_mode) or a.st_uid != os.getuid() or a.st_mode & 0o077 or a.st_size > 16384:
            raise ValueError('private_bounded_regular_file_required')
        raw = os.read(fd, 16385)
        b = os.fstat(fd)
        if len(raw) != a.st_size or (a.st_mtime_ns, a.st_ctime_ns) != (b.st_mtime_ns, b.st_ctime_ns):
            raise ValueError('file_changed')
    finally:
        os.close(fd)
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out: raise ValueError('duplicate_key')
            out[key] = value
        return out
    def invalid(_): raise ValueError('nonfinite_json')
    return raw, json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def publish(path, data):
    tmp = path.parent / ('.clock-' + uuid.uuid4().hex)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.link(tmp, path, follow_symlinks=False)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


def initialize(run_dir, trigger_at, trigger_ref, owner, current=None):
    run = directory(run_dir); current = current or sample(); validate_sample(current)
    if instant(trigger_at) > instant(current['utc']): raise ValueError('future_trigger')
    value = {'schema': SCHEMA, 'run_id': run.name, 'owner_task_id': identifier(owner),
             'trigger_at': trigger_at, 'trigger_ref': identifier(trigger_ref), 'initialized': current}
    raw = encode(value)
    publish(run / 'sweep-clock.json', raw)
    return {'clock_sha256': sha(raw), 'clock': value, 'authority': 'none'}


def load_clock(run_dir, expected):
    run = directory(run_dir)
    raw, value = read(run / 'sweep-clock.json')
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected) or sha(raw) != expected:
        raise ValueError('clock_hash_mismatch')
    required = {'schema', 'run_id', 'owner_task_id', 'trigger_at', 'trigger_ref', 'initialized'}
    if not isinstance(value, dict) or set(value) != required or value['schema'] != SCHEMA or value['run_id'] != run.name:
        raise ValueError('invalid_clock')
    identifier(value['owner_task_id']); identifier(value['trigger_ref']); validate_sample(value['initialized'])
    if instant(value['trigger_at']) > instant(value['initialized']['utc']): raise ValueError('future_trigger')
    return run, value


def status(clock, current):
    consumed = elapsed_since(clock['initialized'], current)
    initial_delay = (instant(clock['initialized']['utc']) - instant(clock['trigger_at'])).total_seconds()
    if initial_delay < 0: raise ValueError('future_trigger')
    elapsed = initial_delay + consumed
    return {'run_id': clock['run_id'], 'owner_task_id': clock['owner_task_id'],
            'trigger_at': clock['trigger_at'], 'checked_at': current['utc'],
            'initial_delay_seconds': initial_delay, 'elapsed_seconds': elapsed,
            'remaining_seconds': max(0, TOTAL - elapsed),
            'timing': 'expired' if elapsed >= TOTAL else 'within_window',
            'authority': 'none', 'completion': 'unverified'}


def begin_capture(run_dir, expected, name, current=None):
    if name not in CAPTURES: raise ValueError('invalid_capture')
    run, clock = load_clock(run_dir, expected); current = current or sample()
    state = status(clock, current)
    if state['elapsed_seconds'] >= 600: raise ValueError('optional_admission_closed')
    value = {'schema': 'iris-sweep-capture-clock/v1', 'name': name, 'run_id': run.name,
             'clock_sha256': expected, 'started': current}
    raw = encode(value); publish(run / ('capture-clock-' + name + '.json'), raw)
    return {'capture_sha256': sha(raw), 'capture': value, 'authority': 'none'}


def admission(clock, current, phase, maximum, reserve, *, capture=None, outcome_verified=False):
    if phase not in PHASES: raise ValueError('invalid_phase')
    maximum = number(maximum, positive=True); reserve = number(reserve)
    result = status(clock, current); elapsed = result['elapsed_seconds']
    cutoff, minimum_reserve = (600, 300)
    if phase == 'feed_publish': cutoff, minimum_reserve = 420, 480
    if phase == 'priority': cutoff, minimum_reserve = 780, 120
    if phase == 'finalization': cutoff, minimum_reserve = 850, 50
    reasons = []
    if elapsed >= TOTAL: reasons.append('total_budget_expired')
    if elapsed > cutoff or (phase in {'collection', 'preparation', 'dispatch'} and elapsed >= cutoff):
        reasons.append('phase_admission_closed')
    if reserve < minimum_reserve: reasons.append('closure_reserve_too_small')
    if elapsed + maximum + reserve > TOTAL: reasons.append('call_and_closure_do_not_fit')
    if elapsed + maximum > 850: reasons.append('tool_work_target_exceeded')
    if phase == 'priority' and not outcome_verified: reasons.append('immutable_outcome_required')
    if capture is not None:
        if phase != 'collection': raise ValueError('capture_only_for_collection')
        # Both samples must follow the same run clock, including monotonic order.
        elapsed_since(clock['initialized'], capture['started'])
        cap_elapsed = elapsed_since(capture['started'], current)
        if cap_elapsed + maximum > 300: reasons.append('capture_budget_exceeded')
        result['capture_elapsed_seconds'] = cap_elapsed
    result.update({'schema': 'iris-sweep-time-admission/v1', 'phase': phase, 'admitted': not reasons,
                   'maximum_call_seconds': maximum, 'closure_reserve_seconds': reserve,
                   'phase_cutoff_seconds': cutoff, 'reasons': reasons,
                   'limits': 'Time-only check; caller must enforce its declared bound and all existing permissions; recheck immediately before every operation.'})
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir', required=True)
    subs = p.add_subparsers(dest='command', required=True)
    init = subs.add_parser('init')
    for name in ('trigger-at', 'trigger-ref', 'owner-task-id'): init.add_argument('--' + name, required=True)
    for name in ('status', 'begin-capture', 'check'):
        sub = subs.add_parser(name); sub.add_argument('--clock-sha256', required=True)
        if name == 'begin-capture': sub.add_argument('--capture', required=True, choices=sorted(CAPTURES))
        if name == 'check':
            sub.add_argument('--phase', required=True, choices=sorted(PHASES))
            sub.add_argument('--maximum-call-seconds', type=float, required=True)
            sub.add_argument('--closure-reserve-seconds', type=float, required=True)
            sub.add_argument('--capture', choices=sorted(CAPTURES))
            sub.add_argument('--capture-sha256')
            sub.add_argument('--outcome-sha256')
    a = p.parse_args()
    try:
        if a.command == 'init':
            result = initialize(a.run_dir, a.trigger_at, a.trigger_ref, a.owner_task_id)
        elif a.command == 'begin-capture':
            result = begin_capture(a.run_dir, a.clock_sha256, a.capture)
        else:
            run, clock = load_clock(a.run_dir, a.clock_sha256); current = sample()
            if a.command == 'status': result = status(clock, current)
            else:
                capture = None
                if bool(a.capture) != bool(a.capture_sha256): raise ValueError('capture_and_hash_required_together')
                if a.capture:
                    raw, capture = read(run / ('capture-clock-' + a.capture + '.json'))
                    if sha(raw) != a.capture_sha256 or capture != {
                        'schema': 'iris-sweep-capture-clock/v1', 'name': a.capture, 'run_id': run.name,
                        'clock_sha256': a.clock_sha256, 'started': capture.get('started')}:
                        raise ValueError('capture_binding_mismatch')
                outcome_verified = False
                if a.outcome_sha256:
                    raw, outcome = read(run / 'outcome.json')
                    if (sha(raw) != a.outcome_sha256 or not isinstance(outcome, dict)
                            or outcome.get('schema') != 'iris-sweep-outcome/v1' or outcome.get('run_id') != run.name
                            or not instant(clock['trigger_at']) <= instant(outcome.get('ended_at')) <= instant(current['utc'])):
                        raise ValueError('outcome_binding_mismatch')
                    outcome_verified = True
                result = admission(clock, current, a.phase, a.maximum_call_seconds, a.closure_reserve_seconds,
                                   capture=capture, outcome_verified=outcome_verified)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0 if result.get('admitted', True) else 3
    except (ValueError, OSError, KeyError, TypeError, AttributeError, OverflowError, subprocess.SubprocessError) as exc:
        print(json.dumps({'schema': 'iris-sweep-time-admission/v1', 'admitted': False,
                          'reason': type(exc).__name__ + ': ' + str(exc)[:200], 'authority': 'none'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
