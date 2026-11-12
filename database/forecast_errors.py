# -*- coding: utf-8 -*-
"""Read original M2 publications and as-of actual versions without changing SQLite.

One complete publication supplies a whole target day. Error signs are
actual minus forecast; M3's spread error remains (DA error minus RT error).
Naive timestamps follow the suite's Asia/Shanghai local-time convention.
"""
import json
import sqlite3
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd


METRICS = ('load', 'da', 'rt')


def local_time(value):
    try:
        t = pd.Timestamp(value)
    except (TypeError, ValueError):
        return pd.NaT
    if pd.isna(t):
        return pd.NaT
    return t.tz_convert('Asia/Shanghai').tz_localize(None) if t.tzinfo is not None else t


def _read(path, start, end, read_publications=True, read_actuals=True):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('预测误差数据库不存在：' + str(path))
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'runs', 'predictions'} <= tables:
            return pd.DataFrame(), pd.DataFrame()
        publications = pd.read_sql_query('''SELECT p.target,p.load,p.da,p.rt,
            r.id AS run_id,r.published_at,r.model_version,r.config_json,r.source,r.sha,r.status
            FROM predictions p JOIN runs r ON p.run_id=r.id
            WHERE r.kind='load' AND substr(p.target,1,10)>=? AND substr(p.target,1,10)<=?''',
            db, params=[str(start.date()), str(end.date())]) if read_publications else pd.DataFrame()
        actuals = pd.read_sql_query('''SELECT * FROM actuals
            WHERE substr(target,1,10)>=? AND substr(target,1,10)<=?''', db,
            params=[str(start.date()), str(end.date())]) if read_actuals and 'actuals' in tables else pd.DataFrame()
    return publications, actuals


