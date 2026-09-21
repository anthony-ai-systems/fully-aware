import copy
import datetime as dt
import unittest

import planning_scenarios as p

NOW = dt.datetime(2026, 9, 21, 16, tzinfo=dt.timezone.utc)


def item(identity, **changes):
    value = {'id': identity, 'identity_kind': 'planning_item', 'revision': 'a' * 64,
             'title': identity, 'kind': 'task', 'state': 'open', 'task_type': 'editing',
             'estimate': {'low': 20, 'high': 40, 'basis': 'Explicit unmeasured range',
                          'kind': 'initial', 'calibration_version': None},
             'required': False, 'reason': 'Existing owner direction', 'due_at': None}
    value.update(changes)
    return value


def edge(before, after, **changes):
    value = {'prerequisite': before, 'dependent': after,
             'prerequisite_revision': 'a' * 64, 'dependent_revision': 'a' * 64,
             'certainty': 'confirmed', 'evidence': 'Reviewed relation in source snapshot'}
    value.update(changes)
    return value


def sample(items=None, edges=None):
    nodes = items or [item('first'), item('second')]
    return {'schema': p.SCHEMA,
            'source': {'snapshot_sha256': 'b' * 64, 'observed_at': '2026-09-21T15:55:00Z',
                       'valid_until': '2026-09-21T16:10:00Z', 'coverage': 'partial',
                       'limitations': ['Other calendars unavailable']},
            'horizon': {'start': '2026-09-21T17:00:00Z', 'end': '2026-09-22T00:00:00Z'},
            'items': nodes, 'dependencies': edges or [],
            'scenarios': [{'id': 'current', 'label': 'Capacity assumption', 'capacity_minutes': 60,
                           'capacity_basis': 'User-confirmed hypothetical remaining budget',
                           'order': [n['id'] for n in nodes if n['kind'] == 'task' and n['state'] == 'open'],
                           'assume_done': [], 'unavailable': []}]}


def ids(rows):
    return [row['id'] for row in rows]


