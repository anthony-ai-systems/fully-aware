import copy
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import planning_calibration as c
import planning_receipts as r
import planning_scenarios as p
from test_planning_scenarios import sample, NOW


class PlanningCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.clock = NOW - dt.timedelta(days=1)

    def episode(self, index, outcome='done', actual=60, *, task_type='editing', event_id=None):
        folder = self.root / f'episode-{index}'; folder.mkdir(mode=0o700)
        start = self.clock - dt.timedelta(hours=2)
        approval = {'quote': 'Approved this synthetic block',
                    'task_id': '11111111-1111-4111-8111-111111111111', 'turn_id': 'fixture-turn'}
        proposal = {'schema': 'iris-schedule-pilot-proposal/v1', 'status': 'scheduled_outcome_pending',
                    'proposal_id': f'proposal-{index}', 'event_id': event_id or f'event-{index}',
                    'start': start.isoformat(), 'focus_end': (start+dt.timedelta(minutes=45)).isoformat(),
                    'end': (start+dt.timedelta(minutes=60)).isoformat(), 'approval': approval,
                    'estimated_focus_minutes': 30}
        calendar = {key: proposal[key] for key in ('proposal_id', 'event_id', 'start', 'focus_end', 'end', 'approval')}
        calendar['schema'] = 'iris-calendar-scheduled-receipt/v1'
        proposal_path = folder / 'proposal-input.json'; proposal_path.write_bytes(r.encode(proposal))
        calendar_path = folder / 'calendar-input.json'; calendar_path.write_bytes(r.encode(calendar))
        ref = lambda path: {'path': str(path), 'sha256': r.sha(path.read_bytes())}
        case = folder / 'receipts'
        with mock.patch.object(r, 'clock_now', return_value=self.clock):
            source = r.append(case, 'outcome-source', {'proposal_ref': ref(proposal_path),
                                                      'calendar_ref': ref(calendar_path)}, None)
        if outcome is not None:
            data = {'outcome': outcome, 'focus_minutes': actual, 'source': 'self_report',
                    'answer_reference': {'task_id': approval['task_id'], 'turn_id': 'synthetic-answer',
                                         'message_id': None, 'reference_limit': 'Synthetic test answer',
                                         'observed_at': self.clock.isoformat()},
                    'raw_answer_minimal': 'Synthetic recorded answer'}
            if outcome == 'unknown':
                data.update(focus_minutes=None, source='unknown', answer_reference=None, raw_answer_minimal=None)
            with mock.patch.object(r, 'clock_now', return_value=self.clock):
                r.append(case, 'outcome', data, source['tail_sha256'])
        return {'case_dir': str(case), 'task_type': task_type, 'unit_definition': 'One comparable editorial unit'}

    def request(self, episodes):
        return {'schema': 'planning-calibration-input/v1', 'context': sample(), 'episodes': episodes}

    def test_empty_and_insufficient_training_keeps_originals(self):
        for episodes in ([], [self.episode(1)]):
            request = self.request(episodes)
            result = c.propose(request, now=NOW)
            self.assertEqual(result['candidate_context'], request['context'])
            self.assertEqual(result['estimate_proposals'], [])
            self.assertFalse(result['rollout_authorized'])

    def test_five_completed_units_propose_estimates_and_change_hypothetical_fit(self):
        request = self.request([self.episode(i) for i in range(5)])
        frozen = copy.deepcopy(request)
        state = {str(f): f.read_bytes() for f in self.root.rglob('*') if f.is_file()}
        result = c.propose(request, now=NOW)
        estimate = result['candidate_context']['items'][0]['estimate']
        self.assertEqual((estimate['low'], estimate['high']), (40, 80))
        self.assertEqual(result['groups'][0]['median_ratio'], 2)
        self.assertEqual(result['candidate_context']['items'][0]['revision'], 'a' * 64)
        before = p.analyze(request['context'], now=NOW)['scenarios'][0]
        after = p.analyze(result['candidate_context'], now=NOW)['scenarios'][0]
        self.assertEqual(len(before['high_duration']['selected']), 1)
        self.assertEqual(after['high_duration']['selected'], [])
        self.assertEqual(request, frozen)
        self.assertEqual(state, {str(f): f.read_bytes() for f in self.root.rglob('*') if f.is_file()})
        with self.assertRaisesRegex(ValueError, 'original_estimates_required'):
            c.propose(dict(request, context=result['candidate_context']), now=NOW)

    def test_partial_missing_and_unknown_never_teach_full_duration(self):
        episodes = [self.episode(1, 'partial', 90), self.episode(2, 'done', None),
                    self.episode(3, 'unknown'), self.episode(4, None)]
        result = c.propose(self.request(episodes), now=NOW)
        self.assertEqual(result['groups'][0]['unknown_units'], 2)
        self.assertEqual(result['groups'][0]['completed_units_with_minutes'], 0)
        self.assertIsNone(result['groups'][0]['median_ratio'])
        self.assertEqual(result['estimate_proposals'], [])

    def test_distinct_types_are_not_pooled(self):
        episodes = [self.episode(i, task_type='editing' if i < 3 else 'research') for i in range(5)]
        result = c.propose(self.request(episodes), now=NOW)
        self.assertEqual([g['completed_units_with_minutes'] for g in result['groups']], [3, 2])
        self.assertEqual(result['estimate_proposals'], [])

    def test_private_directory_and_stable_receipts_are_required(self):
        episode = self.episode(1)
        case = Path(episode['case_dir']); case.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'private_case_directory_required'):
            c.propose(self.request([episode]), now=NOW)
        case.chmod(0o700)
        with mock.patch.object(c, 'fingerprint', side_effect=[(('before', 'a'),), (('after', 'b'),)]):
            with self.assertRaisesRegex(ValueError, 'case_changed_during_read'):
                c.propose(self.request([episode]), now=NOW)

    def test_duplicate_units_and_inconsistent_classification_refuse(self):
        episode = self.episode(1)
        with self.assertRaises(ValueError): c.propose(self.request([episode, episode]), now=NOW)
        duplicate = self.episode(2, event_id='event-1')
        with self.assertRaises(ValueError): c.propose(self.request([episode, duplicate]), now=NOW)
        distinct = self.episode(3); distinct['unit_definition'] = 'Different unit'
        with self.assertRaises(ValueError): c.propose(self.request([episode, distinct]), now=NOW)

    def test_evidence_after_planning_snapshot_is_excluded_by_refusal(self):
        self.clock = NOW - dt.timedelta(minutes=1)
        with self.assertRaisesRegex(ValueError, 'future_training_evidence'):
            c.propose(self.request([self.episode(1)]), now=NOW)

    def test_tamper_refuses_and_repeated_misses_only_propose_workflow_review(self):
        episodes = [self.episode(i, 'skipped', None) for i in range(5)]
        result = c.propose(self.request(episodes), now=NOW)
        self.assertEqual(result['estimate_proposals'], [])
        self.assertEqual(len(result['workflow_improvement_proposals']), 1)
        self.assertFalse(result['workflow_improvement_proposals'][0]['effects_applied'])
        path = Path(episodes[0]['case_dir']) / '0002-outcome.json'
        data = r.parse(path.read_bytes()); data['derived']['timing_calibration_eligible'] = True
        path.write_bytes(r.encode(data))
        with self.assertRaises(ValueError): c.propose(self.request(episodes), now=NOW)


if __name__ == '__main__':
    unittest.main()
