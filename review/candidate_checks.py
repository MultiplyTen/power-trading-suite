# -*- coding: utf-8 -*-
"""Shared research-candidate gates. No budget is inferred or changed here."""
import math
import numpy as np
import pandas as pd

RISK_PARAMETERS = {'lambda_risk', 'confidence', 'error_shrink', 'smooth_limit', 'backtest_window_days'}


def daily_metric(frame, task):
    groups = frame.groupby(frame.index.normalize())
    if task == 'load':
        return groups.apply(lambda x: float(abs(x.actual - x.prediction).sum() / x.actual.abs().sum())
                            if x.actual.abs().sum() > 0 else np.nan)
    if task == 'risk':
        # The worker emits one complete-day P&L row; never average hourly P&L as a day.
        if frame.index.normalize().duplicated().any():
            raise ValueError('风险证据必须每完整自然日一条收益，不能重复加权')
        return pd.Series(frame.profit.to_numpy(), index=frame.index.normalize())
    return groups.apply(lambda x: float((abs(x.da - x.da_hat).mean() + abs(x.rt - x.rt_hat).mean()) / 2))


def daily_improvement(baseline, candidate, task):
    old, new = daily_metric(baseline, task), daily_metric(candidate, task)
    if not old.index.equals(new.index):
        raise ValueError('候选与基准的逐日样本不一致')
    # A positive delta always means an improvement, including negative-profit baselines.
    return new - old if task == 'risk' else old - new


def passes_improvement(improvement, ci, threshold):
    try:
        values = [float(improvement), float(ci[0]), float(ci[1]), float(threshold)]
    except (TypeError, ValueError, IndexError):
        return False
    return (all(math.isfinite(v) for v in values) and values[0] >= values[3]
            and 0 < values[1] <= values[2])


def empirical_cvar90(loss):
    """Equal-weight upper 10% tail, including fractional boundary mass."""
    values = np.sort(np.asarray(loss, dtype=float))[::-1]
    if not len(values) or not np.isfinite(values).all():
        raise ValueError('整日损失证据缺失或无效')
    mass = len(values) * .1
    weights = np.clip(mass - np.arange(len(values)), 0, 1)
    return max(float(values @ weights / mass), 0.0)


def _feasible_flags(series):
    if not series.isin([True, False, 0, 1]).all():
        raise ValueError('风险可行状态缺失或不是布尔值')
    return series.astype(bool)


REFERENCE_FIELDS = ['reference_limit', 'reference_confidence', 'reference_error_shrink',
                    'reference_window_days', 'reference_scenario_days', 'reference_scenario_hash',
                    'reference_config_hash', 'reference_weighting']
REFERENCE_METRICS = ['reference_scenario_exceed', 'reference_scenario_feasible',
                     'reference_scenario_cvar90_cny']


