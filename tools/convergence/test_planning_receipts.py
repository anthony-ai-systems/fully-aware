import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import planning_receipts as p


class PlanningReceiptsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name).resolve()
        self.case=self.root/'decision-one'
        now=dt.datetime.now(dt.timezone.utc)
        self.design=self.root/'design.json'
        self.design.write_bytes(p.encode({'frozen_at':(now-dt.timedelta(days=2)).isoformat()}))
        self.board=self.root/'board.json'
        self.rows=[{'id':'zeta','effective_status':'open','due_at':'2026-09-24','project':'Z'},
                   {'id':'alpha','effective_status':'open','due_at':'2026-09-24T00:00:00Z','project':'A'},
                   {'id':'earliest','state':'todo','due_at':'2026-09-23T23:00:00-01:00','project':'E'},
                   {'id':'unknown','state':'mystery','due_at':'2020-01-01'},
                   {'id':'closed','effective_status':'completed','due_at':'2020-01-01'},
                   {'id':'undated','state':'open','due_at':None}]
        self.board.write_bytes(p.encode({'base':{'items':self.rows}}))
        self.data={'sweep_run_id':'natural-sweep','encounter_id':'encounter-one',
            'encountered_at':(now-dt.timedelta(minutes=1)).isoformat(),
            'frozen_design':self.ref(self.design),'source_ref':self.ref(self.board),
            'source_cutoff':(now-dt.timedelta(minutes=2)).isoformat(),'board_current':True,
            'coverage_limits':['Calendar inventory partial.'],'development_case':False,
            'already_ranked_or_labelled':False}
        self.tail=None

    def tearDown(self): self.tmp.cleanup()

    def ref(self,path): return {'path':str(path),'sha256':p.sha(path.read_bytes())}

    def put(self,kind,data):
        result=p.append(self.case,kind,data,self.tail);self.tail=result['tail_sha256'];return result

    def source(self): return self.put('source',self.data)

    def proposal(self):
        self.source();self.put('baseline',{})
        return self.put('proposal',{'ordered_ids':['alpha','zeta'],'reasons':['First.','Second.'],
                                    'coverage_limits':['Partial.'],'human_labels_known':False})

    def answer(self):
        return {'task_id':'12345678-1234-1234-1234-123456789abc','turn_id':'actual-turn',
                'message_id':None,'reference_limit':'Individual message ID unavailable.',
                'observed_at':dt.datetime.now(dt.timezone.utc).isoformat()}

    def test_baseline_reuses_module_and_preserves_source_ties_dates_and_exclusions(self):
        s=self.source()
        self.assertEqual(s['derived']['eligible_indices'],[0,1,2])
        self.assertEqual(len(s['derived']['exclusions']),3)
        b=self.put('baseline',{})
        self.assertEqual(b['derived']['ordered_ids'],['zeta','alpha','earliest'])
        expected=p.brief._deadline_baseline({'items':self.rows[:3]},True,dt.datetime.now(dt.timezone.utc))
        self.assertEqual(b['derived']['result'],expected)
        self.assertEqual(p.inspect(self.case)['phase'],'baseline')
        self.assertEqual((self.case/'0001-source.json').stat().st_mode&0o777,0o600)
        self.assertEqual(self.case.stat().st_mode&0o777,0o700)

    def test_source_copy_is_frozen_when_original_changes(self):
        self.source();self.board.write_text('{}')
        self.assertEqual(self.put('baseline',{})['derived']['ordered_ids'][0],'zeta')

    def test_source_rejects_nonprospective_stale_unknown_and_duplicate(self):
        for changes in [{'board_current':False},{'already_ranked_or_labelled':True},{'development_case':True},
                        {'source_cutoff':'2000-01-01T00:00:00Z'}, {'encountered_at':'2000-01-01T00:00:00Z'}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                p.append(self.case,'source',dict(self.data,**changes),None)
        self.rows.append(self.rows[0]);self.board.write_bytes(p.encode({'items':self.rows}))
        self.data['source_ref']=self.ref(self.board)
        with self.assertRaises(ValueError):self.source()

    def test_phase_and_stale_compare_and_swap_refuse(self):
        with self.assertRaises(ValueError):self.put('baseline',{})
        self.source()
        with self.assertRaises(ValueError):p.append(self.case,'baseline',{},None)
        with self.assertRaises(ValueError):self.put('label',{})
        self.assertEqual(p.inspect(self.case)['receipts'],1)

    def test_chain_tamper_and_changed_release_refuse(self):
        self.source();self.put('baseline',{})
        file=self.case/'0001-source.json';file.write_bytes(file.read_bytes()+b' ')
        with self.assertRaises(ValueError):p.inspect(self.case)
        # Source module drift invalidates derivative comparisons too.
        file.write_bytes(file.read_bytes()[:-1])
        with mock.patch.object(p,'modules',return_value={'changed':'pin'}),self.assertRaises(ValueError):
            p.inspect(self.case)

    def test_missing_labels_are_unknown_and_not_success(self):
        self.proposal()
        d={k:None for k in ['answer_reference','human_top_three_ids','usefulness','correction_burden',
                            'human_supervision_minutes','hard_failures','raw_answer_minimal']}
        result=self.put('label',d)['derived']
        self.assertFalse(result['complete_labels']);self.assertFalse(result['automatic_win'])
        self.assertIsNone(result['baseline_agreement']);self.assertIsNone(result['proposal_agreement'])

    def test_real_labels_have_provenance_and_no_automatic_win(self):
        self.proposal()
        d={'answer_reference':self.answer(),'human_top_three_ids':['alpha'],'usefulness':'useful',
           'correction_burden':0,'human_supervision_minutes':1,'hard_failures':[],
           'raw_answer_minimal':'Choose alpha; useful; no corrections.'}
        result=self.put('label',d)['derived']
        self.assertTrue(result['complete_labels']);self.assertFalse(result['automatic_win'])
        self.assertEqual(result['baseline_agreement'],1);self.assertEqual(result['proposal_agreement'],1)
        self.assertEqual(p.inspect(self.case)['phase'],'label')

    def test_late_human_label_appends_after_unknown_without_erasing_it(self):
        self.proposal()
        unknown={k:None for k in ['answer_reference','human_top_three_ids','usefulness','correction_burden',
                                  'human_supervision_minutes','hard_failures','raw_answer_minimal']}
        self.put('label',unknown)
        old=(self.case/'0004-label.json').read_bytes()
        self.put('label',dict(unknown,answer_reference=self.answer(),human_top_three_ids=['alpha'],
                             usefulness='useful',raw_answer_minimal='Alpha is useful.'))
        self.assertEqual(old,(self.case/'0004-label.json').read_bytes())
        self.assertEqual(p.inspect(self.case)['receipts'],5)
        with self.assertRaises(ValueError):self.put('label',unknown)

    def test_labels_without_answer_or_negative_measure_refuse(self):
        self.proposal()
        d={'answer_reference':None,'human_top_three_ids':['alpha'],'usefulness':'useful',
           'correction_burden':0,'human_supervision_minutes':1,'hard_failures':[], 'raw_answer_minimal':'Useful.'}
        with self.assertRaises(ValueError):self.put('label',d)
        d['answer_reference']=self.answer();d['human_supervision_minutes']=-1
        with self.assertRaises(ValueError):self.put('label',d)
        self.assertEqual(p.inspect(self.case)['phase'],'proposal')

    def test_missed_is_terminal_and_cannot_replace_or_win(self):
        r=self.put('missed',{'reason':'Snapshot was not captured before ranking.',
                            'encountered_at':None,'evidence_reference':'Existing sweep incomplete receipt.'})
        self.assertFalse(r['derived']['paired_scoring_eligible'])
        self.assertFalse(r['derived']['replacement_case_allowed'])
        with self.assertRaises(ValueError):self.source()

    def test_strict_json_permissions_and_symlinks(self):
        for raw in [b'{"a":1,"a":2}',b'{"n":NaN}',b'{"n":1e999}']:
            with self.assertRaises(ValueError):p.parse(raw)
        self.case.mkdir(mode=0o755)
        with self.assertRaises(ValueError):self.source()
        self.case.chmod(0o700)
        self.source()
        artifact=self.case/'source-artifact.json';artifact.unlink();artifact.symlink_to(self.board)
        with self.assertRaises(ValueError):p.inspect(self.case)
        with self.assertRaises(ValueError):p.number(10**1000)

    def test_crash_before_receipt_publication_can_resume_same_snapshot(self):
        real=p.publish
        def broken(path,data):
            if path.name=='0001-source.json':raise RuntimeError('before publication')
            real(path,data)
        with mock.patch.object(p,'publish',side_effect=broken),self.assertRaises(RuntimeError):self.source()
        self.assertEqual(p.inspect(self.case)['receipts'],0)
        self.source();self.assertEqual(p.inspect(self.case)['receipts'],1)

    def test_crash_after_publication_requires_inspection_not_duplicate(self):
        real=p.publish
        def broken(path,data):
            real(path,data)
            if path.name=='0001-source.json':raise RuntimeError('after publication')
        with mock.patch.object(p,'publish',side_effect=broken),self.assertRaises(RuntimeError):self.source()
        self.assertEqual(p.inspect(self.case)['receipts'],1)
        with self.assertRaises(ValueError):self.source()
        self.assertEqual(len(list(self.case.glob('0001-*'))),1)

    def outcome_source(self,future=False,estimate=45,malformed=False):
        now=dt.datetime.now(dt.timezone.utc)
        start=now+dt.timedelta(hours=1) if future else now-dt.timedelta(hours=2)
        approval={'quote':'Approve this block','task_id':'12345678-1234-1234-1234-123456789abc','turn_id':'turn'}
        proposal={'schema':'iris-schedule-pilot-proposal/v1','status':'scheduled_outcome_pending',
            'proposal_id':'approved-block','event_id':'event-one','start':start.isoformat(),
            'focus_end':(start+dt.timedelta(minutes=45)).isoformat(),
            'end':(start+dt.timedelta(minutes=60)).isoformat(),'approval':approval,'estimated_focus_minutes':estimate}
        calendar={k:proposal[k] for k in ['proposal_id','event_id','start','focus_end','end','approval']}
        calendar['schema']='iris-calendar-scheduled-receipt/v1'
        a=self.root/'proposal.json';a.write_bytes(p.encode([] if malformed else proposal))
        b=self.root/'calendar.json';b.write_bytes(p.encode(calendar))
        return self.put('outcome-source',{'proposal_ref':self.ref(a),'calendar_ref':self.ref(b)})

    def test_outcome_not_due_and_unknown_is_not_zero(self):
        self.outcome_source(future=True)
        d={'outcome':'unknown','focus_minutes':None,'source':'unknown','answer_reference':None,'raw_answer_minimal':None}
        with self.assertRaises(ValueError):self.put('outcome',d)
        self.case=self.root/'past-outcome';self.tail=None;self.outcome_source()
        with self.assertRaises(ValueError):self.put('outcome',dict(d,focus_minutes=0))
        r=self.put('outcome',d);self.assertFalse(r['derived']['timing_calibration_eligible'])

    def test_late_actual_outcome_preserves_unknown_and_refuses_downgrade(self):
        self.outcome_source()
        unknown={'outcome':'unknown','focus_minutes':None,'source':'unknown','answer_reference':None,'raw_answer_minimal':None}
        self.put('outcome',unknown);old=(self.case/'0002-outcome.json').read_bytes()
        self.put('outcome',{'outcome':'partial','focus_minutes':30,'source':'self_report',
                            'answer_reference':self.answer(),'raw_answer_minimal':'Partial;30 minutes.'})
        self.assertEqual(old,(self.case/'0002-outcome.json').read_bytes())
        self.assertFalse(p.inspect(self.case)['derived']['timing_calibration_eligible'])
        with self.assertRaises(ValueError):self.put('outcome',unknown)

    def test_actual_done_outcome_binds_original_approval_and_keeps_estimate(self):
        self.outcome_source()
        result=self.put('outcome',{'outcome':'done','focus_minutes':32,'source':'self_report',
                                  'answer_reference':self.answer(),'raw_answer_minimal':'Done,32 focused minutes.'})
        self.assertTrue(result['derived']['timing_calibration_eligible'])
        self.assertEqual(p.load(self.case)[0][0]['derived']['estimated_focus_minutes'],45)
        self.assertEqual(p.inspect(self.case)['phase'],'outcome')


    def test_regressed_clock_refuses_before_publishing_unreloadable_receipt(self):
        self.source()
        last=p.instant(p.load(self.case)[0][-1]['recorded_at'])
        with mock.patch.object(p,'clock_now',side_effect=[last+dt.timedelta(seconds=1),last-dt.timedelta(seconds=1)]):
            with self.assertRaisesRegex(ValueError,'clock regressed'):self.put('baseline',{})
        self.assertEqual(p.inspect(self.case)['receipts'],1)

    def test_invalid_snapshot_does_not_burn_case_and_corrected_source_works(self):
        good=self.board.read_bytes()
        self.board.write_bytes(p.encode({'items':[self.rows[0],self.rows[0]]}))
        self.data['source_ref']=self.ref(self.board)
        with self.assertRaises(ValueError):self.source()
        self.assertFalse((self.case/'design-artifact.json').exists())
        self.assertFalse((self.case/'source-artifact.json').exists())
        self.board.write_bytes(good);self.data['source_ref']=self.ref(self.board)
        self.source();self.assertEqual(p.inspect(self.case)['phase'],'source')

    def test_oversized_derived_receipt_does_not_publish_snapshots(self):
        rows=[{'id':str(i)+'x'*145,'state':'open','due_at':'2026-09-24'} for i in range(2000)]
        self.board.write_bytes(p.encode({'items':rows}));self.data['source_ref']=self.ref(self.board)
        with self.assertRaisesRegex(ValueError,'receipt too large'):self.source()
        self.assertFalse((self.case/'source-artifact.json').exists())
        self.assertFalse((self.case/'design-artifact.json').exists())

    def test_outcome_requires_positive_estimate_and_dict_artifacts_without_mutation(self):
        for i,estimate in enumerate([None,0,-1,True]):
            self.case=self.root/('invalid-estimate-'+str(i));self.tail=None
            with self.assertRaises(ValueError):self.outcome_source(estimate=estimate)
            self.assertFalse((self.case/'proposal-artifact.json').exists())
        self.case=self.root/'malformed-outcome';self.tail=None
        with self.assertRaises(ValueError):self.outcome_source(malformed=True)
        self.assertFalse((self.case/'proposal-artifact.json').exists())
        with self.assertRaises(ValueError):p.response(dict(self.answer(),task_id=123),dt.datetime.now(dt.timezone.utc))

    def test_fieldwise_downgrade_and_older_known_label_refuse(self):
        self.proposal()
        d={'answer_reference':self.answer(),'human_top_three_ids':['alpha'],'usefulness':'useful',
           'correction_burden':0,'human_supervision_minutes':1,'hard_failures':[], 'raw_answer_minimal':'Alpha, useful.'}
        self.put('label',d)
        for key in ['human_top_three_ids','usefulness','correction_burden','human_supervision_minutes','hard_failures']:
            with self.subTest(key=key),self.assertRaises(ValueError):self.put('label',dict(d,**{key:None}))
        older=dict(d['answer_reference'],observed_at=(p.instant(d['answer_reference']['observed_at'])-dt.timedelta(microseconds=1)).isoformat())
        with self.assertRaises(ValueError):self.put('label',dict(d,answer_reference=older))
        self.assertEqual(p.inspect(self.case)['label_receipts'],1)

    def test_known_outcome_cannot_lose_minutes_or_become_unknown(self):
        self.outcome_source()
        d={'outcome':'done','focus_minutes':32,'source':'self_report','answer_reference':self.answer(),'raw_answer_minimal':'Done,32minutes.'}
        self.put('outcome',d)
        for change in [{'focus_minutes':None},{'outcome':'unknown'}]:
            with self.assertRaises(ValueError):self.put('outcome',dict(d,**change))
        self.assertEqual(p.inspect(self.case)['outcome_receipts'],1)

    def test_late_preparation_refuses_but_keeps_missed_path(self):
        self.source();stamp=p.instant(p.load(self.case)[0][0]['recorded_at'])
        with mock.patch.object(p,'clock_now',return_value=stamp+dt.timedelta(seconds=901)):
            with self.assertRaisesRegex(ValueError,'window expired'):self.put('baseline',{})
        self.put('missed',{'reason':'Window expired.','encountered_at':None,'evidence_reference':'Original sweep.'})
        self.assertEqual(p.inspect(self.case)['phase'],'missed')


    def test_empty_proposal_is_not_a_scored_case(self):
        self.source();self.put('baseline',{})
        with self.assertRaises(ValueError):self.put('proposal',{'ordered_ids':[],'reasons':[],
            'coverage_limits':[],'human_labels_known':False})

    def test_non_dict_design_is_rejected_before_freezing(self):
        self.design.write_bytes(p.encode([]));self.data['frozen_design']=self.ref(self.design)
        with self.assertRaises(ValueError):self.source()
        self.assertFalse((self.case/'design-artifact.json').exists())

    def test_older_outcome_answer_cannot_supersede_newer_one(self):
        self.outcome_source()
        d={'outcome':'done','focus_minutes':32,'source':'self_report','answer_reference':self.answer(),'raw_answer_minimal':'Done,32minutes.'}
        self.put('outcome',d)
        old=dict(d['answer_reference'],observed_at=(p.instant(d['answer_reference']['observed_at'])-dt.timedelta(microseconds=1)).isoformat())
        with self.assertRaises(ValueError):self.put('outcome',dict(d,answer_reference=old))


if __name__=='__main__':unittest.main()
