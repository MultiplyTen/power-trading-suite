"""Frozen baseline scenario construction for research-candidate comparisons."""
import hashlib
import json
import numpy as np
import pandas as pd


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()


def risk_scenarios(history, day, config):
    """Keep the original worker's proxy/equal-weight scope, with an auditable snapshot."""
    last = day - pd.Timedelta(days=config['lag_days']) + pd.Timedelta(hours=23)
    h = history.loc[last.normalize() - pd.Timedelta(days=config['backtest_window_days'] - 1):last]
    required = ['da', 'rt', 'da_hat', 'rt_hat', 'load_hat', 'actual_load', 'spread_error']
    h = h[required]
    expected = pd.date_range(last.normalize() - pd.Timedelta(days=config['backtest_window_days'] - 1), last, freq='h')
    if not h.index.equals(expected) or not np.isfinite(h.to_numpy(dtype=float)).all():
        raise ValueError(f'{day.date()}：原始基准窗口缺少完整唯一联合场景')
    target = history.loc[day:day + pd.Timedelta(hours=23)]
    if not target.index.equals(pd.date_range(day, periods=24, freq='h')) or not np.isfinite(target[required].to_numpy(dtype=float)).all():
        raise ValueError(f'{day.date()}：风险回测目标日不是完整24小时')
    f = np.broadcast_to(target.load_hat.to_numpy(), (len(h) // 24, 24))
    l = f + (h.actual_load - h.load_hat).to_numpy().reshape(-1, 24)
    spread = (target.da_hat - target.rt_hat).to_numpy() + h.spread_error.to_numpy().reshape(-1, 24)
    valid = (l >= 0).all(axis=1)
    if int(valid.sum()) < 30:
        raise ValueError(f'{day.date()}：非负负荷的完整基准场景少于30日')
    std = h.groupby(h.index.hour).spread_error.std().reindex(range(24)).to_numpy()
    if not np.isfinite(std).all():
        raise ValueError('原始基准场景价差误差校准不足')
    f, l, spread = f[valid], l[valid], spread[valid]
    snapshot = {'f': f.tolist(), 'l': l.tolist(), 'spread': spread.tolist(), 'std': std.tolist(),
                'window_start': str(h.index.min()), 'window_end': str(h.index.max()),
                'weighting': 'equal_proxy_scenarios'}
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return f, l, spread, std, target, digest


def evaluate_reference(k, scenarios, reference_config, limit, pnl):
    """Evaluate any k under the unchanged baseline fee/shrink/confidence/window."""
    from candidate_checks import empirical_cvar90
    f, l, spread, std, _, scenario_hash = scenarios
    values = pnl(k, f, l, spread, reference_config, std)
    if not np.isfinite(values).all() or not np.isfinite(limit) or limit < 0:
        raise ValueError('同基准场景损益或限额无效')
    exceed = float((-values > limit + 1e-8).mean())
    return {'reference_scenario_exceed': exceed,
            'reference_scenario_feasible': exceed <= 1 - reference_config['confidence'] + 1e-10,
            'reference_scenario_cvar90_cny': empirical_cvar90(-values),
            'reference_limit': float(limit), 'reference_confidence': float(reference_config['confidence']),
            'reference_error_shrink': float(reference_config['error_shrink']),
            'reference_window_days': int(reference_config['backtest_window_days']),
            'reference_scenario_days': len(f), 'reference_scenario_hash': scenario_hash,
            'reference_config_hash': config_hash(reference_config),
            'reference_weighting': 'equal_proxy_scenarios'}