def _digest_valid(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def risk_validation_guard(baseline, candidate, min_days=10):
    """Compare observed downside AND frozen original-configuration scenario downside.

    Candidate-owned feasibility/limits are not evidence for this gate. The worker must
    recompute k on the baseline's scenario window, shrink, confidence and daily limit.
    """
    result = {'schema_version': 'risk-reference-validation-2', 'status': 'insufficient_evidence',
              'minimum_complete_days': max(int(min_days), 10),
              'comparison_basis': 'same_days_original_scenarios_and_original_constraints',
              'interpretation': 'research_observed_and_proxy_scenario_evidence_not_future_guarantee'}
    try:
        required = ['profit', 'period_hours', 'reference_baseline_sha256'] + REFERENCE_FIELDS + REFERENCE_METRICS
        if any(name not in f for f in [baseline, candidate] for name in required):
            raise ValueError('缺少按原配置同场景重新评分的风险证据')
        if not baseline.index.equals(candidate.index) or baseline.index.normalize().duplicated().any():
            raise ValueError('风险候选与基准必须是同一组唯一自然日')
        result['complete_days'] = len(baseline)
        if len(baseline) < result['minimum_complete_days']:
            raise ValueError('完整验证日不足，不能判为零风险或通过')
        source_hash = baseline.attrs.get('artifact_sha256')
        if not _digest_valid(source_hash) or not candidate.reference_baseline_sha256.eq(source_hash).all():
            raise ValueError('候选未绑定同一份原配置基准风险输出')
        result['reference_baseline_sha256'] = source_hash
        numerical = ['profit', 'period_hours', 'reference_scenario_exceed', 'reference_scenario_cvar90_cny'] + REFERENCE_FIELDS[:5]
        for f in [baseline, candidate]:
            if not np.isfinite(f[numerical].to_numpy(dtype=float)).all():
                raise ValueError('原配置风险证据含缺失或非有限值')
            if (not f.period_hours.eq(24).all() or not f.reference_scenario_days.ge(30).all()
                    or not f.reference_window_days.ge(90).all() or f.reference_limit.lt(0).any()
                    or f.reference_error_shrink.lt(0).any()
                    or not f.reference_confidence.between(0, 1, inclusive='neither').all()
                    or not f.reference_scenario_exceed.between(0, 1).all()
                    or f.reference_scenario_cvar90_cny.lt(0).any()):
                raise ValueError('原配置完整日/场景/约束证据无效')
            for field in ['reference_scenario_hash', 'reference_config_hash']:
                if not f[field].map(_digest_valid).all():
                    raise ValueError('原配置场景或配置哈希缺失')
            if not f.reference_weighting.eq('equal_proxy_scenarios').all():
                raise ValueError('未知的原配置场景权重口径')
            flags = _feasible_flags(f.reference_scenario_feasible)
            expected = f.reference_scenario_exceed <= 1 - f.reference_confidence + 1e-10
            if not np.array_equal(flags.to_numpy(), expected.to_numpy()):
                raise ValueError('原配置场景可行标记与超限比例/原置信度不一致')
        for field in REFERENCE_FIELDS:
            same = (np.isclose(baseline[field].to_numpy(dtype=float), candidate[field].to_numpy(dtype=float), rtol=0, atol=1e-10).all()
                    if field in REFERENCE_FIELDS[:5] else baseline[field].equals(candidate[field]))
            if not same:
                raise ValueError('候选更改了风险比较的原场景或约束：' + field)
        limits = baseline.reference_limit.to_numpy(dtype=float)
        metrics = {}
        for label, f in [('baseline', baseline), ('candidate', candidate)]:
            losses = -f.profit.to_numpy(dtype=float)
            metrics[label] = {'cvar90_daily_loss_cny': empirical_cvar90(losses),
                              'exceed_fraction_at_baseline_limits': float((losses > limits + 1e-8).mean()),
                              'scenario_feasible_fraction': float(_feasible_flags(f.reference_scenario_feasible).mean()),
                              'reference_scenario_exceed_mean_fraction': float(f.reference_scenario_exceed.mean()),
                              'reference_scenario_cvar90_mean_cny': float(f.reference_scenario_cvar90_cny.mean())}
        result.update(metrics)
        old, new = metrics['baseline'], metrics['candidate']
        checks = {'cvar_not_worse': new['cvar90_daily_loss_cny'] <= old['cvar90_daily_loss_cny'] + 1e-8,
                  'exceed_fraction_not_worse': new['exceed_fraction_at_baseline_limits'] <= old['exceed_fraction_at_baseline_limits'] + 1e-12,
                  'scenario_feasibility_not_worse': new['scenario_feasible_fraction'] + 1e-12 >= old['scenario_feasible_fraction'],
                  'reference_scenario_exceed_not_worse': bool((candidate.reference_scenario_exceed <= baseline.reference_scenario_exceed + 1e-12).all()),
                  'reference_scenario_cvar_not_worse': bool((candidate.reference_scenario_cvar90_cny <= baseline.reference_scenario_cvar90_cny + 1e-8).all())}
        result['daily_reference_audit'] = []
        for day in baseline.index:
            b, n = baseline.loc[day], candidate.loc[day]
            row = {'date': str(day), **{key: b[key].item() if hasattr(b[key], 'item') else b[key] for key in REFERENCE_FIELDS}}
            for label, record in [('baseline', b), ('candidate', n)]:
                row[label + '_profit'] = float(record.profit)
                for metric in REFERENCE_METRICS:
                    value = record[metric]
                    row[label + '_' + metric] = value.item() if hasattr(value, 'item') else value
            result['daily_reference_audit'].append(row)
        result.update(checks=checks, status='passed' if all(checks.values()) else 'blocked_downside_worsened')
        if not all(checks.values()):
            result['reason'] = '收益改善不足以抵消原约束下的尾损、场景超限或可行率恶化'
    except (ValueError, TypeError, KeyError) as exc:
        result['reason'] = str(exc)
    return result


def risk_evidence_passed(evidence, min_days=10):
    """Recompute serialized daily reference evidence at apply time, not status flags."""
    try:
        if (evidence.get('status') != 'passed' or evidence.get('schema_version') != 'risk-reference-validation-2'
                or evidence.get('comparison_basis') != 'same_days_original_scenarios_and_original_constraints'):
            return False
        audit = pd.DataFrame(evidence['daily_reference_audit']).set_index('date')
        audit.index = pd.DatetimeIndex(audit.index)
        frames = {}
        for label in ['baseline', 'candidate']:
            frame = audit[REFERENCE_FIELDS].copy()
            frame['profit'] = audit[label + '_profit']
            for metric in REFERENCE_METRICS:
                frame[metric] = audit[label + '_' + metric]
            frame['period_hours'] = 24
            frame['reference_baseline_sha256'] = evidence['reference_baseline_sha256'] if label == 'candidate' else 'self'
            frame.attrs['artifact_sha256'] = evidence['reference_baseline_sha256']
            frames[label] = frame
        checked = risk_validation_guard(frames['baseline'], frames['candidate'], min_days)
        if checked['status'] != 'passed' or checked['complete_days'] != evidence['complete_days']:
            return False
        for label in ['baseline', 'candidate']:
            for key, value in checked[label].items():
                if not math.isfinite(float(evidence[label][key])) or abs(float(evidence[label][key]) - value) > 1e-9:
                    return False
        return True
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
