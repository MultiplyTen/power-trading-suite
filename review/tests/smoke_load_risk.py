"""Offline synthetic M4 diagnostic/report smoke, with no model run or config apply.

Run from the repository root with --output pointing to a new disposable directory.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))
from reconciliation import SCHEMA
from load_risk import run
from reporting import reports


def smoke(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(output / 'synthetic.sqlite3')
    db.executescript(SCHEMA)
    for day in pd.date_range('2026-08-01', periods=14, freq='D'):
        for label, hour, minute, status, forecast in [
            ('original', 12, 0, 'research', 10.),
            ('late', 13, 0, 'research', 99.),
            ('replay', 12, 15, 'research_replay', 77.)]:
            rid = label + '-' + day.strftime('%Y%m%d')
            published = day - pd.Timedelta(days=1) + pd.Timedelta(hours=hour, minutes=minute)
            version = 'synthetic-model-a' if day.day <= 7 else 'synthetic-model-b'
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',
                       (rid, 'load', str(published), version, '{}', 'synthetic_fixture', rid, status))
            for t in pd.date_range(day, periods=24, freq='h'):
                db.execute('INSERT INTO predictions VALUES(?,?,?,?,?,?,?)',
                           (rid, str(t), forecast, None, None, None, None))
        for t in pd.date_range(day, periods=24, freq='h'):
            available = day + pd.Timedelta(days=1, hours=12)
            observed = 11. if t.hour < 12 else 13.
            db.execute('INSERT INTO actuals VALUES(?,?,?,?,?,?,?)',
                       (str(t), 'load', observed, str(available), 'synthetic_fixture', 'actual-v1', str(available)))
    db.commit(); db.close()
    c = {'_root': str(output), 'db_path': 'synthetic.sqlite3', 'output_dir': 'reports',
         'review_date': '2026-08-20', 'decision_cutoff_hour': 12, 'decision_cutoff_minute': 30,
         'actual_lag_days': 5}
    diagnostic = run(c)
    report_stems = reports(c, pd.DataFrame(), pd.DataFrame(), pd.Timestamp('2026-08-15'),
                           pd.DataFrame(), {'suggestions': [], 'status': 'not_requested_in_smoke'},
                           load_risk=diagnostic)
    assert diagnostic['summary']['sample_hours'] == 336
    assert diagnostic['summary']['complete_days'] == 14
    assert abs(diagnostic['summary']['signed_bias_mwh'] - 2.) < 1e-9
    assert diagnostic['summary']['selected_model_version_count'] == 2
    assert diagnostic['automatic_correction'] is False
    for stem in report_stems:
        html = (output / 'reports' / (stem + '.html')).read_text()
        assert diagnostic['files']['json'] in html and 'k=1' in html
        assert (output / 'reports' / (stem + '.xlsx')).is_file()
    result = {'scope': 'synthetic', 'status': 'passed', 'diagnostic': diagnostic['files'],
              'sample_hours': 336, 'complete_days': 14, 'signed_bias_mwh': 2.,
              'wmape': diagnostic['summary']['wmape'], 'report_stems': report_stems,
              'model_or_trade_executed': False, 'config_applied': False}
    (output / 'smoke_result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(dict(result, output=str(output)), ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    smoke(parser.parse_args().output)
