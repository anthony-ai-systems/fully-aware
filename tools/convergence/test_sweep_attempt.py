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
                      'work_admission':'refused; no fresh work'}
        self.source=self.run/'late-closure.json'; self.save(self.source,self.receipt)
    def save(self,path,value):
        path.write_bytes(c.encode(value)); path.chmod(0o600)
    def create(self):
        return a.create(str(self.run),str(self.source),c.sha(self.source.read_bytes()),'2026-09-22',17,root=self.root,now=NOW)
    def test_real_late_shape_surfaces_miss_separate_from_source(self):
        pointer=self.create(); result=a.read_attempt(pointer['path'],pointer['sha256'],root=self.root,now=NOW)
        self.assertEqual(result['status'],'missed_before_start'); self.assertGreater(result['trigger_to_close_seconds'],6000)
        self.assertEqual(result['source_freshness'],'not_established'); self.assertEqual(result['authority'],'none')
    def test_conflicting_original_owner_refused(self):
        self.receipt["owner"] = "another owner"; self.save(self.source, self.receipt)
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
    def test_normal_outcome_is_recorded_not_success(self):
        self.source=self.run/'outcome.json'
        self.receipt={'schema':'iris-sweep-outcome/v1','run_id':'run-a','trigger_at':TRIGGER,
                      'started_at':'2026-09-23T00:01:25Z','ended_at':'2026-09-23T00:10:00Z','status':'partial'}
        self.save(self.source,self.receipt); p=self.create(); result=a.read_attempt(p['path'],p['sha256'],root=self.root,now=NOW)
        self.assertEqual(result['status'],'outcome_recorded'); self.assertNotIn('success',result)
    def test_uncertain_late_attempt_stays_unknown(self):
        self.receipt['original_start_monotonic']='unknown'; self.save(self.source,self.receipt)
        p=self.create(); self.assertEqual(a.read_attempt(p['path'],p['sha256'],root=self.root,now=NOW)['status'],'unknown')

if __name__=='__main__': unittest.main()
