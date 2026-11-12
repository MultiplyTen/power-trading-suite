# -*- coding: utf-8 -*-
"""Synthetic SQLite evidence for original-publication/as-of error contracts."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from database.forecast_errors import (
    complete_m2_publications, paired_forecast_errors, select_joint_error_history,
)
from database.trading import published_load_forecast


class OriginalPublicationErrors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'synthetic.sqlite'
        with sqlite3.connect(self.db) as d:
            # No target PK here: the reader must defend against duplicate raw targets.
            d.executescript('''CREATE TABLE runs(id TEXT PRIMARY KEY,kind TEXT,published_at TEXT,
                model_version TEXT,config_json TEXT,source TEXT,sha TEXT,status TEXT);
                CREATE TABLE predictions(run_id TEXT,target TEXT,load REAL,da REAL,rt REAL,k REAL,research_k REAL);
                CREATE TABLE actuals(target TEXT,metric TEXT,value REAL,available_at TEXT,evidence TEXT,source_sha TEXT,ingested_at TEXT);''')
        self.version = 'load-suite-v1.2.1-calendar'

    def forecast(self, day, rid='original', hour=11, minute=0, load=10,
                 version=None, status='research', mode='forecast', missing=None,
                 published=None, duplicate=False):
        day = pd.Timestamp(day)
        published = published or str(day - pd.Timedelta(days=1) + pd.Timedelta(hours=hour, minutes=minute))
        with sqlite3.connect(self.db) as d:
            d.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',
                (rid, 'load', published, version or self.version,
                 json.dumps({'run': {'mode': mode}}), 'model_memory', rid + '-sha', status))
            rows = [(rid, str(t), load + t.hour / 100., 100. + t.hour, 110. + t.hour, None, None)
                    for t in pd.date_range(day, periods=24, freq='h') if t.hour != missing]
            d.executemany('INSERT INTO predictions VALUES(?,?,?,?,?,?,?)', rows)
            if duplicate:
                d.execute('INSERT INTO predictions VALUES(?,?,?,?,?,?,?)', rows[0])

    def actuals(self, day, missing=None, lag=2, suffix='original',
                available=None, ingested=None, load=12):
        day = pd.Timestamp(day)
        available = available or str(day + pd.Timedelta(days=lag))
        ingested = ingested or available
        with sqlite3.connect(self.db) as d:
            for t in pd.date_range(day, periods=24, freq='h'):
                for metric, value in [('load', load + t.hour / 100.), ('da', 103. + t.hour), ('rt', 117. + t.hour)]:
                    if missing == (metric, t.hour):
                        continue
                    d.execute('INSERT INTO actuals VALUES(?,?,?,?,?,?,?)',
                        (str(t), metric, value, available, 'synthetic_verified',
                         suffix + '-' + metric, ingested))

    def pair(self, day='2026-04-01', asof='2026-04-10 12:30', **kw):
        return paired_forecast_errors(self.db, day, day, asof, **kw)

    def test_latest_complete_run_excludes_late_replay_and_partial(self):
        day = '2026-04-01'
        self.forecast(day, 'early', hour=9, load=9)
        self.forecast(day, 'selected', hour=11, load=10)
        self.forecast(day, 'partial', hour=12, load=800, missing=23)
        self.forecast(day, 'late', hour=12, minute=31, load=900)
        self.forecast(day, 'load_replay_fake_status', hour=12, load=700)
        self.forecast(day, 'replay_status', hour=12, status='research_replay', load=600)
        self.actuals(day)
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        f, a = self.pair()
        self.assertEqual(len(f), 24)
        self.assertEqual(set(f.run_id), {'selected'})
        self.assertEqual(set(f.model_version), {self.version})
        np.testing.assert_allclose(f.load_error, 2.)
        np.testing.assert_allclose(f.da_error, 3.)
        np.testing.assert_allclose(f.rt_error, 7.)
        np.testing.assert_allclose(f.spread_error, -4.)
        self.assertEqual(a['run_exclusion_counts']['replay'], 2)
        self.assertEqual(a['run_exclusion_counts']['late_publication'], 1)
        self.assertEqual(a['run_exclusion_counts']['incomplete_publication_day'], 1)
        self.assertEqual(a['complete_pair_days'], 1)
        self.assertEqual(a['daily'][0]['eligible_complete_runs'], 2)
        self.assertEqual(a['daily'][0]['publication_revision_change']['load']['sum_hourly_change'], 24.)
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_missing_hour_is_not_spliced_across_publications(self):
        self.forecast('2026-04-01', 'one', missing=0)
        self.forecast('2026-04-01', 'two', hour=12, missing=23)
        self.actuals('2026-04-01')
        f, a = self.pair()
        self.assertTrue(f.empty)
        self.assertEqual(a['missing_publication_days'], 1)

    def test_duplicate_target_and_backtest_are_rejected(self):
        self.forecast('2026-04-01', 'duplicates', duplicate=True)
        self.forecast('2026-04-01', 'backtest', hour=12, mode='backtest')
        f, a = self.pair()
        self.assertTrue(f.empty)
        self.assertEqual(a['run_exclusion_counts']['duplicate_target'], 1)
        self.assertEqual(a['run_exclusion_counts']['not_forecast_run'], 1)

    def test_tied_latest_runs_are_not_arbitrarily_chosen(self):
        self.forecast('2026-04-01', 'same_time_one')
        self.forecast('2026-04-01', 'same_time_two', load=99)
        f, a = self.pair()
        self.assertTrue(f.empty)
        self.assertEqual(a['daily'][0]['status'], 'ambiguous_latest_publication')

    def test_actual_revision_and_receipt_cannot_arrive_after_asof(self):
        day = '2026-04-01'
        self.forecast(day)
        self.actuals(day)
        self.actuals(day, suffix='late_available', available='2026-04-11', ingested='2026-04-11', load=900)
        self.actuals(day, suffix='late_receipt', available='2026-04-05', ingested='2026-04-11', load=800)
        f, a = self.pair()
        np.testing.assert_allclose(f.load_error, 2.)
        self.assertEqual(set(f.actual_load_source_sha), {'original-load'})
        self.assertEqual(a['actual_row_exclusion_counts']['actual_not_available_at_asof'], 144)
        selected = [x for x in a['actual_version_selection'] if x['metric'] == 'load']
        self.assertTrue(all(x['eligible_versions'] == 1 and x['total_versions'] == 3 for x in selected))

    def test_current_available_actual_revision_is_used(self):
        self.forecast('2026-04-01')
        self.actuals('2026-04-01')
        self.actuals('2026-04-01', suffix='known_revision', available='2026-04-05', load=13)
        f, _ = self.pair()
        np.testing.assert_allclose(f.load_error, 3.)
        self.assertEqual(set(f.actual_load_source_sha), {'known_revision-load'})

    def test_complete_day_policy_and_partial_load_diagnostic(self):
        self.forecast('2026-04-01')
        self.actuals('2026-04-01', missing=('load', 7))
        f, a = self.pair()
        self.assertTrue(f.empty)
        self.assertEqual(a['partial_pair_days'], 1)
        f, a = self.pair(metrics=('load',), complete_actual_days=False)
        self.assertEqual(len(f), 23)
        self.assertEqual(a['daily'][0]['missing_actual_hours'], {'load': 1})

    def test_incomplete_price_does_not_remove_load_only_diagnostic(self):
        self.forecast('2026-04-01')
        self.actuals('2026-04-01', missing=('rt', 7))
        f, _ = self.pair()
        self.assertTrue(f.empty)
        f, a = self.pair(metrics=('load',), complete_actual_days=False)
        self.assertEqual(len(f), 24)
        self.assertEqual(a['complete_pair_days'], 1)

    def test_model_version_filter_does_not_call_old_errors_current(self):
        self.forecast('2026-04-01', 'older_model', version='M2-old')
        self.actuals('2026-04-01')
        f, a = self.pair(model_version=self.version)
        self.assertTrue(f.empty)
        self.assertEqual(a['observed_run_model_versions'], ['M2-old'])
        self.assertEqual(a['run_exclusion_counts']['model_version_mismatch'], 1)

    def test_timezone_cutoff_and_original_target_reader(self):
        self.forecast('2026-04-01', 'on_time_utc', published='2026-03-31T04:30:00+00:00')
        self.forecast('2026-04-01', 'late_utc', published='2026-03-31T04:31:00+00:00', load=99)
        f, meta = published_load_forecast('2026-04-01', self.db)
        self.assertEqual(len(f), 24)
        self.assertEqual(meta['run_id'], 'on_time_utc')
        self.assertEqual(meta['forecast_cutoff'], '2026-03-31 12:30:00')

    def test_day_ahead_actual_can_be_known_before_target_hour(self):
        self.forecast('2026-04-01')
        self.actuals('2026-04-01')
        with sqlite3.connect(self.db) as d:
            d.execute("UPDATE actuals SET available_at='2026-03-31 18:00:00',ingested_at='2026-03-31 18:00:00' WHERE metric='da'")
        f, _ = self.pair()
        self.assertEqual(len(f), 24)

    def test_negative_rebased_days_do_not_meet_original_sample_gate(self):
        idx = pd.date_range('2026-04-01', periods=30 * 24, freq='h')
        original = pd.DataFrame({'load_hat': 100., 'actual_load': 1.,
            'da_hat': 100., 'actual_da': 101., 'rt_hat': 100., 'actual_rt': 102.,
            'model_version': self.version}, index=idx)
        proxy = pd.DataFrame({'load_hat': 2., 'actual_load': 2., 'spread_error': 0.}, index=idx)
        _, m = select_joint_error_history(proxy, original, {}, np.full(24, 10.), self.version)
        self.assertEqual(m['same_version_complete_days'], 30)
        self.assertEqual(m['same_version_nonnegative_scenario_days'], 0)
        self.assertEqual(m['source'], 'proxy_research_fallback')
        with self.assertRaisesRegex(ValueError, '可用日仅0'):
            select_joint_error_history(proxy, original, {}, np.full(24, 10.), self.version, mode='require_m2_publications')

    def test_thirty_day_gate_and_explicit_proxy_fallback(self):
        for i, day in enumerate(pd.date_range('2026-04-01', periods=30, freq='D')):
            self.forecast(day, 'run-%02d' % i)
            self.actuals(day)
        original, a = paired_forecast_errors(self.db, '2026-04-01', '2026-04-30', '2026-05-10', model_version=self.version)
        # A 90-day proxy remains separately available for compatibility.
        proxy = pd.DataFrame({'load_hat': 10., 'actual_load': 11., 'da': 100., 'rt': 110.,
            'da_hat': 100., 'rt_hat': 110., 'spread_error': 0., 'da_error': 0., 'rt_error': 0.},
            index=pd.date_range('2025-12-01', periods=90 * 24, freq='h'))
        f, m = select_joint_error_history(proxy, original, a, np.full(24, 10.), self.version)
        self.assertEqual(m['source'], 'original_m2_same_version')
        self.assertEqual(len(f), 30 * 24)
        self.assertFalse(m['mixed_error_sources'])
        self.assertEqual(m['same_version_nonnegative_scenario_days'], 30)
        np.testing.assert_allclose(f.spread_error, -4.)
        few = original.iloc[:-24]
        f, m = select_joint_error_history(proxy, few, a, np.full(24, 10.), self.version)
        self.assertEqual(m['source'], 'proxy_research_fallback')
        self.assertEqual(len(f), 90 * 24)
        self.assertFalse(m['current_model_original_errors_used'])
        with self.assertRaisesRegex(ValueError, '29'):
            select_joint_error_history(proxy, few, a, np.full(24, 10.), self.version, mode='require_m2_publications')
        # Even 30 days from a prior model cannot be promoted to current calibration.
        f, m = select_joint_error_history(proxy, original, a, np.full(24, 10.), 'new-model')
        self.assertEqual(m['source'], 'proxy_research_fallback')
        self.assertEqual(m['same_version_complete_days'], 0)

    def test_original_joint_pairs_drive_finite_grid_and_exact_risk(self):
        from coefficient_opt.coefficient_optimization import optimize, optimize_exact
        import yaml
        for i, day in enumerate(pd.date_range('2026-04-01', periods=30, freq='D')):
            self.forecast(day, 'original-risk-%02d' % i)
            self.actuals(day, load=12 + (i % 7 - 3) * .8)
            with sqlite3.connect(self.db) as d:
                d.execute("UPDATE actuals SET value=value+? WHERE metric='rt' AND substr(target,1,10)=?",
                          ((i % 9 - 4) * 10, str(day.date())))
        original, audit = paired_forecast_errors(self.db, '2026-04-01', '2026-04-30',
            '2026-05-10 12:30', model_version=self.version)
        # A deliberately different proxy proves that original errors reach risk.
        proxy = pd.DataFrame({'load_hat': 1., 'actual_load': 1., 'spread_error': 0.},
            index=pd.date_range('2025-12-01', periods=90 * 24, freq='h'))
        selected, metadata = select_joint_error_history(proxy, original, audit,
            np.full(24, 10.), self.version, mode='require_m2_publications')
        self.assertEqual(metadata['source'], 'original_m2_same_version')
        self.assertEqual(set(selected.run_id), {'original-risk-%02d' % i for i in range(30)})
        f = np.full((30, 24), 10.)
        l = f + (selected.actual_load - selected.load_hat).to_numpy().reshape(30, 24)
        spread = 7. + selected.spread_error.to_numpy().reshape(30, 24)
        std = selected.groupby(selected.index.hour).spread_error.std().to_numpy()
        config = yaml.safe_load((Path(__file__).resolve().parents[1] / 'coefficient_opt/config.yaml').read_text())
        k, info = optimize(f, l, spread, config, std, limit=1e9)
        exact, exact_info = optimize_exact(f, l, spread, config, std, limit=1e9, time_limit=10.)
        for coefficients, risk in [(k, info), (exact, exact_info)]:
            self.assertEqual(coefficients.shape, (24,))
            self.assertTrue(np.isfinite(coefficients).all())
            self.assertTrue(np.isfinite(risk['cvar']))
            self.assertTrue(np.isfinite(risk['mean']))
            self.assertTrue(np.isfinite(risk['objective']))
            self.assertLessEqual(np.abs(np.diff(coefficients)).max(), config['smooth_limit'] + 1e-7)
        self.assertGreaterEqual(exact_info['objective'], info['objective'] - 1e-6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
