import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sweep_clock as c

T = dt.datetime(2026, 9, 22, 20, 1, tzinfo=dt.timezone.utc)

def sample(seconds=0, **changes):
    value = {'utc': (T + dt.timedelta(seconds=seconds)).isoformat(),
             'monotonic_ns': 10**12 + int(seconds * 1e9), 'boot_id': 'boot-a', 'host': 'MacBook-Pro-2.local'}
    value.update(changes); return value


def clock(delay=0):
    return {'schema': c.SCHEMA, 'run_id': 'run-a', 'owner_task_id': 'owner-a',
            'trigger_at': (T - dt.timedelta(seconds=delay)).isoformat(), 'trigger_ref': 'actual-trigger-a', 'initialized': sample()}


class ClockTests(unittest.TestCase):
    def test_late_start_does_not_get_fresh_budget(self):
        result = c.admission(clock(6068), sample(), 'collection', 10, 300)
        self.assertFalse(result['admitted']); self.assertEqual(result['remaining_seconds'], 0)
        self.assertIn('total_budget_expired', result['reasons'])

    def test_delayed_initialization_counts_before_and_after_restart(self):
        self.assertEqual(c.status(clock(35), sample(40))['elapsed_seconds'], 75)
        serialized = json.loads(json.dumps(clock(35)))
        self.assertEqual(c.status(serialized, sample(70))['elapsed_seconds'], 105)

    def test_boot_host_or_clock_regression_refuses(self):
        for current in [sample(5, boot_id='boot-b'), sample(5, host='mini'), sample(-1), sample(20, utc=sample(2)['utc'])]:
            with self.subTest(current=current), self.assertRaises(ValueError): c.status(clock(), current)

    def test_suspend_wall_time_never_extends_budget(self):
        current = sample(5, utc=sample(1000)['utc'])
        self.assertEqual(c.status(clock(), current)['elapsed_seconds'], 1000)
        self.assertFalse(c.admission(clock(), current, 'dispatch', 10, 300)['admitted'])

    def test_optional_cutoff_and_fit(self):
        self.assertTrue(c.admission(clock(), sample(599), 'preparation', 1, 300)['admitted'])
        self.assertFalse(c.admission(clock(), sample(600), 'preparation', 1, 300)['admitted'])
        self.assertFalse(c.admission(clock(), sample(550), 'dispatch', 51, 300)['admitted'])
        self.assertFalse(c.admission(clock(), sample(0), 'collection', 10, 299)['admitted'])

    def test_publication_preserves_refresh_and_close_reserve(self):
        self.assertTrue(c.admission(clock(), sample(419), 'feed_publish', 1, 480)['admitted'])
        self.assertFalse(c.admission(clock(), sample(421), 'feed_publish', 1, 480)['admitted'])
        self.assertFalse(c.admission(clock(), sample(100), 'feed_publish', 10, 479)['admitted'])

    def test_priority_needs_frozen_outcome_and_original_allowance(self):
        self.assertFalse(c.admission(clock(), sample(700), 'priority', 10, 120)['admitted'])
        self.assertTrue(c.admission(clock(), sample(770), 'priority', 10, 120, outcome_verified=True)['admitted'])
        self.assertFalse(c.admission(clock(), sample(781), 'priority', 1, 120, outcome_verified=True)['admitted'])

    def test_finalization_has_transport_reserve_and_no_late_normal_work(self):
        self.assertTrue(c.admission(clock(), sample(849), 'finalization', 1, 50)['admitted'])
        for elapsed in (850, 899, 900, 950):
            self.assertFalse(c.admission(clock(), sample(elapsed), 'finalization', 1, 50)['admitted'])

    def test_nonfinite_negative_unknown_duration_is_not_admitted(self):
        for value in (None, True, '10', 0, -1, float('nan'), float('inf'), 10**1000):
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError):
                c.admission(clock(), sample(), 'collection', value, 300)

    def test_capture_is_cumulative_and_does_not_reset_total(self):
        capture = {'started': sample(20)}
        self.assertTrue(c.admission(clock(), sample(310), 'collection', 10, 300, capture=capture)['admitted'])
        self.assertFalse(c.admission(clock(), sample(310), 'collection', 11, 300, capture=capture)['admitted'])
        self.assertFalse(c.admission(clock(400), sample(220), 'collection', 10, 300, capture=capture)['admitted'])

    def test_capture_cannot_precede_run_monotonic_sample(self):
        capture = {"started": sample(0, monotonic_ns=10**12 - 1)}
        with self.assertRaises(ValueError):
            c.admission(clock(), sample(2), "collection", 10, 300, capture=capture)

    def test_init_and_capture_cannot_replace_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp).resolve(); os.chmod(run, 0o700)
            first = c.initialize(str(run), T.isoformat(), 'trigger', 'owner', sample())
            original = (run / 'sweep-clock.json').read_bytes()
            with self.assertRaises(FileExistsError): c.initialize(str(run), sample(20)['utc'], 'new', 'owner', sample(20))
            self.assertEqual(original, (run / 'sweep-clock.json').read_bytes())
            capture = c.begin_capture(str(run), first['clock_sha256'], 'notion', sample(5))
            self.assertEqual(capture['capture']['started'], sample(5))
            with self.assertRaises(FileExistsError): c.begin_capture(str(run), first['clock_sha256'], 'notion', sample(25))
            with self.assertRaises(ValueError): c.load_clock(str(run), '0'*64)

    def test_future_trigger_and_nonprivate_directory_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp).resolve()
            with self.assertRaises(ValueError): c.initialize(str(run), sample(1)['utc'], 'trigger', 'owner', sample())
            os.chmod(run, 0o755)
            with self.assertRaises(ValueError): c.initialize(str(run), T.isoformat(), 'trigger', 'owner', sample())

    def test_symlink_and_duplicate_keys_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp).resolve(); f=run/'real.json'; f.write_text('{"x":1,"x":2}'); f.chmod(0o600)
            with self.assertRaises(ValueError): c.read(f)
            link=run/'alias.json'; link.symlink_to(f)
            with self.assertRaises(ValueError): c.read(link)

    def test_cli_unknown_bound_returns_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp).resolve(); result=c.initialize(str(run), T.isoformat(), 'trigger', 'owner', sample())
            args=['clock','--run-dir',str(run),'check','--clock-sha256',result['clock_sha256'],'--phase','dispatch','--maximum-call-seconds','nan','--closure-reserve-seconds','300']
            with patch('sys.argv',args), patch.object(c,'sample',return_value=sample(2)), patch('builtins.print'):
                self.assertEqual(c.main(),2)

if __name__ == '__main__': unittest.main()
