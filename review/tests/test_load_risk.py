import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from load_risk import diagnose, run
from reconciliation import SCHEMA


class LoadRiskTests(unittest.TestCase):
    def paired(self, days=7):
        index = pd.date_range('2026-08-01', periods=24 * days, freq='h')
        # Deliberate +2 bias and nonzero dispersion, to distinguish bias from uncertainty.
        errors = np.tile(np.r_[[1.] * 12, [3.] * 12], days)
        return pd.DataFrame({'load_hat': 10., 'actual_load': 10. + errors,
                             'model_version': 'model-a', 'run_id': 'original',
                             'actual_load_source_sha': 'actual-v1'}, index=index)

    def test_bias_uncertainty_and_k1_exposure_remain_separate(self):
        with patch('load_risk._day_type', return_value='workday'):
            report, tables = diagnose(self.paired())
        s = report['summary']
        self.assertEqual(s['status'], 'sufficient_descriptive_sample')
        self.assertEqual(s['sample_hours'], 168)
        self.assertEqual(s['complete_days'], 7)
        self.assertAlmostEqual(s['signed_bias_mwh'], 2.)
        self.assertAlmostEqual(s['wmape'], 2. / 12.)
        self.assertAlmostEqual(s['error_p10_mwh'], 1.)
        self.assertAlmostEqual(s['centered_residual_p10_mwh'], -1.)
        self.assertAlmostEqual(s['k1_total_absolute_deviation_mwh'], 336.)
        self.assertFalse(report['automatic_correction'])
        self.assertEqual(len(tables['hourly']), 24)
        self.assertEqual(tables['hourly'].iloc[0]['sample_hours'], 7)

    def test_few_or_partial_days_do_not_become_zero_risk(self):
        report, _ = diagnose(self.paired(2))
        self.assertEqual(report['status'], 'insufficient_sample')
        self.assertIsNone(report['summary']['centered_residual_p90_mwh'])
        self.assertIsNone(report['summary']['bias_correction_candidate_mwh'])
        partial = self.paired().iloc[::24]
        report, _ = diagnose(partial)
        self.assertEqual(report['summary']['complete_days'], 0)
        self.assertIsNone(report['summary']['error_p90_mwh'])
        empty = self.paired().iloc[:0]
        report, _ = diagnose(empty)
        self.assertIsNone(report['summary']['k1_total_absolute_deviation_mwh'])

    def test_duplicate_target_is_rejected_and_versions_are_not_pooled_invisibly(self):
        f = self.paired()
        with self.assertRaisesRegex(ValueError, '重复'):
            diagnose(pd.concat([f, f.iloc[:1]]))
        f.loc[f.index >= '2026-08-04', 'model_version'] = 'model-b'
        report, tables = diagnose(f, {'version_selection': 'retained'})
        self.assertEqual(report['summary']['selected_model_version_count'], 2)
        self.assertEqual(len(tables['model_versions']), 2)
        self.assertEqual(report['pairing_audit']['version_selection'], 'retained')

    def test_original_predecision_pairing_and_output_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / 'review.db'
            db = sqlite3.connect(path); db.executescript(SCHEMA)
            for rid, published, status, prediction in [
                ('early', '2026-07-31 10:00:00', 'research', 8.),
                ('chosen', '2026-07-31 12:00:00', 'research', 10.),
                ('late', '2026-07-31 13:00:00', 'research', 99.),
                ('replay', '2026-07-31 12:15:00', 'research_replay', 77.)]:
                db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',
                           (rid, 'load', published, 'model-a', '{}', 'model_memory', rid, status))
                for t in pd.date_range('2026-08-01', periods=24, freq='h'):
                    db.execute('INSERT INTO predictions VALUES(?,?,?,?,?,?,?)', (rid, str(t), prediction, None, None, None, None))
            for t in pd.date_range('2026-08-01', periods=24, freq='h'):
                for actual, available, digest in [(12., '2026-08-02 12:00:00', 'asof'), (88., '2026-08-10 12:00:00', 'future')]:
                    db.execute('INSERT INTO actuals VALUES(?,?,?,?,?,?,?)',
                               (str(t), 'load', actual, available, 'verified', digest, available))
            db.commit(); db.close()
            c = {'_root': tmp, 'db_path': 'review.db', 'output_dir': 'output', 'review_date': '2026-08-03',
                 'decision_cutoff_hour': 12, 'decision_cutoff_minute': 30}
            report = run(c)
            self.assertEqual(report['summary']['sample_hours'], 24)
            self.assertAlmostEqual(report['summary']['signed_bias_mwh'], 2.)
            self.assertEqual(report['status'], 'insufficient_sample')
            paired = pd.read_csv(root / 'output' / report['files']['paired_hours'])
            self.assertEqual(set(paired.run_id), {'chosen'})
            self.assertEqual(set(paired.actual_load), {12.})
            self.assertEqual(paired.target.nunique(), 24)
            saved = json.loads((root / 'output' / report['files']['json']).read_text())
            self.assertEqual(saved['error_convention'], 'actual_load_minus_forecast_load')
            self.assertEqual(len(saved['tables']['hourly']), 24)


if __name__ == '__main__':
    unittest.main()
