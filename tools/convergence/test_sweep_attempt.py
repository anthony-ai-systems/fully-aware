import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
import sweep_attempt as a
import sweep_clock as c

TRIGGER='2026-09-23T00:01:21.916Z'
CLOSED='2026-09-23T01:42:30.300723Z'
NOW=dt.datetime(2026,9,23,2,tzinfo=dt.timezone.utc)

class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve(); self.run=self.root/'run-a'; self.run.mkdir(mode=0o700)
        self.receipt={'schema':'iris-sweep-late-closure/v1','automation_id':a.AUTOMATION,
                      'trigger_at':TRIGGER,'late_closure_at':CLOSED,'original_start_monotonic':'not_recorded',
                      'work_admission':'refused; no fresh work',
                      'owner':'IRIS existing task 01a0cf00-be7a-7263-ac37-4d175f09b546'}
        self.source=self.run/'late-closure.json'; self.save(self.source,self.receipt)
    def save(self,path,value):
        path.write_bytes(c.encode(value)); path.chmod(0o600)
    def create(self):
        return a.create(str(self.run),str(self.source),c.sha(self.source.read_bytes()),'2026-09-22',17,root=self.root,now=NOW)
    def test_real_late_shape_surfaces_miss_separate_from_source(self):
        pointer=self.create(); result=a.read_attempt(pointer['path'],pointer['sha256'],root=self.root,now=NOW)
        self.assertEqual(result['status'],'missed_before_start'); self.assertGreater(result['trigger_to_close_seconds'],6000)
        self.assertEqual(result['source_freshness'],'not_established'); self.assertEqual(result['authority'],'none')

    def legacy_attempt(self):
        pointer = self.create()
        target = Path(pointer['path']); value = json.loads(target.read_text())
        value['owner_thread_id'] = '01a08366-bd65-72a3-b7a8-ae0e5ab5bb20'
        self.receipt['owner'] = 'IRIS existing task ' + value['owner_thread_id']
        self.save(self.source, self.receipt)
        value['receipt']['sha256'] = c.sha(self.source.read_bytes())
        self.save(target, value)
        return target, value

    def test_new_attempt_uses_current_owner_and_read_is_not_execution(self):
        pointer = self.create()
        self.assertEqual(json.loads(Path(pointer['path']).read_text())['owner_thread_id'],
                         '01a0cf00-be7a-7263-ac37-4d175f09b546')
        self.assertEqual(a.read_attempt(pointer['path'], pointer['sha256'], root=self.root, now=NOW)['authority'], 'none')

    def test_legacy_attempt_remains_readable_without_rewriting(self):
        target, _ = self.legacy_attempt()
        before = (target.read_bytes(), self.source.read_bytes())
        result = a.read_attempt(str(target), c.sha(before[0]), root=self.root, now=NOW)
        self.assertEqual(result['status'], 'missed_before_start')
        self.assertEqual((target.read_bytes(), self.source.read_bytes()), before)

    def test_relabelled_legacy_receipt_refuses_in_both_directions(self):
        target, value = self.legacy_attempt()
        # An envelope cannot adopt another owner's late closure, in either direction.
        for envelope_owner, receipt_owner in (
                ('01a08366-bd65-72a3-b7a8-ae0e5ab5bb20', '01a0cf00-be7a-7263-ac37-4d175f09b546'),
                ('01a0cf00-be7a-7263-ac37-4d175f09b546', '01a08366-bd65-72a3-b7a8-ae0e5ab5bb20')):
            value['owner_thread_id'] = envelope_owner
            self.receipt['owner'] = 'IRIS existing task ' + receipt_owner
            self.save(self.source, self.receipt)
            value['receipt']['sha256'] = c.sha(self.source.read_bytes())
            self.save(target, value)
            self.assertEqual(a.read_attempt(str(target), c.sha(target.read_bytes()), root=self.root, now=NOW)['availability'], 'unavailable')

    def test_retired_owner_cannot_claim_trigger_at_or_after_retirement(self):
        target, value = self.legacy_attempt()
        for trigger in ('2026-09-23T16:08:12Z', '2026-09-23T16:08:13Z'):
            value.update(trigger_at=trigger, closed_at='2026-09-23T17:00:00Z', recorded_at='2026-09-23T17:01:00Z')
            value['intended_slot'] = {'local_date':'2026-09-23','hour':9,'timezone':'America/Los_Angeles'}
            self.receipt.update(trigger_at=trigger, late_closure_at=value['closed_at'])
            self.save(self.source, self.receipt); value['receipt']['sha256'] = c.sha(self.source.read_bytes())
            with self.assertRaisesRegex(ValueError, 'retired_attempt_owner'):
                a.validate(value, target, root=self.root, now=dt.datetime(2026,9,23,18,tzinfo=dt.timezone.utc))

    def test_creator_does_not_relabel_a_legacy_receipt(self):
        self.receipt['owner'] = 'IRIS existing task 01a08366-bd65-72a3-b7a8-ae0e5ab5bb20'
        self.save(self.source, self.receipt)
        with self.assertRaisesRegex(ValueError, 'receipt_owner_mismatch'):
            self.create()
        self.assertFalse((self.run/'attempt.json').exists())
    def test_conflicting_original_owner_refused(self):
        self.receipt["owner"] = "another owner"; self.save(self.source, self.receipt)
        with self.assertRaises(ValueError): self.create()
    def test_missing_original_owner_refused(self):
        del self.receipt['owner']; self.save(self.source, self.receipt)
        with self.assertRaises(ValueError): self.create()
    def test_wrong_envelope_filename_refused(self):
        p=self.create(); alias=self.run/'renamed.json'; alias.write_bytes(Path(p['path']).read_bytes()); alias.chmod(0o600)
        self.assertEqual(a.read_attempt(str(alias),p['sha256'],root=self.root,now=NOW)['availability'],'unavailable')
    def test_receipt_filename_schema_mismatch_refused(self):
        self.source=self.run/'outcome.json'; self.save(self.source,self.receipt)
        with self.assertRaises(ValueError): self.create()
    def test_envelope_never_overwrites(self):
        self.create()
        with self.assertRaises(FileExistsError): self.create()
    def test_source_tampering_refuses(self):
        p=self.create(); self.receipt['work_admission']='work occurred'; self.save(self.source,self.receipt)
        self.assertEqual(a.read_attempt(p['path'],p['sha256'],root=self.root,now=NOW)['availability'],'unavailable')
    def test_status_owner_slot_and_future_tampering_refuses(self):
        p=self.create(); original=json.loads(Path(p['path']).read_text())
        cases=[('status','outcome_recorded'),('owner_thread_id','other'),('run_id','other'),
               ('automation_id','other'),('recorded_at','2027-01-01T00:00:00Z'),
               ('intended_slot',{'local_date':'2026-09-22','hour':13,'timezone':'America/Los_Angeles'})]
        for key,value in cases:
            changed=copy.deepcopy(original); changed[key]=value; self.save(Path(p['path']),changed)
            result=a.read_attempt(p['path'],c.sha(Path(p['path']).read_bytes()),root=self.root,now=NOW)
            self.assertEqual(result['availability'],'unavailable',key)
    def test_read_does_not_follow_other_run_reference(self):
        p=self.create(); other=self.root/'other'; other.mkdir(mode=0o700); target=other/'late-closure.json'; self.save(target,self.receipt)
        data=json.loads(Path(p['path']).read_text()); data['receipt']['path']=str(target); self.save(Path(p['path']),data)
        self.assertEqual(a.read_attempt(p['path'],c.sha(Path(p['path']).read_bytes()),root=self.root,now=NOW)['availability'],'unavailable')
    def test_symlink_and_missing_hash_refused(self):
        p=self.create(); source=Path(p['path']); alias=self.run/'alias.json'; alias.symlink_to(source)
        self.assertEqual(a.read_attempt(str(alias),p['sha256'],root=self.root,now=NOW)['availability'],'unavailable')
        self.assertEqual(a.read_attempt(p['path'],None,root=self.root,now=NOW)['availability'],'unavailable')
    def test_deep_json_attempt_is_unavailable(self):
        path=self.run/'attempt.json'; raw=('[' * 8000 + '0' + ']' * 8000).encode()
        self.assertEqual(len(raw),16001)
        path.write_bytes(raw); path.chmod(0o600)
        self.assertEqual(a.read_attempt(str(path),c.sha(raw),root=self.root,now=NOW),
                         {'availability':'unavailable','reason':'attempt_validation_failed','authority':'none'})
    def test_normal_outcome_is_recorded_not_success(self):
        self.source=self.run/'outcome.json'
        self.receipt={'schema':'iris-sweep-outcome/v1','run_id':'run-a','trigger_at':TRIGGER,
                      'started_at':'2026-09-23T00:01:25Z','ended_at':'2026-09-23T00:10:00Z','status':'partial'}
        self.save(self.source,self.receipt); p=self.create(); result=a.read_attempt(p['path'],p['sha256'],root=self.root,now=NOW)
        self.assertEqual(result['status'],'outcome_recorded'); self.assertNotIn('success',result)
    def test_normal_outcome_under_late_closure_is_refused(self):
        self.source=self.run/'late-closure.json'
        self.receipt={'schema':'iris-sweep-outcome/v1','run_id':'run-a','trigger_at':TRIGGER,
                      'started_at':'2026-09-23T00:01:25Z','ended_at':'2026-09-23T00:10:00Z','status':'partial'}
        self.save(self.source,self.receipt)
        with self.assertRaises(ValueError): self.create()
    def test_uncertain_late_attempt_stays_unknown(self):
        self.receipt['original_start_monotonic']='unknown'; self.save(self.source,self.receipt)
        p=self.create(); self.assertEqual(a.read_attempt(p['path'],p['sha256'],root=self.root,now=NOW)['status'],'unknown')

if __name__=='__main__': unittest.main()
