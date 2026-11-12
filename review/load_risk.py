# -*- coding: utf-8 -*-
"""Read-only diagnostics of original, pre-decision load forecasts and later observations."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from common import resolve, dump


def review_as_of(c):
    if c.get('review_as_of'):
        time = pd.Timestamp(c['review_as_of'])
        return time.tz_localize('Asia/Shanghai') if time.tzinfo is None else time.tz_convert('Asia/Shanghai')
    if c.get('review_date', 'today') == 'today':
        return pd.Timestamp.now(tz='Asia/Shanghai')
    return pd.Timestamp(c['review_date']).tz_localize('Asia/Shanghai') + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)


def _day_type(day):
    from database.runtime import day_details
    try:
        work, holiday = day_details(day.date())
        return 'workday' if work else 'holiday' if holiday else 'weekend'
    except ValueError:
        return 'calendar_unavailable'


def _summary(frame, full_days, minimum):
    count = len(frame)
    days = frame.index.normalize() if count else pd.DatetimeIndex([])
    complete = len(set(days).intersection(full_days))
    ready = count >= minimum and complete >= minimum
    result = {'status': 'sufficient_descriptive_sample' if ready else 'insufficient_sample',
              'sample_hours': count, 'observed_days': len(set(days)), 'complete_days': complete,
              'signed_bias_mwh': None, 'signed_bias_ratio': None, 'wmape': None,
              'k1_total_absolute_deviation_mwh': None, 'k1_max_hourly_deviation_mwh': None,
              'bias_correction_candidate_mwh': None,
              'error_p10_mwh': None, 'error_p50_mwh': None, 'error_p90_mwh': None,
              'centered_residual_p10_mwh': None, 'centered_residual_p50_mwh': None,
              'centered_residual_p90_mwh': None}
    if not count:
        return result
    errors = frame.actual_load - frame.load_hat
    bias = float(errors.mean())
    denom = float(frame.actual_load.abs().sum())
    result.update(signed_bias_mwh=bias,
                  signed_bias_ratio=float(errors.sum() / denom) if denom > 0 else None,
                  wmape=float(errors.abs().sum() / denom) if denom > 0 else None,
                  k1_total_absolute_deviation_mwh=float(errors.abs().sum()),
                  k1_max_hourly_deviation_mwh=float(errors.abs().max()))
    if ready:
        result['bias_correction_candidate_mwh'] = bias
        for prefix, values in [('error', errors), ('centered_residual', errors - bias)]:
            for q, label in [(.1, 'p10'), (.5, 'p50'), (.9, 'p90')]:
                result[prefix + '_' + label + '_mwh'] = float(values.quantile(q))
    return result


def diagnose(frame, audit=None, c=None):
    """Aggregate a uniquely paired frame; a correction candidate is never auto-applied."""
    c = c or {}
    settings = c.get('load_risk_diagnostics', {})
    minimum = max(2, int(settings.get('min_complete_days', 7)))
    f = frame.copy()
    f.index = pd.DatetimeIndex(f.index)
    if f.index.tz is not None:
        f.index = f.index.tz_convert('Asia/Shanghai').tz_localize(None)
    if f.index.has_duplicates:
        raise ValueError('负荷误差诊断每目标小时只能一条配对，拒绝重复发布加权')
    if len(f) and (not {'load_hat', 'actual_load'}.issubset(f.columns)
                   or not np.isfinite(f[['load_hat', 'actual_load']].to_numpy(dtype=float)).all()
                   or (f[['load_hat', 'actual_load']] < 0).any().any()):
        raise ValueError('负荷误差配对缺失或无效')
    f = f.sort_index()
    if len(f):
        f['error_mwh'] = f.actual_load - f.load_hat
        f['day_type'] = [_day_type(t) for t in f.index]
        f['model_version'] = f.get('model_version', pd.Series('unknown', index=f.index)).fillna('unknown').astype(str)
    counts = pd.Series(f.index.normalize()).value_counts() if len(f) else pd.Series(dtype=int)
    full_days = set(counts[counts.eq(24)].index)
    summary = _summary(f, full_days, minimum)
    tables = {}
    hourly = []
    for hour in range(24):
        hourly.append({'hour': hour, **_summary(f[f.index.hour == hour], full_days, minimum)})
    tables['hourly'] = pd.DataFrame(hourly)
    tables['day_type'] = pd.DataFrame([{'day_type': name, **_summary(g, full_days, minimum)}
                                       for name, g in f.groupby('day_type')] if len(f) else [])
    latest = f.index.max().normalize() if len(f) else None
    recent = []
    for days in settings.get('recent_windows_days', [7, 14, 30]):
        days = int(days)
        if days < 1:
            raise ValueError('近期诊断窗口须为正天数')
        start = latest - pd.Timedelta(days=days - 1) if latest is not None else None
        g = f[f.index >= start] if start is not None else f
        recent.append({'window_calendar_days': days, 'target_start': str(start.date()) if start is not None else None,
                       'target_end': str(latest.date()) if latest is not None else None,
                       **_summary(g, full_days, minimum)})
    tables['recent'] = pd.DataFrame(recent)
    tables['model_versions'] = pd.DataFrame([{'model_version': name, **_summary(g, full_days, minimum)}
                                             for name, g in f.groupby('model_version')] if len(f) else [])
    tables['paired_hours'] = f.rename_axis('target').reset_index()
    summary['selected_model_version_count'] = int(f.model_version.nunique()) if len(f) else 0
    source = 'actual_load_source_sha'
    summary['selected_actual_source_version_count'] = int(f[source].nunique()) if len(f) and source in f else 0
    report = {'schema_version': 'load-error-diagnostics-1', 'status': summary['status'], 'summary': summary,
              'minimum_complete_days': minimum,
              'error_convention': 'actual_load_minus_forecast_load',
              'bias_interpretation': 'positive means forecast underestimation; candidate only, never applied',
              'uncertainty_interpretation': 'centered historical residual quantiles, not calibrated future intervals',
              'k1_exposure': 'k=1 declares the forecast; load error remains an unhedged quantity deviation',
              'monetary_risk_status': 'not_evaluated_no_prices_or_operating_budget',
              'version_comparison': 'version groups may cover different dates; differences are descriptive, not causal gains',
              'pairing_audit': audit or {}, 'automatic_correction': False, 'execution_authorized': False}
    return report, tables


def run(c, output_dir=None):
    from database.forecast_errors import paired_forecast_errors
    settings = c.get('load_risk_diagnostics', {})
    asof = review_as_of(c)
    end = asof.tz_localize(None).normalize()
    window = int(settings.get('window_days', 90))
    if window < 1:
        raise ValueError('负荷诊断回看窗口须为正天数')
    start = end - pd.Timedelta(days=window - 1)
    frame, audit = paired_forecast_errors(resolve(c, c['db_path']), start, end, asof,
                                          decision_cutoff_hour=c.get('decision_cutoff_hour', 12),
                                          decision_cutoff_minute=c.get('decision_cutoff_minute', 30),
                                          metrics=('load',), complete_actual_days=False)
    report, tables = diagnose(frame, audit, c)
    report.update(review_as_of=asof.isoformat(), target_window=[str(start.date()), str(end.date())])
    out = Path(output_dir) if output_dir is not None else resolve(c, c['output_dir'])
    out.mkdir(parents=True, exist_ok=True)
    stem = 'load_risk_' + end.strftime('%Y%m%d')
    files = {'json': stem + '.json'}
    for name, table in tables.items():
        files[name] = stem + '_' + name + '.csv'
        table.to_csv(out / files[name], index=False, encoding='utf-8-sig')
    report['files'] = files
    report['tables'] = {name: json.loads(table.to_json(orient='records', date_format='iso', force_ascii=False))
                        for name, table in tables.items() if name != 'paired_hours'}
    dump(out / files['json'], report)
    return report