def complete_m2_publications(path, start, end, asof, decision_cutoff_hour=12,
                             decision_cutoff_minute=30, metrics=METRICS,
                             model_version=None):
    """Return one unambiguous, original, complete 24-hour publication per day.

    The newest eligible *complete* run is selected. Later partial runs never
    supply missing hours to an older run. Explicit backtest/intraday runs and
    replay labels (in either run id or status) are excluded.
    """
    metrics = tuple(metrics)
    if not metrics or not set(metrics) <= set(METRICS):
        raise ValueError('metrics须为load/da/rt的非空子集')
    if not (0 <= decision_cutoff_hour <= 23 and 0 <= decision_cutoff_minute <= 59):
        raise ValueError('事前发布截止时分无效')
    start, end, asof = local_time(start), local_time(end), local_time(asof)
    if any(pd.isna(t) for t in (start, end, asof)) or start.normalize() > end.normalize():
        raise ValueError('历史误差起止日期或asof无效')
    start, end = start.normalize(), end.normalize()
    raw, _ = _read(path, start, end, read_actuals=False)
    if not raw.empty:
        raw['target'] = raw.target.map(local_time)
    rejected, daily, chosen = Counter(), [], []
    for day in pd.date_range(start, end, freq='D'):
        idx = pd.date_range(day, periods=24, freq='h')
        cutoff = min(asof, day - pd.Timedelta(days=1) + pd.Timedelta(
            hours=decision_cutoff_hour, minutes=decision_cutoff_minute))
        candidates = []
        d = raw[raw.target.dt.normalize().eq(day)] if not raw.empty else raw
        reasons = Counter()
        for rid, f in d.groupby('run_id') if not d.empty else []:
            r = f.iloc[0]
            reason = None
            published = local_time(r.published_at)
            try:
                config = json.loads(r.config_json or '{}')
            except (ValueError, TypeError):
                config = None
            if 'replay' in str(r.status).lower() or 'replay' in str(rid).lower():
                reason = 'replay'
            elif config is None or not isinstance(config, dict):
                reason = 'invalid_run_config'
            elif not isinstance(config.get('run', {}), dict) or config.get('run', {}).get('mode', 'forecast') != 'forecast':
                reason = 'not_forecast_run'
            elif any(x in str(r.status).lower() for x in ('failed', 'backtest', 'intraday', 'backfill', 'no_original_publication')):
                reason = 'not_original_forecast_status'
            elif pd.isna(published):
                reason = 'unknown_publication_time'
            elif published > cutoff:
                reason = 'late_publication'
            elif pd.isna(r.model_version) or not r.model_version:
                reason = 'unknown_model_version'
            elif model_version is not None and str(r.model_version) != str(model_version):
                reason = 'model_version_mismatch'
            elif f.target.duplicated().any():
                reason = 'duplicate_target'
            elif len(f) != 24 or set(f.target) != set(idx):
                reason = 'incomplete_publication_day'
            else:
                values = f[list(metrics)].apply(pd.to_numeric, errors='coerce')
                if not np.isfinite(values.to_numpy()).all() or ('load' in metrics and (values['load'] < 0).any()):
                    reason = 'invalid_forecast_values'
            if reason:
                rejected[reason] += 1
                reasons[reason] += 1
                continue
            candidates.append((published, rid, f))
        item = {'date': str(day.date()), 'forecast_cutoff': str(cutoff),
                'rejected_publications': dict(reasons)}
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
                rejected['ambiguous_latest_publication'] += 1
                item['status'] = 'ambiguous_latest_publication'
            else:
                published, rid, f = candidates[0]
                g = f.set_index('target').reindex(idx).copy()
                g.loc[:, list(metrics)] = g[list(metrics)].apply(pd.to_numeric)
                g = g.rename(columns={m: m + '_hat' for m in metrics})
                g['forecast_cutoff'] = cutoff
                g['published_at'] = published
                g.index.name = 'target'
                chosen.append(g)
                item.update(status='complete_publication', run_id=rid,
                            published_at=str(published), model_version=str(g.model_version.iloc[0]),
                            source=str(g.source.iloc[0]), publication_sha=str(g.sha.iloc[0]),
                            eligible_complete_runs=len(candidates))
                earliest = min(candidates, key=lambda x: x[0])
                old = earliest[2].set_index('target').reindex(idx)
                item['earliest_eligible_run_id'] = earliest[1]
                item['publication_revision_change'] = {
                    m: {'maximum_absolute_hourly_change': float((g[m + '_hat'] - old[m]).abs().max()),
                        'sum_hourly_change': float((g[m + '_hat'] - old[m]).sum())}
                    for m in metrics}
        else:
            item['status'] = 'no_complete_publication'
        daily.append(item)
    frame = pd.concat(chosen).sort_index() if chosen else pd.DataFrame(
        columns=[m + '_hat' for m in metrics] + ['run_id', 'model_version', 'published_at', 'forecast_cutoff'],
        index=pd.DatetimeIndex([], name='target'))
    versions = sorted(frame.model_version.astype(str).unique()) if not frame.empty else []
    audit = dict(source='original_m2_publications', asof=str(asof),
                 forecast_cutoff_rule='target D-1 %02d:%02d Asia/Shanghai' % (
                     decision_cutoff_hour, decision_cutoff_minute),
                 requested_model_version=model_version, metrics=list(metrics),
                 requested_days=len(daily), complete_publication_days=len(frame) // 24,
                 missing_publication_days=len(daily) - len(frame) // 24,
                 model_versions=versions, mixed_model_versions=len(versions) > 1,
                 observed_run_model_versions=sorted(raw.model_version.fillna('unknown').astype(str).unique()) if not raw.empty else [],
                 run_exclusion_counts=dict(rejected), daily=daily,
                 strict_model_input_availability_certified=False,
                 availability_note='Original publication time is checked; historical training input availability is not independently certified.')
    return frame, audit


