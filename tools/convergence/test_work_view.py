"""Behavioral checks for the private read-only work view. All data is synthetic."""
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import work_view as w

NOW = '2026-09-05T17:00:00Z'
SHA = 'a' * 40
HASH = 'b' * 64


def clayton():
    return {'rendered_at': NOW, 'state': 'IDLE', 'current': None,
            'pending': 0, 'dispatches_today': 0, 'dispatch_cap': 24,
            'waiting_on_you': ['_(none listed)_']}


def boot():
    return {'schema': 'boot-pack/v1', 'generated_at': NOW,
            'warnings': [], 'open_items': [], 'sections': {'decision_queue': {'items': []}}}


def lane(name='client-a'):
    return {'name': name, 'step': 'review draft', 'health': 'waiting', 'updated': NOW,
            'waiting_on_anthony': ['choose an approach'], 'blocked': []}


def plans():
    return {'generated': NOW, 'lanes': [lane()]}


def view(c=None, b=None, p=None, **kw):
    return w.build_view(c if c is not None else clayton(),
                        b if b is not None else boot(), p if p is not None else plans(), NOW, **kw)


class ProjectionTests(unittest.TestCase):
    def test_deterministic_shared_snapshot_and_unchanged_inputs(self):
        inputs = (clayton(), boot(), plans()); before = copy.deepcopy(inputs)
        one = w.build_view(*inputs, NOW); two = w.build_view(*inputs, NOW)
        self.assertEqual(one, two); self.assertEqual(inputs, before)
        self.assertIn(one['snapshot_id'], w.render_markdown(one))
        self.assertTrue(one['advisory']); self.assertTrue(one['no_commands'])
        self.assertEqual(one['audience'], 'operator-local')

    def test_source_age_uses_producer_not_recent_read(self):
        c = clayton(); c['rendered_at'] = '2026-09-05T16:00:00Z'
        result = view(c, observations={'clayton': {'observed_at': NOW, 'sha256': HASH}})
        src = result['sources']['clayton']
        self.assertEqual(src['freshness'], 'stale'); self.assertEqual(src['age_seconds'], 3600)
        self.assertEqual(src['observed_at'], NOW)

    def test_timestamp_boundaries(self):
        cases = [('2026-09-05T16:50:00Z', 'fresh'), ('2026-09-05T16:49:59Z', 'stale'),
                 ('2026-09-05T17:01:01Z', 'future'), ('2026-09-05T17:00:00', 'unknown'),
                 ('invalid', 'unknown'), ('0001-01-01T00:00:00+23:59', 'unknown')]
        for stamp, expected in cases:
            with self.subTest(stamp=stamp):
                c = clayton(); c['rendered_at'] = stamp
                self.assertEqual(view(c)['sources']['clayton']['freshness'], expected)

    def test_plans_and_boot_have_independent_freshness(self):
        p = plans(); p['generated'] = '2026-09-02T22:00:00Z'
        p['lanes'][0]['updated'] = '2026-09-01'
        s = view(p=p)['sources']
        self.assertEqual(s['plans']['freshness'], 'stale')
        self.assertEqual(s['boot_pack']['freshness'], 'fresh')
        self.assertEqual(s['clayton']['freshness'], 'fresh')
        self.assertEqual(s['plans']['projection']['lanes'][0]['updated'], '2026-09-01')
        self.assertNotIn('lane_0_updated_invalid', s['plans']['issues'])

    def test_lane_updated_validates_calendar_dates_including_leap_day(self):
        for stamp, expected in (('2024-02-29', '2024-02-29'), ('2023-02-29', None), ('2026-02-30', None)):
            with self.subTest(stamp=stamp):
                p = plans(); p['lanes'][0]['updated'] = stamp
                source = view(p=p)['sources']['plans']
                self.assertEqual(source['projection']['lanes'][0]['updated'], expected)
                if expected is None:
                    self.assertIn('lane_0_updated_invalid', source['issues'])
                else:
                    self.assertNotIn('lane_0_updated_invalid', source['issues'])

    def test_lane_updated_keeps_aware_timestamp_support(self):
        stamp = '2026-09-05T12:00:00-05:00'
        p = plans(); p['lanes'][0]['updated'] = stamp
        source = view(p=p)['sources']['plans']
        self.assertEqual(source['projection']['lanes'][0]['updated'], stamp)
        self.assertNotIn('lane_0_updated_invalid', source['issues'])

    def test_lane_updated_rejects_naive_and_malformed_timestamps(self):
        for stamp in ('2026-09-05T17:00:00', 'malformed'):
            with self.subTest(stamp=stamp):
                p = plans(); p['lanes'][0]['updated'] = stamp
                source = view(p=p)['sources']['plans']
                self.assertIsNone(source['projection']['lanes'][0]['updated'])
                self.assertIn('lane_0_updated_invalid', source['issues'])

    def test_missing_does_not_become_empty(self):
        v = w.build_view(None, None, None, NOW, {'clayton': {'read_error': 'missing'}})
        self.assertEqual(v['sources']['clayton']['availability'], 'missing')
        self.assertEqual(v['sources']['clayton']['projection'], {})
        self.assertEqual(v['sources']['plans']['availability'], 'unconfigured')
        self.assertIn('unknown', w.render_markdown(v))

    def test_zero_idle_never_claims_clear_budget_progress_or_review(self):
        p = view()['sources']['clayton']['projection']
        self.assertEqual(p['pending'], 0)
        self.assertEqual(p['queue_completeness'], 'unknown')
        self.assertEqual(p['budget_verification'], 'unknown')
        self.assertIsNone(p['last_progress_at'])
        for key in ('review_records', 'completion_records', 'artifact_verification', 'outcome_verification'):
            self.assertEqual(p[key], 'unavailable')
        self.assertNotIn('remaining_budget', p)
        self.assertEqual(p['installation']['receipt_presence'], 'unavailable')
        self.assertEqual(p['installation']['readiness'], 'unverified')

    def test_untrusted_numeric_types_do_not_coerce(self):
        for value in (True, False, -1, '3', 3.5, {}, []):
            with self.subTest(value=value):
                c = clayton(); c.update(pending=value, dispatches_today=value, dispatch_cap=value)
                p = view(c)['sources']['clayton']['projection']
                self.assertIsNone(p['pending']); self.assertIsNone(p['dispatches_today']); self.assertIsNone(p['dispatch_cap'])

    def test_valid_running_identity_without_progress_inference(self):
        c = clayton(); c.update(state='RUNNING', current={'item': 'brief-a', 'source_item_id': 'iris:a',
            'brief_id': 'brief-a', 'kind': 'landing-page', 'started': NOW, 'elapsed_s': 99,
            'source_ref': '/private/content', 'artifacts': ['/private/output'], 'gate': 'approved'})
        p = view(c)['sources']['clayton']['projection']
        self.assertEqual(p['current']['source_item_id'], 'iris:a'); self.assertIsNone(p['last_progress_at'])
        raw = json.dumps(p); self.assertNotIn('/private/', raw); self.assertNotIn('elapsed_s', raw)

    def test_installation_consistency_requires_complete_matching_evidence(self):
        c = clayton(); c.update(repository_checkout_sha=SHA, deployed_checkout_sha=SHA,
            installed_profile_hashes={name: HASH for name in w.PROFILE_NAMES},
            installed_source_receipt={'source_commit': SHA, 'deployed_checkout_sha': SHA,
                'installed_at': NOW, 'profile_hashes': {name: HASH for name in w.PROFILE_NAMES}})
        installation = lambda: view(c)['sources']['clayton']['projection']['installation']
        self.assertEqual(installation()['installation_consistency'], 'consistent')
        self.assertEqual(installation()['readiness'], 'unverified')
        c['deployed_checkout_sha'] = 'c'*40
        self.assertEqual(installation()['installation_consistency'], 'mismatch')
        c['deployed_checkout_sha'] = SHA
        del c['installed_source_receipt']['installed_at']
        self.assertEqual(installation()['installation_consistency'], 'unknown')
        c['installed_source_receipt']['installed_at'] = NOW
        c['installed_profile_hashes']['a-lane.json'] = 'c'*64
        self.assertEqual(installation()['installation_consistency'], 'mismatch')

    def test_invalid_receipt_values_stay_unknown(self):
        c = clayton(); c.update(repository_checkout_sha='secret path', installed_source_receipt={'source_commit': True})
        p = view(c)['sources']['clayton']['projection']['installation']
        self.assertIsNone(p['repository_checkout_sha']); self.assertEqual(p['installation_consistency'], 'unknown')
        self.assertNotIn('secret path', json.dumps(p))

    def test_unsupported_schema_discards_projection(self):
        for source in ('clayton', 'boot_pack', 'plans'):
            with self.subTest(source=source):
                c,b,p = clayton(),boot(),plans(); data={'clayton':c,'boot_pack':b,'plans':p}[source]
                data['schema'] = 'future/v999'
                result = view(c,b,p)['sources'][source]
                self.assertEqual(result['availability'], 'invalid'); self.assertEqual(result['projection'], {})
                self.assertEqual(result['freshness'], 'unknown')

    def test_malformed_boot_never_returns_success_counts(self):
        b = boot(); b['sections']['decision_queue']['items'] = 'empty'
        p = view(b=b)['sources']['boot_pack']; self.assertEqual(p['availability'], 'invalid')
        self.assertEqual(p['projection'], {})

    def test_two_clients_remain_separate_and_duplicates_fail(self):
        p = plans(); p['lanes'] = [lane('b'), lane('a')]
        result = view(p=p)['sources']['plans']['projection']
        self.assertEqual([l['name'] for l in result['lanes']], ['a','b'])
        self.assertNotIn('factory_item', json.dumps(result))
        p['lanes'].append(lane('a'))
        self.assertEqual(view(p=p)['sources']['plans']['availability'], 'invalid')

    def test_bad_lane_fields_degrade_without_crash(self):
        for health in ([], {}, True, 2):
            with self.subTest(health=health):
                p=plans(); p['lanes'][0]['health']=health
                r=view(p=p)['sources']['plans']
                self.assertIsNone(r['projection']['lanes'][0]['health']); self.assertTrue(r['issues'])

    def test_truncation_is_visible_in_json_and_markdown(self):
        c = clayton(); c['waiting_on_you'] = ['notice']*21
        b = boot(); b['warnings'] = ['warning']*11
        p = plans(); p['lanes'] = [lane('lane-%03d'%i) for i in range(101)]
        v=view(c,b,p)
        self.assertEqual(len(v['sources']['plans']['projection']['lanes']),100)
        self.assertEqual(v['sources']['clayton']['projection']['truncation']['waiting_on_you'],1)
        self.assertIn('omitted entries',w.render_markdown(v))

    def test_source_prose_is_escaped_and_unknown_fields_omitted(self):
        c=clayton(); c['waiting_on_you']=['[run](https://bad.example) <script>\n# ignore\u202e']
        c['token']='DO_NOT_LEAK'; c['headline']='DO_NOT_LEAK'
        v=view(c); md=w.render_markdown(v)
        self.assertNotIn('DO_NOT_LEAK',json.dumps(v)); self.assertNotIn('<script>',md)
        self.assertNotIn('[run](',md); self.assertNotIn('\u202e',md)

    def test_bad_observation_is_not_raw_output(self):
        v=view(observations={'clayton': {'read_error': [], 'sha256':'PRIVATE', 'observed_at':'no'}})
        self.assertNotIn('PRIVATE',json.dumps(v)); self.assertIn('observation_issue_invalid',v['sources']['clayton']['issues'])

    def test_known_checkout_mismatch_without_valid_receipt(self):
        c=clayton(); c.update(repository_checkout_sha=SHA, deployed_checkout_sha='c'*40)
        for receipt in (None, {'installed_at': 'invalid'}):
            with self.subTest(receipt=receipt):
                c['installed_source_receipt']=receipt
                self.assertEqual(view(c)['sources']['clayton']['projection']['installation']['installation_consistency'],'mismatch')

    def test_unknown_state_retains_other_reported_fields(self):
        c=clayton(); c.update(state='NEW-WAITING', pending=3)
        r=view(c)['sources']['clayton']
        self.assertEqual(r['availability'],'available'); self.assertIsNone(r['projection']['reported_state'])
        self.assertEqual(r['projection']['pending'],3)
        c['state']='HALTED (fleet-kill present)'
        self.assertEqual(view(c)['sources']['clayton']['projection']['reported_state'],'HALTED')

    def test_invalid_source_has_no_age_claim(self):
        c=clayton();c['schema']='future/v9';r=view(c)['sources']['clayton']
        self.assertIsNone(r['source_timestamp']);self.assertIsNone(r['age_seconds'])

    def test_receipt_presence_and_future_time(self):
        c=clayton();c['installed_source_receipt']='bad'
        r=view(c)['sources']['clayton']['projection']['installation']
        self.assertEqual(r['receipt_presence'],'invalid');self.assertNotIn('receipt_present',r)
        c.update(repository_checkout_sha=SHA,deployed_checkout_sha=SHA,
            installed_profile_hashes={n:HASH for n in w.PROFILE_NAMES},
            installed_source_receipt={'source_commit':SHA,'deployed_checkout_sha':SHA,
            'installed_at':'2027-01-01T00:00:00Z','profile_hashes':{n:HASH for n in w.PROFILE_NAMES}})
        self.assertEqual(view(c)['sources']['clayton']['projection']['installation']['installation_consistency'],'unknown')

    def test_unregistered_plan_count_without_paths(self):
        p=plans();p['unregistered_plan_files']=['/private/plan1','/private/plan2']
        v=view(p=p)
        self.assertEqual(v['sources']['plans']['projection']['reported_unregistered_plan_count'],2)
        self.assertNotIn('/private/',json.dumps(v))
        self.assertIn('unregistered plan files reported: 2',w.render_markdown(v))

    def test_all_prose_routes_escape_links(self):
        text='[run](https://bad.example) <script>'
        c=clayton();c.update(state='RUNNING',current={'item':text})
        b=boot();b['warnings']=[text]
        p=plans();p['lanes'][0]['step']=text
        md=w.render_markdown(view(c,b,p))
        self.assertNotIn('[run](',md);self.assertNotIn('<script>',md)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, raw):
        p=self.root/name; p.write_bytes(raw); return p

    def test_strict_loader(self):
        cases=[(b'{"x":1,"x":2}','duplicate_key'), (b'{"x":NaN}','nonfinite_number'),
               (b'{"x":Infinity}','nonfinite_number'), (b'{"x":1e999}','nonfinite_number'),
               (b'[]','not_object'),(b'\xff','utf8'),(b'{','malformed_json'),
               (b'['*2000+b']'*2000,'deep_json')]
        for raw,error in cases:
            with self.subTest(error=error,raw=raw[:20]):
                p=self.write('input.json',raw); data,issue,digest=w.load_input(p)
                self.assertIsNone(data)
                if error == "deep_json":
                    self.assertIn(issue, ("malformed_json", "not_object"))
                else:
                    self.assertEqual(issue,error)
                self.assertEqual(digest,hashlib.sha256(raw).hexdigest())

    def test_regular_bounded_files_only(self):
        p=self.write('huge.json',b' '* (w.MAX_INPUT_BYTES+1))
        self.assertEqual(w.load_input(p)[1],'oversize')
        self.assertEqual(w.load_input(self.root)[1],'not_regular')
        self.assertEqual(w.load_input(self.root/'missing')[1],'missing')
        target=self.write('valid.json',b'{}'); link=self.root/'link';link.symlink_to(target)
        self.assertEqual(w.load_input(link)[1],'symlink')
        fifo=self.root/'fifo';os.mkfifo(fifo)
        self.assertEqual(w.load_input(fifo)[1],'not_regular')

    def call(self,args):
        out,err=io.StringIO(),io.StringIO()
        with mock.patch.object(w,'_repo_root',return_value=self.root),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            result=w.main(args)
        return result,out.getvalue(),err.getvalue()

    def test_cli_private_outputs_and_inputs_unchanged(self):
        inp=self.write('input.json',json.dumps(clayton()).encode()); before=inp.read_bytes()
        out=self.root/'state'/'convergence'
        code,text,err=self.call(['--clayton-status',str(inp),'--now',NOW,'--format','json','--out-dir',str(out)])
        self.assertEqual(code,0,err); self.assertEqual(inp.read_bytes(),before)
        v=json.loads(text); self.assertEqual(v,json.loads((out/'work-view.json').read_text()))
        self.assertIn(v['snapshot_id'],(out/'WORK-VIEW.md').read_text())
        for name in ('WORK-VIEW.md','work-view.json'):
            self.assertEqual(stat.S_IMODE((out/name).stat().st_mode),0o600)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode),0o700)
        self.assertFalse(list(out.glob('*.tmp')))

    def test_cli_refuses_outside_symlink_and_input_collisions(self):
        outside=self.root/'elsewhere'
        self.assertEqual(self.call(['--out-dir',str(outside)])[0],2);self.assertFalse(outside.exists())
        state=self.root/'state';state.mkdir();link=state/'link';link.symlink_to(outside)
        self.assertEqual(self.call(['--out-dir',str(link)])[0],2)
        out=state/'view';out.mkdir();inp=out/'work-view.json';inp.write_text('{}')
        self.assertEqual(self.call(['--out-dir',str(out),'--clayton-status',str(inp)])[0],2)
        self.assertEqual(inp.read_text(),'{}')

    def test_cli_handles_missing_and_bad_now(self):
        code,text,_=self.call(['--clayton-status',str(self.root/'missing'),'--format','json','--now',NOW])
        self.assertEqual(code,0);self.assertEqual(json.loads(text)['sources']['clayton']['availability'],'missing')
        self.assertEqual(self.call(['--now','2026-09-05'])[0],2)

    def test_atomic_write_failure_preserves_old_target(self):
        p=self.write('old.json',b'old')
        with mock.patch.object(w.os,'replace',side_effect=OSError('synthetic')):
            with self.assertRaises(w.WorkViewError):w._atomic_write(p,'new')
        self.assertEqual(p.read_bytes(),b'old')
        self.assertFalse((self.root/'.old.json.tmp').exists())

    def test_link_parent_path_cannot_bypass_symlink_refusal(self):
        actual=self.root/'actual';actual.mkdir();child=actual/'child';child.mkdir()
        (actual/'input.json').write_text('{}')
        link=self.root/'link';link.symlink_to(child)
        self.assertEqual(w.load_input(link/'..'/'input.json')[1],'symlink')

    def test_replay_records_real_read_time_and_markdown_snapshot(self):
        inp=self.write('input.json',json.dumps(clayton()).encode());out=self.root/'state'/'view'
        code,text,err=self.call(['--now','2020-01-01T00:00:00Z','--out-dir',str(out),
            '--clayton-status',str(inp),'--format','markdown'])
        self.assertEqual(code,0,err);v=json.loads((out/'work-view.json').read_text())
        self.assertNotEqual(v['sources']['clayton']['observed_at'],'2020-01-01T00:00:00Z')
        self.assertEqual(text,(out/'WORK-VIEW.md').read_text());self.assertIn(v['snapshot_id'],text)

    def test_stale_temporary_does_not_block_new_write(self):
        p=self.root/'view.json';oldtmp=self.root/'.view.json.tmp';oldtmp.write_text('interrupted')
        w._atomic_write(p,'new')
        self.assertEqual(p.read_text(),'new');self.assertEqual(oldtmp.read_text(),'interrupted')

    def test_invalid_path_returns_safe_issue(self):
        self.assertEqual(w.load_input('bad\x00path')[1],'read_error')

    def test_unknown_user_path_is_safe(self):
        with mock.patch.object(Path,'expanduser',side_effect=RuntimeError('private diagnostic')):
            self.assertEqual(w.load_input('~unknown/input.json')[1],'read_error')
            code,out,err=self.call(['--out-dir','~unknown/state/out'])
            self.assertEqual(code,2);self.assertNotIn('private diagnostic',err)


if __name__=='__main__':
    unittest.main()
