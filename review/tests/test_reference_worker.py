"""Exercise the worker/CSV/hash chain without database access or model training."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from candidate_checks import risk_validation_guard


class ReferenceWorkerTests(unittest.TestCase):
    def test_worker_binds_original_reference_and_rejects_changed_baseline(self):
        root = Path(__file__).resolve().parents[2]
        worker = root / 'review/tuning_worker.py'
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            idx = pd.date_range('2026-04-01', '2026-08-10 23:00:00', freq='h')
            h = pd.DataFrame({'da': 300., 'rt': 350., 'da_hat': 300., 'rt_hat': 350.,
                              'actual_load': 12., 'load_hat': 10., 'spread_error': 0.}, index=idx)
            history = tmp / 'history.csv'; h.to_csv(history, index_label='timestamp')
            baseline = tmp / 'baseline.csv'
            job = {'project_root': str(root / 'coefficient_opt'), 'task': 'risk', 'parameter': None,
                   'value': None, 'start': '2026-08-01', 'end': '2026-08-10', 'lag': 5,
                   'output': str(baseline), 'history': str(history)}
            def execute(name, data):
                path = tmp / (name + '.json'); path.write_text(json.dumps(data))
                return subprocess.run([sys.executable, str(worker), str(path)], cwd=root,
                                      capture_output=True, text=True, timeout=60)
            result = execute('baseline', job)
            self.assertEqual(result.returncode, 0, result.stderr)
            sha = hashlib.sha256(baseline.read_bytes()).hexdigest()
            candidate = tmp / 'candidate.csv'
            trial = dict(job, parameter='confidence', value=.85, output=str(candidate),
                         reference_risk=str(baseline), reference_risk_sha256=sha)
            result = execute('candidate', trial)
            self.assertEqual(result.returncode, 0, result.stderr)
            b = pd.read_csv(baseline, parse_dates=['timestamp']).set_index('timestamp')
            n = pd.read_csv(candidate, parse_dates=['timestamp']).set_index('timestamp')
            b.attrs['artifact_sha256'] = sha
            self.assertTrue(n.reference_confidence.eq(.9).all())
            self.assertTrue(n.reference_error_shrink.eq(1.).all())
            self.assertTrue(n.reference_baseline_sha256.eq(sha).all())
            self.assertTrue(b.reference_scenario_hash.equals(n.reference_scenario_hash))
            self.assertEqual(risk_validation_guard(b, n)['status'], 'passed')
            broken = dict(trial, output=str(tmp / 'bad.csv'), reference_risk_sha256='0' * 64)
            result = execute('bad-reference', broken)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('哈希不匹配', result.stderr)


if __name__ == '__main__':
    unittest.main()