def paired_forecast_errors(path, start, end, asof, decision_cutoff_hour=12,
                           decision_cutoff_minute=30, metrics=METRICS,
                           model_version=None, complete_actual_days=True):
    """Pair complete publications with actual versions known at ``asof``.

    By default only complete 24-hour joint days are returned. M4 may pass
    complete_actual_days=False to diagnose available hours and their gaps.
    Actual receipt (ingested_at) and available_at must both be known and <=asof.
    """
    frame, audit = complete_m2_publications(path, start, end, asof,
        decision_cutoff_hour, decision_cutoff_minute, metrics, model_version)
    asof = local_time(asof)
    _, raw = _read(path, local_time(start).normalize(), local_time(end).normalize(), read_publications=False)
    excluded, selected = Counter(), []
    revisions = {}
    if not raw.empty:
        raw = raw[raw.metric.isin(metrics)].copy()
        for col in ['target', 'available_at', 'ingested_at']:
            raw[col] = raw[col].map(local_time)
        raw['value'] = pd.to_numeric(raw.value, errors='coerce')
        for (target, metric), f in raw.groupby(['target', 'metric']):
            total_versions = len(f)
            good = f.available_at.notna() & f.ingested_at.notna()
            excluded['unknown_actual_time'] += int((~good).sum())
            late = good & ((f.available_at > asof) | (f.ingested_at > asof))
            excluded['actual_not_available_at_asof'] += int(late.sum())
            invalid = ~np.isfinite(f.value) | ((f.value < 0) if metric == 'load' else f.value.eq(0))
            excluded['invalid_actual_value'] += int(invalid.sum())
            # Load/RT realization cannot precede its hour; cleared DA can.
            early = good & (f.available_at < target) if metric != 'da' else pd.Series(False, index=f.index)
            excluded['actual_available_before_target'] += int(early.sum())
            f = f[good & ~late & ~invalid & ~early]
            if f.empty:
                continue
            f = f.sort_values(['available_at', 'ingested_at'])
            r = f.iloc[-1]
            tie = f[(f.available_at == r.available_at) & (f.ingested_at == r.ingested_at)]
            if tie.value.nunique() > 1:
                excluded['ambiguous_actual_revision'] += 1
                continue
            selected.append(dict(target=target, metric=metric, **{
                'actual_' + metric: float(r.value),
                'actual_' + metric + '_available_at': r.available_at,
                'actual_' + metric + '_ingested_at': r.ingested_at,
                'actual_' + metric + '_source_sha': r.source_sha,
                'actual_' + metric + '_evidence': r.evidence,
                'actual_' + metric + '_eligible_versions': len(f)}))
            revisions[(str(target), metric)] = {'selected_source_sha': r.source_sha,
                'eligible_versions': len(f), 'total_versions': total_versions}
    for metric in metrics:
        rows = [r for r in selected if r['metric'] == metric]
        cols = [k for k in rows[0] if k not in ['target', 'metric']] if rows else ['actual_' + metric]
        actual = pd.DataFrame(rows).set_index('target')[cols] if rows else pd.DataFrame(columns=cols)
        frame = frame.join(actual)
        frame[metric + '_error'] = frame['actual_' + metric] - frame[metric + '_hat']
    required = ['actual_' + m for m in metrics]
    complete, partial, kept = 0, 0, []
    for item in audit['daily']:
        day = pd.Timestamp(item['date'])
        g = frame[frame.index.normalize() == day]
        if g.empty:
            item['paired_hours'] = 0
            continue
        ok = g[required].notna().all(axis=1)
        item['paired_hours'] = int(ok.sum())
        item['missing_actual_hours'] = {m: int(g['actual_' + m].isna().sum()) for m in metrics}
        if ok.all():
            complete += 1
            item['status'] = 'complete_pair'
        elif ok.any():
            partial += 1
            item['status'] = 'partial_actual_day'
        else:
            item['status'] = 'no_actual_pair'
        if ok.all() or not complete_actual_days:
            kept.append(g[ok])
    result = pd.concat(kept).sort_index() if kept else frame.iloc[:0].copy()
    if {'da', 'rt'} <= set(metrics):
        result['spread_error'] = result.da_error - result.rt_error
    audit.update(complete_pair_days=complete, partial_pair_days=partial,
                 returned_hours=len(result), complete_actual_days=complete_actual_days,
                 missing_actual_days=audit['complete_publication_days'] - complete,
                 actual_row_exclusion_counts={k: v for k, v in excluded.items() if v},
                 actual_version_selection=list(dict(target=t, metric=m, **v) for (t, m), v in revisions.items()))
    return result, audit


