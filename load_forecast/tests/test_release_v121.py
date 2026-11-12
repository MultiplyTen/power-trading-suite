"""Release regressions: input completeness, causal disclosure, model labels."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from configuration import read_config
from evaluation.disclosure import read_disclosure_features, disclosure_target_coverage
from evaluation.rolling_backtest import load_object
from inputs.quality import readiness, SPECS
from weather.features import VARS

ROOT = Path(__file__).resolve().parents[1]


class ReleaseRegressions(unittest.TestCase):
    def _coverage_config(self, directory, rows):
        path = Path(directory) / 'test.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE disclosure_hourly(target_date TEXT, target_start TEXT, metric TEXT, value REAL, ingested_at TEXT, source_id TEXT, record_kind TEXT, quality_status TEXT)')
            db.executemany('INSERT INTO disclosure_hourly VALUES(?,?,?,?,?,?,?,?)', rows)
        return {'_root': directory, 'db_path': str(path), 'run': {'decision_cutoff_hour': 12, 'decision_cutoff_minute': 30}}

    def _full_coverage_rows(self, received='2026-10-14 12:00'):
        return [
            ('2026-10-15', str(target), f'metric_{metric}', 100., received,
             'original', 'forecast', 'complete')
            for metric in range(10)
            for target in pd.date_range('2026-10-15', periods=24, freq='h')
        ]

    def test_empty_inputs_block_instead_of_passing(self):
        c = read_config(ROOT / 'config.historical.yaml')
        issues = readiness(c, {name: pd.DataFrame() for name in SPECS}, pd.Timestamp('2026-08-03'))
        self.assertEqual({x['object'] for x in issues}, set(SPECS))

    def test_disclosure_late_revision_and_backfill_cannot_change_old_features(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE disclosure_hourly(target_start TEXT, metric TEXT, value REAL, ingested_at TEXT, source_id TEXT, record_kind TEXT, quality_status TEXT)')
                row = ('2026-10-15 00:00', 'supply', 100., '2026-10-14 12:00', 'original', 'forecast', 'complete')
                db.execute('INSERT INTO disclosure_hourly VALUES(?,?,?,?,?,?,?)', row)
            c = {'_root': temp, 'db_path': str(path), 'run': {'decision_cutoff_hour': 12, 'decision_cutoff_minute': 30}}
            before = read_disclosure_features(c)
            with sqlite3.connect(path) as db:
                db.executemany('INSERT INTO disclosure_hourly VALUES(?,?,?,?,?,?,?)', [
                    ('2026-10-15 00:00', 'supply', 999., '2026-10-14T04:31:00+00:00', 'late', 'forecast', 'complete'),
                    ('2026-10-15 01:00', 'supply', 999., '2026-11-01 12:00', 'backfill', 'forecast', 'complete'),
                    ('2026-10-15 02:00', 'supply', 999., None, 'unknown', 'forecast', 'complete'),
                ])
            pd.testing.assert_frame_equal(before, read_disclosure_features(c))
            self.assertEqual(before.iloc[0]['disc_supply'], 100.)

    def test_coverage_backfill_is_unavailable_like_model_features(self):
        for received in ('2026-11-01 12:00', '2026-10-14T04:31:00+00:00', None):
            with self.subTest(received=received), tempfile.TemporaryDirectory() as temp:
                c = self._coverage_config(temp, self._full_coverage_rows(received))
                self.assertTrue(read_disclosure_features(c).empty)
                coverage = disclosure_target_coverage(c, '2026-10-15')
                self.assertEqual(coverage['status'], 'unavailable')
                self.assertEqual(coverage['complete_metric_count'], 0)
                self.assertEqual(sum(row['complete_hours'] for row in coverage['metrics']), 0)

    def test_coverage_uses_latest_eligible_version_and_unique_hours(self):
        with tempfile.TemporaryDirectory() as temp:
            rows = self._full_coverage_rows()
            rows += [tuple(list(row[:3]) + [999., '2026-10-14 12:31', 'late'] + list(row[6:])) for row in rows[:24]]
            rows += [('2026-10-15', '2026-10-15 00:00:00', 'metric_0', 200.,
                      '2026-10-14 12:30', 'eligible_revision', 'forecast', 'complete')]
            c = self._coverage_config(temp, rows)
            coverage = disclosure_target_coverage(c, '2026-10-15')
            self.assertEqual(coverage['status'], 'complete')
            self.assertEqual(coverage['complete_metric_count'], 10)
            self.assertTrue(all(row['hours'] == row['complete_hours'] == 24 for row in coverage['metrics']))
            self.assertEqual(read_disclosure_features(c).loc['2026-10-15 00:00', 'disc_metric_0'], 200.)

    def test_duplicate_or_off_grid_rows_do_not_count_as_full_coverage(self):
        for off_grid in (False, True):
            with self.subTest(off_grid=off_grid), tempfile.TemporaryDirectory() as temp:
                rows = self._full_coverage_rows()
                rows = [tuple([row[0], str(pd.Timestamp(row[1]) + pd.Timedelta(minutes=30)) if off_grid else '2026-10-15 00:00:00'] + list(row[2:])) for row in rows]
                c = self._coverage_config(temp, rows)
                coverage = disclosure_target_coverage(c, '2026-10-15')
                self.assertEqual(coverage['status'], 'unavailable')
                self.assertEqual(coverage['complete_metric_count'], 0)
                self.assertTrue(all(row['hours'] <= 1 for row in coverage['metrics']))

    def test_coverage_missing_table_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            c = {'_root': temp, 'db_path': str(Path(temp) / 'test.db')}
            coverage = disclosure_target_coverage(c, '2026-10-15')
            self.assertEqual(coverage['status'], 'unavailable')
            self.assertEqual(coverage['metrics'], [])

    def test_fixed_model_is_not_labeled_validation_selected(self):
        c = read_config(ROOT / 'config.historical.yaml')
        c['training']['selection_mode'] = 'fixed'
        idx = pd.date_range('2026-01-01', '2026-08-31 23:00', freq='h')
        s = pd.Series(100., index=idx)
        weather = pd.DataFrame(1., index=idx, columns=VARS)
        def walk_stub(f, x, *args, **kwargs):
            return pd.Series(100., index=f.index), None, []
        with patch('evaluation.rolling_backtest.walk', side_effect=walk_stub):
            _, _, meta, _, _, _ = load_object('portfolio', s, weather, c, '2026-06-01', '2026-08-31', c['models']['portfolio'])
        self.assertEqual(meta['selection_status'], 'fixed_specification')


if __name__ == '__main__':
    unittest.main()
