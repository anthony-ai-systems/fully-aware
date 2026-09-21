"""Propose scenario estimates from verified focus-outcome chains; never adopt them.

The existing IRIS owner supplies explicit comparable-unit classifications. A
completed focus unit is not evidence of completion of a larger client project.
The original estimates, Calendar events and outcome receipts remain immutable.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
import statistics

try:
    from . import planning_receipts as receipts
    from . import planning_scenarios as planner
except ImportError:
    import planning_receipts as receipts
    import planning_scenarios as planner

MINIMUM_COMPLETED_UNITS = 5
ALGORITHM = 'focus-ratio-range/v1'


def fingerprint(case):
    paths = []
    for path in case.glob('[0-9]*-*.json'):
        paths.append(path)
        planner.require(len(paths) <= 16, 'too_many_receipts')
    return tuple((path.name, receipts.sha(receipts.read(path, 256 * 1024))) for path in sorted(paths))


def propose(payload, *, now=None):
    planner.keys(payload, 'schema context episodes')
    planner.require(payload['schema'] == 'planning-calibration-input/v1', 'unsupported_schema')
    planner.validate(payload['context'])
    planner.require(all(item['estimate'] is None or item['estimate']['kind'] == 'initial'
                        for item in payload['context']['items']), 'original_estimates_required')
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = planner.instant(payload['context']['source']['observed_at'])
    planner.require(cutoff <= now, 'future_context')
    candidate = copy.deepcopy(payload['context'])
    groups, paths, proposal_ids, event_ids, summaries = {}, set(), set(), set(), []
    for ordinal, episode in enumerate(planner.sequence(payload['episodes'], 200), 1):
        planner.keys(episode, 'case_dir task_type unit_definition')
        task_type = planner.text(episode['task_type'], 128)
        unit = planner.text(episode['unit_definition'], 500)
        planner.text(episode['case_dir'], 4096)
        case = Path(episode['case_dir'])
        planner.require(case.is_absolute() and case.resolve() == case and case.is_dir(), 'invalid_case_directory')
        planner.require(case.stat().st_uid == os.getuid() and not case.stat().st_mode & 0o077,
                        'private_case_directory_required')
        planner.require(case not in paths, 'duplicate_case'); paths.add(case)
        before = fingerprint(case)
        chain, tail = receipts.load(case)
        planner.require(before == fingerprint(case), 'case_changed_during_read')
        planner.require(chain and chain[0]['kind'] == 'outcome-source', 'outcome_chain_required')
        planner.require(all(planner.instant(row['recorded_at']) <= cutoff for row in chain), 'future_training_evidence')
        original = chain[0]['derived']
        planner.require(original['proposal_id'] not in proposal_ids and original['event_id'] not in event_ids,
                        'duplicate_focus_unit')
        proposal_ids.add(original['proposal_id']); event_ids.add(original['event_id'])
        group = groups.setdefault(task_type, {'unit_definition': unit, 'ratios': [], 'outcomes': [], 'tails': []})
        planner.require(group['unit_definition'] == unit, 'inconsistent_unit_definition')
        row = chain[-1] if chain[-1]['kind'] == 'outcome' else None
        outcome = row['data']['outcome'] if row else 'unknown'
        group['outcomes'].append(outcome); group['tails'].append(tail)
        eligible = bool(row and row['derived']['timing_calibration_eligible'])
        if eligible:
            group['ratios'].append(row['data']['focus_minutes'] / original['estimated_focus_minutes'])
        summaries.append({'slot': ordinal, 'tail_sha256': tail, 'task_type': task_type,
                          'outcome': outcome, 'timing_eligible': eligible})
    versions, proposals, improvements = [], [], []
    for task_type, group in sorted(groups.items()):
        ratios = group['ratios']; observed = len(group['outcomes']) - group['outcomes'].count('unknown')
        group_version = planner.digest({'task_type': task_type, 'unit_definition': group['unit_definition'],
                                        'tails': sorted(group['tails']), 'algorithm': ALGORITHM,
                                        'minimum_completed_units': MINIMUM_COMPLETED_UNITS})
        summary = {'task_type': task_type, 'unit_definition': group['unit_definition'],
                   'version': group_version, 'units': len(group['outcomes']),
                   'observed_units': observed, 'unknown_units': group['outcomes'].count('unknown'),
                   'completed_units_with_minutes': len(ratios),
                   'minimum_completed_units': MINIMUM_COMPLETED_UNITS,
                   'median_ratio': statistics.median(ratios) if ratios else None,
                   'estimate_change_proposed': len(ratios) >= MINIMUM_COMPLETED_UNITS}
        versions.append(summary)
        if len(ratios) >= MINIMUM_COMPLETED_UNITS:
            for item in candidate['items']:
                if item['task_type'] != task_type or item['estimate'] is None or item['kind'] != 'task':
                    continue
                old = copy.deepcopy(item['estimate'])
                low = max(1, math.floor(old['low'] * min(ratios)))
                high = max(low, math.ceil(old['high'] * max(ratios)))
                typical = math.ceil((old['low'] + old['high']) / 2 * statistics.median(ratios))
                if high > planner.MAX_MINUTES:
                    proposals.append({'id': item['id'], 'status': 'range_requires_review', 'version': group_version})
                    continue
                item['estimate'] = {'low': low, 'high': high,
                                    'kind': 'calibrated_proposal', 'calibration_version': group_version,
                                    'basis': f'Proposed empirical focus-unit range from {len(ratios)} completed self-reports; version {group_version}; future accuracy unverified.'}
                # This is a changed derived planning item, not a new canonical
                # source revision. Preserve its original identity and revision.
                proposals.append({'id': item['id'], 'status': 'proposal_only', 'version': group_version,
                                  'original': old, 'proposed': copy.deepcopy(item['estimate']),
                                  'typical_minutes': typical})
        missed = sum(group['outcomes'].count(k) for k in ('moved', 'skipped'))
        if observed >= MINIMUM_COMPLETED_UNITS and missed >= 3:
            improvements.append({'task_type': task_type, 'version': group_version,
                                 'reason': f'{missed} moved or skipped units among {observed} observed units',
                                 'proposal': 'Review the time window, unit size and prerequisites before expanding blocks.',
                                 'effects_applied': False})
    planner.validate(candidate)
    return {'schema': 'planning-calibration/v1', 'input_sha256': planner.digest(payload),
            'training_cutoff': cutoff.isoformat(), 'episodes': summaries, 'groups': versions,
            'algorithm': ALGORITHM,
            'estimate_proposals': proposals, 'workflow_improvement_proposals': improvements,
            'candidate_context': candidate, 'live_estimates_changed': False, 'rollout_authorized': False,
            'limits': ['Unit classifications are explicit owner-reviewed inputs, not inferred preferences.',
                       'Empirical minima, medians and maxima are descriptive, not confidence intervals.',
                       'Unknown, partial, moved and skipped units do not teach completed-unit duration.',
                       'The candidate may be compared in scenarios; original estimates and records never change.',
                       'Future usefulness requires held-out evaluation; no learned-ranking gain is established.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    args = parser.parse_args(argv)
    try:
        result = propose(receipts.parse(receipts.read(args.input, 512 * 1024)))
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError, OverflowError, RecursionError):
        print(json.dumps({'schema': 'planning-error/v1', 'error': 'invalid_or_unreadable_calibration_input'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
