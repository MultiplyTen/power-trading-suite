"""Policy selection follows model selection, and precedes untouched test days."""
import numpy as np
import pandas as pd
from strategy.optimizer import decide,gain,recovery,daily_constraints
from evaluation.metrics import classification,day_ci

def evaluate_policy(prices,portfolio,c):
    a=prices.join(portfolio[['actual','prediction','baseline','p10','p90']].rename(columns={'actual':'actual_load','prediction':'load_hat','baseline':'load_baseline','p10':'load_p10','p90':'load_p90'}),how='inner',validate='1:1')
    a['load_error']=a.actual_load-a.load_hat;a['spread_error']=a.spread-a.spread_hat
    b=c['backtest'];freeze=pd.Timestamp(b.get('model_selection_end',b['validation_end']))+pd.Timedelta(days=1)
    a=a.loc[freeze:].copy();histories={}
    for ts in a.index:
        stop=ts.normalize()-pd.Timedelta(days=c['run']['observation_lag_days'])+pd.Timedelta(hours=23)
        histories[ts]=a[(a.index<=stop)&(a.index>=ts.normalize()-pd.Timedelta(days=c['strategy']['scenario_days']))&(a.index.hour==ts.hour)][['load_error','spread_error']].dropna()
    def apply(spec,end=None):
        rows=[]
        for ts,row in (a.loc[:end] if end is not None else a).iterrows():
            rows.append(dict(valid_time=ts,**decide(row.load_hat,row.prob_rt_above_da,row.spread_hat,histories[ts],spec,enabled=False)))
        r=pd.DataFrame(rows).set_index('valid_time');return daily_constraints(a.join(r).loc[r.index],spec)
    validation=[];best_score=0.;selected=None;selected_ci=[0.,0.]
    vstop=pd.Timestamp(b['validation_end'])+pd.Timedelta(hours=23)
    for risk in c['strategy']['risk_lambda_candidates']:
        for band in c['strategy']['probability_deadband_candidates']:
            spec=dict(c['strategy'],risk_lambda=risk,probability_deadband=band);r=apply(spec,vstop).loc[pd.Timestamp(b['validation_start']):]
            dg=gain(r.research_k*r.load_hat,r.actual_load,r.spread,spec)-gain(r.load_hat,r.actual_load,r.spread,spec);total=float(dg.sum());ci=day_ci(dg,r.index,c['backtest']['bootstrap_samples'],c['project']['seed']);validation.append(dict(risk_lambda=risk,probability_deadband=band,increment=total,day_block_ci=ci,adjusted_hours=int((r.research_k!=1).sum())))
            if total>best_score+1e-8:best_score=total;selected=spec;selected_ci=ci
    validation.append(dict(candidate='neutral',increment=0.,day_block_ci=[0.,0.],adjusted_hours=0))
    v=a.loc[pd.Timestamp(b['validation_start']):vstop];met=classification(v.spread,v.prob_rt_above_da);base=classification(v.spread,v.majority_prob);gate=c['strategy']['validation_gate']
    direction_ok=met.get('accuracy',0)-base.get('accuracy',0)>=gate['min_accuracy_gain_vs_majority'] and met.get('balanced_accuracy',0)>=gate['min_balanced_accuracy']
    ci_ok=not gate['require_positive_day_block_ci'] or (selected_ci[0] is not None and selected_ci[0]>0)
    basis_ok=not gate['require_confirmed_price_basis'] or c['input']['price_basis_confirmed']
    availability_ok=not gate['require_verified_availability'] or (c['run']['strict_availability'] and c['input'].get('availability_certified',False))
    enabled=selected is not None and direction_ok and ci_ok and basis_ok and availability_ok and c['input'].get('unit_confirmed',False)
    out=apply(selected or c['strategy'])
    if selected is None:
        out['research_k']=1.;out['expected_increment']=0.;out['tail_loss']=0.;out['reason']='验证期未优于不调整，冻结中性策略'
    if enabled:out['adopted_k']=out.research_k;out['declaration_quantity']=out.load_hat*out.adopted_k
    out['research_increment']=gain(out.research_k*out.load_hat,out.actual_load,out.spread,c['strategy'])-gain(out.load_hat,out.actual_load,out.spread,c['strategy'])
    out['adopted_increment']=gain(out.declaration_quantity,out.actual_load,out.spread,c['strategy'])-gain(out.load_hat,out.actual_load,out.spread,c['strategy'])
    out['research_recovery']=recovery(out.research_k*out.load_hat,out.actual_load,out.spread,c['strategy'])
    details=dict(enabled=enabled,direction_ok=direction_ok,ci_ok=ci_ok,basis_ok=basis_ok,availability_ok=availability_ok,selected=None if selected is None else {k:selected[k] for k in ['risk_lambda','probability_deadband']},validation=validation)
    return out,details
