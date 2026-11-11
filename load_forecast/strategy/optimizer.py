import numpy as np
import pandas as pd

def recovery(q,l,s,c):
    return np.maximum(q-c['deviation_upper_ratio']*l,0)*np.maximum(s,0)+np.maximum(c['deviation_lower_ratio']*l-q,0)*np.maximum(-s,0)

def gain(q,l,s,c):return (q-l)*s-recovery(q,l,s,c)

def decide(f,p,spread,scenarios,c,enabled=False):
    result=dict(research_k=1.,adopted_k=1.,declaration_quantity=np.nan,scenario_count=len(scenarios),expected_increment=0.,tail_loss=0.,reason='无有效负荷')
    if not np.isfinite(f) or f<0:return result
    result['declaration_quantity']=f
    if not np.isfinite(p) or not np.isfinite(spread):result['reason']='价格预测缺失';return result
    if abs(p-.5)<c['probability_deadband']:result['reason']='价差概率处于中性带';return result
    if (p-.5)*spread<=0:result['reason']='价差金额与方向概率矛盾';return result
    if len(scenarios)<c['min_scenarios_per_hour']:result['reason']='同小时样本外联合场景不足';return result
    l=f+scenarios['load_error'].to_numpy();s=spread+scenarios['spread_error'].to_numpy();ok=np.isfinite(l)&np.isfinite(s)&(l>=0);l=l[ok];s=s[ok];result['scenario_count']=len(l)
    if len(l)<c['min_scenarios_per_hour']:result['reason']='剔除不可能的负负荷场景后样本不足';return result
    ks=np.unique(np.r_[np.arange(c['k_min'],c['k_max']+c['k_step']/2,c['k_step']),1.]);best=(0.,1.,0.,0.)
    for k in sorted(ks,key=lambda k:abs(k-1)):
        dg=gain(k*f,l,s,c)-gain(f,l,s,c);tail=max(-np.sort(dg)[:max(1,int(np.ceil(len(dg)*c['tail_fraction'])))].mean(),0);mean=float(dg.mean());u=mean-c['risk_lambda']*tail
        if u>best[0]+1e-9:best=(u,k,mean,tail)
    _,k,mean,tail=best;result.update(research_k=float(k),expected_increment=mean,tail_loss=float(tail),reason='候选效用不正' if k==1 else '场景效用最大')
    if enabled:result.update(adopted_k=float(k),declaration_quantity=float(k*f))
    elif k!=1:result['reason']='研究候选未通过来源/样本外收益启用门槛'
    return result

def daily_constraints(frame,c):
    a=frame.copy()
    for d,g in a.groupby(a.index.normalize()):
        energy=((g.research_k-1)*g.load_hat).abs().sum();loss=g.tail_loss.sum()
        fail=(c['max_daily_adjustment_energy'] is not None and energy>c['max_daily_adjustment_energy']) or (c['max_daily_expected_loss'] is not None and loss>c['max_daily_expected_loss'])
        if fail:a.loc[g.index,['research_k','adopted_k']]=1.;a.loc[g.index,'declaration_quantity']=g.load_hat;a.loc[g.index,'reason']='整日预算/保守尾部损失约束触发，整日中性';a.loc[g.index,['expected_increment','tail_loss']]=0.
    return a
