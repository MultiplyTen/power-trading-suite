import sqlite3
import unittest
from reconciliation import SCHEMA, reconcile


class ReviewCutoff(unittest.TestCase):
    def test_reconciliation_uses_configured_hour_and_minute(self):
        with sqlite3.connect(':memory:') as db:
            db.executescript(SCHEMA)
            for rid, published in [('timely', '2026-07-31 12:15:00'), ('late', '2026-07-31 12:45:00')]:
                db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',
                           (rid, 'load', published, 'test', '{}', 'synthetic', rid, 'production'))
                db.execute('INSERT INTO predictions VALUES(?,?,?,?,?,?,?)',
                           (rid, '2026-08-01 00:00:00', 10., None, None, None, None))
            db.execute('INSERT INTO actuals VALUES(?,?,?,?,?,?,?)',
                       ('2026-08-01 00:00:00', 'load', 12., '2026-08-02', 'synthetic', 'actual', '2026-08-02'))
            config = {'review_date': '2026-08-10', 'actual_lag_days': 5,
                      'decision_cutoff_hour': 12, 'decision_cutoff_minute': 30}
            done, _, _ = reconcile(db, config)
        result = done.set_index('run_id').late_publication
        self.assertFalse(result['timely'])
        self.assertTrue(result['late'])