class PlanningScenariosTests(unittest.TestCase):
    def test_range_exposes_displacement_without_schedule_authority(self):
        original = sample(); frozen = copy.deepcopy(original)
        result = p.analyze(original, now=NOW)
        scenario = result['scenarios'][0]
        self.assertEqual(ids(scenario['low_duration']['selected']), ['first', 'second'])
        self.assertEqual(ids(scenario['high_duration']['selected']), ['first'])
        self.assertEqual(scenario['sensitive_to_estimates'], ['second'])
        self.assertFalse(result['calendar_write_authorized'])
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(original, frozen)

    def test_required_work_and_its_prerequisite_precede_discretionary(self):
        value = sample([item('discretionary'), item('required', required=True), item('prep')],
                       [edge('prep', 'required')])
        result = p.analyze(value, now=NOW)['scenarios'][0]['low_duration']
        self.assertEqual(ids(result['selected']), ['prep', 'required', 'discretionary'])
        value['scenarios'][0]['capacity_minutes'] = 40
        result = p.analyze(value, now=NOW)['scenarios'][0]['low_duration']
        self.assertEqual(ids(result['selected']), ['prep', 'required'])
        self.assertEqual(result['required_unfulfilled'], [])

    def test_dependent_does_not_skip_failed_prerequisite(self):
        value = sample([item('downstream'), item('prep', estimate=None)], [edge('prep', 'downstream')])
        report = p.analyze(value, now=NOW)['scenarios'][0]['low_duration']
        self.assertEqual(report['selected'], [])
        self.assertIn('duration_unknown', next(r for r in report['deferred'] if r['id'] == 'prep')['reasons'])
        self.assertIn('prerequisite_not_satisfied', report['deferred'][0]['reasons'])

    def test_decision_scenario_is_explicit_and_does_not_modify_real_state(self):
        value = sample([item('choice', kind='decision', estimate=None), item('work')], [edge('choice', 'work')])
        option = copy.deepcopy(value['scenarios'][0]); option.update(id='if-resolved', assume_done=['choice'])
        value['scenarios'].append(option)
        result = p.analyze(value, now=NOW)
        self.assertEqual(result['decisions'][0]['affected_ids'], ['work'])
        self.assertEqual(result['scenarios'][0]['low_duration']['selected'], [])
        self.assertEqual(ids(result['scenarios'][1]['low_duration']['selected']), ['work'])
        self.assertEqual(value['items'][0]['state'], 'open')

    def test_missing_changed_uncertain_and_cyclic_relations_fail_closed(self):
        cases = [([edge('missing', 'second')], 'missing_prerequisite'),
                 ([edge('first', 'second', prerequisite_revision='c' * 64)], 'changed_dependency_revision'),
                 ([edge('first', 'second', certainty='uncertain')], 'uncertain_dependency'),
                 ([edge('first', 'second'), edge('second', 'first')], 'dependency_cycle')]
        for edges, reason in cases:
            with self.subTest(reason=reason):
                result = p.analyze(sample(edges=edges), now=NOW)
                self.assertNotIn('second', ids(result['scenarios'][0]['low_duration']['selected']))
                self.assertTrue(any(reason in row['reasons'] for row in result['dependency_issues']))

    def test_cancelled_prerequisite_is_not_satisfied(self):
        value = sample([item('prep', state='cancelled'), item('second')], [edge('prep', 'second')])
        self.assertEqual(p.analyze(value, now=NOW)['scenarios'][0]['low_duration']['selected'], [])
        value['scenarios'][0]['assume_done'] = ['prep']
        with self.assertRaises(ValueError): p.analyze(value, now=NOW)

    def test_unknown_capacity_and_duration_are_not_zero(self):
        value = sample(); value['scenarios'][0]['capacity_minutes'] = None
        result = p.analyze(value, now=NOW)['scenarios'][0]['low_duration']
        self.assertEqual(result['selected'], [])
        self.assertIsNone(result['remaining_minutes'])
        self.assertEqual(len(result['deferred']), 2)

    def test_stale_and_future_source_never_emit_current_plan(self):
        for current in (NOW + dt.timedelta(hours=1), NOW - dt.timedelta(hours=1)):
            result = p.analyze(sample(), now=current)
            self.assertFalse(result['source_current'])
            self.assertIsNone(result['scenarios'][0]['low_duration'])
            self.assertEqual(result['scenarios'][0]['status'], 'source_recheck_required')

    def test_invalid_shapes_and_omitted_work_refuse(self):
        mutations = [lambda v: v['items'].append(copy.deepcopy(v['items'][0])),
                     lambda v: v['items'][0].update(identity_kind='canonical_work'),
                     lambda v: v['scenarios'][0].update(order=['first']),
                     lambda v: v['scenarios'][0].update(capacity_minutes=True),
                     lambda v: v['scenarios'][0].update(capacity_minutes=float('nan')),
                     lambda v: v['source'].update(limitations=[]),
                     lambda v: v['source'].update(observed_at='2026-09-21T15:55:00'),
                     lambda v: v['items'][0]['estimate'].update(low=50)]
        for mutate in mutations:
            value = sample(); mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                p.analyze(value, now=NOW)

    def test_correction_invalidates_transitive_dependents_but_not_unrelated_item(self):
        before = sample([item('source'), item('middle'), item('last'), item('unrelated')],
                        [edge('source', 'middle'), edge('middle', 'last')])
        after = copy.deepcopy(before); after['items'][0]['revision'] = 'd' * 64
        result = p.changes(before, after)
        self.assertEqual(result['changed_ids'], ['source'])
        self.assertEqual(result['affected_ids'], ['last', 'middle', 'source'])
        self.assertFalse(result['effects_applied'])
        self.assertFalse(result['previous_scenarios_reusable'])

    def test_removed_relation_or_item_remains_visible_in_change_report(self):
        before = sample(edges=[edge('first', 'second')]); after = copy.deepcopy(before)
        after['dependencies'] = []
        self.assertEqual(p.changes(before, after)['affected_ids'], ['second'])
        after['items'] = after['items'][1:]; after['scenarios'][0]['order'] = ['second']
        self.assertEqual(p.changes(before, after)['affected_ids'], ['first', 'second'])

    def test_deadline_correction_is_visible_without_claiming_clock_feasibility(self):
        value = sample(); value['items'][0]['due_at'] = '2026-09-21T16:30:00Z'
        report = p.analyze(value, now=NOW)
        self.assertEqual(report['deadline_constraints'][0]['position'], 'before_horizon')
        self.assertTrue(any('clock deadline' in x for x in report['limits']))


if __name__ == '__main__':
    unittest.main()