def select_joint_error_history(proxy, original, pairing_audit, forecast,
                               model_version, mode='prefer_m2_publications',
                               minimum_days=30):
    """Choose a single, clearly labelled residual family, never mix sources.

    Original errors must belong to the target publication's model version and
    contain complete same-day load/DA/RT pairs. ``proxy`` remains an explicit
    research comparison when original usable days are insufficient.
    """
    if mode not in ('prefer_m2_publications', 'require_m2_publications', 'proxy_only'):
        raise ValueError('historical_error_source配置无效')
    if not isinstance(minimum_days, int) or minimum_days < 30:
        raise ValueError('原始M2联合误差至少需要30个完整日')
    columns = ['load_hat', 'actual_load', 'da_hat', 'rt_hat', 'actual_da', 'actual_rt']
    same = original[original.model_version.astype(str).eq(str(model_version))].copy() if not original.empty else original.copy()
    day_count, usable_count = 0, 0
    keep = []
    for _, f in same.groupby(same.index.normalize()):
        expected = pd.date_range(f.index[0].normalize(), periods=24, freq='h')
        if len(f) != 24 or f.index.has_duplicates or not f.index.equals(expected):
            continue
        if not set(columns) <= set(f) or not np.isfinite(f[columns].to_numpy(dtype=float)).all():
            continue
        day_count += 1
        if (np.asarray(forecast, dtype=float) + (f.actual_load - f.load_hat).to_numpy() >= 0).all():
            usable_count += 1
        keep.append(f)
    ready = usable_count >= minimum_days
    if mode == 'require_m2_publications' and not ready:
        raise ValueError('同版本原始M2负荷/日前/实时完整误差可用日仅%s，要求至少%s天；禁止以proxy冒充真实校准' % (usable_count, minimum_days))
    use_original = ready and mode != 'proxy_only'
    selected = pd.concat(keep).sort_index() if use_original else proxy.copy()
    if use_original:
        # One original publication supplies all three predictions for each day.
        selected['da'] = selected.actual_da
        selected['rt'] = selected.actual_rt
        selected['da_error'] = selected.actual_da - selected.da_hat
        selected['rt_error'] = selected.actual_rt - selected.rt_hat
        selected['spread_error'] = selected.da_error - selected.rt_error
    versions = sorted(original.model_version.astype(str).unique()) if not original.empty else []
    metadata = dict(mode=mode, source='original_m2_same_version' if use_original else 'proxy_research_fallback',
        target_model_version=model_version, observed_original_model_versions=versions,
        same_version_complete_days=day_count, same_version_nonnegative_scenario_days=usable_count,
        same_version_negative_rebased_days=day_count-usable_count,
        minimum_days=minimum_days, selected_scenario_days=len(selected) // 24,
        original_pairs_used=use_original, mixed_error_sources=False,
        current_model_original_errors_used=use_original,
        strict_input_availability_certified=False,
        fallback_reason=None if use_original else ('explicit_proxy_only' if mode == 'proxy_only' else 'insufficient_same_version_original_pairs'),
        proxy_load_method='causal 28-day same-hour load baseline',
        proxy_price_method='coefficient_opt.price_prediction rolling model; not M2 model residuals',
        pairing_audit=pairing_audit)
    return selected, metadata
