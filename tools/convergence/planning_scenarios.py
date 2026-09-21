"""Local, read-only dependency and capacity scenarios over owner-reviewed inputs.

This is a projection, not a task authority, calendar scheduler or remote endpoint.
IDs/revisions are supplied by the existing owner; no identity is inferred from text.
Capacity is an explicit assumption, not proof of a free calendar window.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import re

try:
    from . import planning_receipts as receipts
except ImportError:
    import planning_receipts as receipts

SCHEMA = 'planning-scenarios-input/v1'
ID = re.compile(r'^[A-Za-z][A-Za-z0-9_.:-]{0,127}$')
SHA = re.compile(r'^[a-f0-9]{64}$')
MAX_ITEMS = 100
MAX_MINUTES = 60 * 24 * 42


def require(condition, code):
    if not condition:
        raise ValueError(code)


def keys(value, expected):
    require(type(value) is dict and set(value) == set(expected.split()), 'invalid_fields')


def text(value, maximum=1000):
    require(type(value) is str and 0 < len(value.strip()) <= maximum
            and not any(ord(c) < 32 for c in value), 'invalid_text')
    return value


def ident(value):
    require(type(value) is str and ID.fullmatch(value), 'invalid_id')
    return value


def sha(value):
    require(type(value) is str and SHA.fullmatch(value), 'invalid_revision')
    return value


def instant(value):
    require(type(value) is str and len(value) <= 64, 'invalid_time')
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(result.tzinfo is not None and result.utcoffset() is not None, 'invalid_time')
        return result.astimezone(dt.timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('invalid_time') from None


def minutes(value):
    require(type(value) in (int, float) and math.isfinite(value)
            and 0 <= value <= MAX_MINUTES, 'invalid_minutes')
    return value


def sequence(value, maximum):
    require(type(value) is list and len(value) <= maximum, 'invalid_list')
    return value


def ids(value, universe):
    sequence(value, MAX_ITEMS)
    for item in value:
        ident(item)
    require(len(set(value)) == len(value) and set(value) <= universe, 'invalid_identity_list')
    return value


def digest(value):
    return hashlib.sha256(receipts.encode(value)).hexdigest()


def validate(payload):
    keys(payload, 'schema source horizon items dependencies scenarios')
    require(payload['schema'] == SCHEMA, 'unsupported_schema')
    source = payload['source']
    keys(source, 'snapshot_sha256 observed_at valid_until coverage limitations')
    sha(source['snapshot_sha256'])
    observed, expires = instant(source['observed_at']), instant(source['valid_until'])
    require(observed < expires and expires - observed <= dt.timedelta(days=1), 'invalid_freshness_window')
    require(source['coverage'] in ('complete', 'partial'), 'invalid_coverage')
    for line in sequence(source['limitations'], 20):
        text(line)
    require(source['coverage'] != 'partial' or source['limitations'], 'partial_coverage_requires_limits')
    keys(payload['horizon'], 'start end')
    start, end = (instant(payload['horizon'][k]) for k in ('start', 'end'))
    require(start < end and end - start <= dt.timedelta(days=42), 'invalid_horizon')
    items = {}
    for item in sequence(payload['items'], MAX_ITEMS):
        keys(item, 'id identity_kind revision title kind state task_type estimate required reason due_at')
        identity = ident(item['id']); sha(item['revision'])
        require(identity not in items, 'duplicate_item')
        require(item['identity_kind'] in ('canonical_work', 'planning_item'), 'invalid_identity_kind')
        if item['identity_kind'] == 'canonical_work':
            require(re.fullmatch(r'work-[a-f0-9]{24}', identity), 'invalid_work_identity')
        text(item['title'], 500); text(item['reason']); text(item['task_type'], 128)
        require(item['kind'] in ('task', 'decision', 'external'), 'invalid_kind')
        require(item['state'] in ('open', 'done', 'cancelled', 'unknown'), 'invalid_state')
        require(type(item['required']) is bool, 'invalid_required')
        if item['due_at'] is not None:
            instant(item['due_at'])
        if item['estimate'] is not None:
            keys(item['estimate'], 'low high basis kind calibration_version')
            low, high = (minutes(item['estimate'][k]) for k in ('low', 'high'))
            require(0 < low <= high, 'invalid_estimate_range')
            text(item['estimate']['basis'])
            require(item['estimate']['kind'] in ('initial', 'calibrated_proposal'), 'invalid_estimate_kind')
            if item['estimate']['kind'] == 'initial':
                require(item['estimate']['calibration_version'] is None, 'initial_calibration_version')
            else:
                sha(item['estimate']['calibration_version'])
        items[identity] = item
    seen = set()
    for edge in sequence(payload['dependencies'], MAX_ITEMS * 4):
        keys(edge, 'prerequisite dependent prerequisite_revision dependent_revision certainty evidence')
        ident(edge['prerequisite']); ident(edge['dependent'])
        sha(edge['prerequisite_revision']); sha(edge['dependent_revision']); text(edge['evidence'])
        require(edge['dependent'] in items, 'unknown_dependent')
        require(edge['certainty'] in ('confirmed', 'uncertain'), 'invalid_certainty')
        pair = edge['prerequisite'], edge['dependent']
        require(pair not in seen, 'duplicate_dependency'); seen.add(pair)
    seen = set()
    tasks = {key for key, item in items.items() if item['kind'] == 'task' and item['state'] == 'open'}
    for scenario in sequence(payload['scenarios'], 8):
        keys(scenario, 'id label capacity_minutes capacity_basis order assume_done unavailable')
        ident(scenario['id']); text(scenario['label'], 200); text(scenario['capacity_basis'])
        require(scenario['id'] not in seen, 'duplicate_scenario'); seen.add(scenario['id'])
        if scenario['capacity_minutes'] is not None:
            minutes(scenario['capacity_minutes'])
        require(set(ids(scenario['order'], set(items))) == tasks, 'order_must_cover_open_tasks')
        ids(scenario['assume_done'], set(items)); ids(scenario['unavailable'], set(items))
        require(not set(scenario['assume_done']) & set(scenario['unavailable']), 'conflicting_assumptions')
        require(all(items[key]['state'] not in ('cancelled', 'done') for key in scenario['assume_done']),
                'terminal_assumption')
    return items


def graph(payload, items):
    parents = {key: [] for key in items}
    problems = {key: set() for key in items}
    for edge in payload['dependencies']:
        before, after = edge['prerequisite'], edge['dependent']
        parents[after].append(before)
        if before not in items:
            problems[after].add('missing_prerequisite')
        elif (edge['prerequisite_revision'] != items[before]['revision']
              or edge['dependent_revision'] != items[after]['revision']):
            problems[after].add('changed_dependency_revision')
        if edge['certainty'] == 'uncertain':
            problems[after].add('uncertain_dependency')
    ancestors = {}
    for key in items:
        found, pending = set(), list(parents[key])
        while pending:
            parent = pending.pop()
            if parent in found:
                continue
            found.add(parent); pending.extend(parents.get(parent, []))
        ancestors[key] = found
        if key in found:
            problems[key].add('dependency_cycle')
    return parents, problems, ancestors


def simulate(items, parents, problems, ancestors, scenario, bound):
    capacity = scenario['capacity_minutes']
    fulfilled = {key for key, item in items.items() if item['state'] == 'done'} | set(scenario['assume_done'])
    unavailable = set(scenario['unavailable'])
    pending = set(scenario['order']) - fulfilled
    protected = {key for key, item in items.items() if item['required']}
    protected |= {ancestor for key in protected.copy() for ancestor in ancestors[key]}
    order = {key: i for i, key in enumerate(scenario['order'])}
    selected, deferred, used = [], {}, 0
    while pending:
        # Work on a prerequisite before its dependent even if the displayed
        # priority order puts the dependent first. Required work and its
        # prerequisites retain precedence over discretionary work.
        ready = [key for key in pending if all(p in fulfilled for p in parents[key])]
        if not ready:
            for key in pending:
                deferred[key] = sorted(problems[key] | {'prerequisite_not_satisfied'})
            break
        key = min(ready, key=lambda k: (k not in protected, order[k]))
        pending.remove(key)
        item = items[key]
        reasons = set(problems[key])
        if key in unavailable:
            reasons.add('unavailable_in_scenario')
        if capacity is None:
            reasons.add('capacity_unknown')
        if item['estimate'] is None:
            reasons.add('duration_unknown')
        cost = item['estimate'][bound] if item['estimate'] else None
        if not reasons and used + cost > capacity:
            reasons.add('capacity_exceeded')
        if reasons:
            deferred[key] = sorted(reasons)
        else:
            used += cost
            selected.append({'id': key, 'minutes': cost})
            fulfilled.add(key)
    for key, item in items.items():
        if item['state'] == 'unknown' and key not in fulfilled:
            deferred[key] = ['state_unknown']
    return {'selected': selected, 'deferred': [{'id': k, 'reasons': v} for k, v in sorted(deferred.items())],
            'used_minutes': used, 'remaining_minutes': None if capacity is None else capacity - used,
            'required_unfulfilled': sorted(k for k, item in items.items()
                                          if item['required'] and item['state'] != 'cancelled' and k not in fulfilled)}


def analyze(payload, *, now=None):
    items = validate(payload)
    now = now or dt.datetime.now(dt.timezone.utc)
    require(now.tzinfo is not None and now.utcoffset() is not None, 'invalid_clock')
    source = payload['source']
    fresh = instant(source['observed_at']) <= now <= instant(source['valid_until'])
    parents, problems, ancestors = graph(payload, items)
    results = []
    for scenario in payload['scenarios']:
        result = {'id': scenario['id'], 'label': scenario['label'], 'assumptions': copy.deepcopy(scenario),
                  'status': 'hypothetical' if fresh else 'source_recheck_required',
                  'low_duration': None, 'high_duration': None, 'sensitive_to_estimates': []}
        if fresh:
            low = simulate(items, parents, problems, ancestors, scenario, 'low')
            high = simulate(items, parents, problems, ancestors, scenario, 'high')
            result.update(low_duration=low, high_duration=high,
                          sensitive_to_estimates=sorted({r['id'] for r in low['selected']}
                                                       ^ {r['id'] for r in high['selected']}))
        results.append(result)
    decisions = []
    for key, item in items.items():
        if item['kind'] in ('decision', 'external') and item['state'] in ('open', 'unknown'):
            affected = sorted(other for other, lineage in ancestors.items() if key in lineage and other != key
                              and items[other]['state'] in ('open', 'unknown'))
            decisions.append({'id': key, 'kind': item['kind'], 'affected_ids': affected,
                              'affected_count': len(affected), 'reason': item['reason']})
    decisions.sort(key=lambda row: (-row['affected_count'], row['id']))
    start, end = (instant(payload['horizon'][k]) for k in ('start', 'end'))
    due = [{'id': key, 'due_at': item['due_at'],
            'position': 'before_horizon' if instant(item['due_at']) < start else 'within_horizon'}
           for key, item in items.items() if item['due_at'] and item['state'] not in ('done', 'cancelled')
           and instant(item['due_at']) <= end]
    return {'schema': 'planning-scenarios/v1', 'input_sha256': digest(payload),
            'source': copy.deepcopy(source), 'source_current': fresh, 'item_count': len(items),
            'scenarios': results, 'decisions': decisions, 'deadline_constraints': due,
            'dependency_issues': [{'id': key, 'reasons': sorted(v)} for key, v in problems.items() if v],
            'calendar_write_authorized': False, 'execution_authorized': False,
            'limits': ['Local owner-reviewed projection; source truth and identity are not established by hashes.',
                       'Capacity is a supplied assumption, not a verified calendar window or a schedule.',
                       'Selected work and assumed resolutions are hypothetical, not completed work.',
                       'Ordering is explicit priority with prerequisite and required-work constraints; no universal importance score.',
                       'Deadline constraints are exposed; minute budgets cannot prove delivery by a clock deadline.']}


def changes(before, after):
    old, new = validate(before), validate(after)
    changed = {key for key in set(old) | set(new) if old.get(key) != new.get(key)}
    old_edges = {(e['prerequisite'], e['dependent']): e for e in before['dependencies']}
    new_edges = {(e['prerequisite'], e['dependent']): e for e in after['dependencies']}
    for edge in set(old_edges) | set(new_edges):
        if old_edges.get(edge) != new_edges.get(edge):
            changed.add(edge[1])
    source_changed = before['source']['snapshot_sha256'] != after['source']['snapshot_sha256']
    affected = set(changed)
    for payload, nodes in ((before, old), (after, new)):
        _, _, ancestors = graph(payload, nodes)
        affected |= {key for key, lineage in ancestors.items() if changed & lineage}
    return {'schema': 'planning-invalidation/v1', 'before_sha256': digest(before), 'after_sha256': digest(after),
            'changed_ids': sorted(changed), 'affected_ids': sorted(affected),
            'source_snapshot_changed': source_changed,
            'source_recheck_required': source_changed or bool(changed),
            'previous_scenarios_reusable': before == after,
            'effects_applied': False,
            'limits': ['Recheck affected plans and existing preparation before reuse; this report changes no source, job or task.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('analyze', 'changes'))
    parser.add_argument('--input', required=True)
    parser.add_argument('--previous')
    args = parser.parse_args(argv)
    try:
        payload = receipts.parse(receipts.read(args.input, 512 * 1024))
        if args.operation == 'changes':
            require(args.previous is not None, 'previous_required')
            result = changes(receipts.parse(receipts.read(args.previous, 512 * 1024)), payload)
        else:
            require(args.previous is None, 'unexpected_previous')
            result = analyze(payload)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError, OverflowError, RecursionError):
        print(json.dumps({'schema': 'planning-error/v1', 'error': 'invalid_or_unreadable_planning_input'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
