"""Reproducible synthetic comparison; no business inputs, DB or network required."""
import argparse
import copy
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'load_forecast'))
from configuration import read_config
from evaluation.metrics import regression
from evaluation.rolling_backtest import walk
from features.load import make_features
from weather.features import VARS


def run(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    config = read_config(ROOT / 'load_forecast/config.yaml')
    rng = np.random.default_rng(42)
    index = pd.date_range('2026-01-01', '2026-07-31 23:00', freq='h')
    hour = index.hour.to_numpy()
    day = np.arange(len(index)) / 24
    weekday_effect = np.array([12., 0., 1., 0., -5., -25., -32.])[index.dayofweek]
    temperature = 24 + 6 * np.sin(2 * np.pi * (day - 80) / 365)
    actual = pd.Series(100 + weekday_effect + 12 * np.sin(2 * np.pi * hour / 24)
                       + 2 * np.maximum(temperature - 26, 0)
                       + rng.normal(0, 4, len(index)), index=index)
    weather = pd.DataFrame(0., index=index, columns=VARS)
    weather['temperature_2m'] = temperature
    weather['relative_humidity_2m'] = 70.
    weather['wind_speed_100m'] = 3.
    results = {}
    predictions = pd.DataFrame({'actual': actual.loc['2026-07-01':]})
    for mode in ('basic', 'enhanced'):
        c = copy.deepcopy(config)
        c['training']['calendar_features'] = mode
        f, x = make_features(actual, weather, c, 28)
        p, _, trace = walk(f, x, c, '2026-07-01', '2026-07-31',
                           c['models']['portfolio']['ridge_alpha'], 'baseline_ridge')
        predictions[mode] = p.reindex(predictions.index)
        results[mode] = {'feature_count': len(x.columns), 'fit_origins': len(trace)}
    # Both variants use exactly the same held-out timestamps and origin rules.
    common = predictions.notna().all(axis=1)
    for mode in ('basic', 'enhanced'):
        results[mode].update(regression(predictions.loc[common, 'actual'], predictions.loc[common, mode]))
    result = {
        'scope': 'synthetic_only', 'seed': 42,
        'notice': 'Synthetic weekly/weather signal, not evidence of real business accuracy or returns. Data generation was fixed before comparison; no tuning to select a favorable result.',
        'test_window': ['2026-07-01', '2026-07-31'],
        'observation_lag_days': config['run']['observation_lag_days'],
        'ridge_alpha': config['models']['portfolio']['ridge_alpha'],
        'results': results,
    }
    (output / 'comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    predictions.to_csv(output / 'predictions.csv', index_label='target')
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='Explicit destination for synthetic outputs')
    run(parser.parse_args().output)
