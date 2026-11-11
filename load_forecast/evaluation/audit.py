"""Additional common-sample, grouped, interval and day-block diagnostics."""
import numpy as np
import pandas as pd
from evaluation.metrics import regression,classification,day_ci

def diagnostics(result,c):
    lo=pd.Timestamp(c['backtest']['test_start']);hi=pd.Timestamp(c['backtest']['test_end'])+pd.Timedelta(hours=23);rows=[];blocks=[];calibration=[]
    for name,f in result['loads'].items():
        a=f.loc[lo:hi]
        for hour,g in a.groupby(a.index.hour):rows.append(dict(object=name,group='hour_'+str(hour),**regression(g.actual,g.prediction)))
        good=a[['actual','prediction','baseline']].notna().all(axis=1);x=a.loc[good];diff=(x.actual-x.baseline).abs()-(x.actual-x.prediction).abs();ci=day_ci(diff,x.index,c['backtest']['bootstrap_samples'],c['project']['seed'])
        blocks.append(dict(object=name,metric='daily_absolute_error_improvement_vs_baseline',ci_low=ci[0],ci_high=ci[1],positive_is_better=True))
        mask=a[['actual','p10','p90']].notna().all(axis=1);z=a.loc[mask];rows.append(dict(object=name,group='interval80',n=len(z),coverage=float(((z.actual>=z.p10)&(z.actual<=z.p90)).mean()),width=float((z.p90-z.p10).mean())))
    a=result['decisions'].loc[lo:hi]
    for obj in ['da','rt']:
        for col in [obj+'_hat',obj+'_base']+[x for x in a if x.startswith(obj+'__')]:rows.append(dict(object=obj,group=col,**regression(a[obj],a[col])))
        mask=a[[obj,obj+'_p10',obj+'_p90']].notna().all(axis=1);z=a.loc[mask];rows.append(dict(object=obj,group='interval80',n=len(z),coverage=float(((z[obj]>=z[obj+'_p10'])&(z[obj]<=z[obj+'_p90'])).mean()),width=float((z[obj+'_p90']-z[obj+'_p10']).mean())))
        # Peak threshold is fitted before the test, not chosen from the test tail.
        training=result['prices'].loc[:lo-pd.Timedelta(hours=1),obj].dropna();threshold=training.quantile(.95);z=a[a[obj]>=threshold];rows.append(dict(object=obj,group='spike_above_pretest_p95',threshold=threshold,**regression(z[obj],z[obj+'_hat'])))
    for col in ['prob_rt_above_da','majority_prob','hour_prob','lag7_prob']:rows.append(dict(object='spread',group=col,**classification(a.spread,a[col])))
    p=a.prob_rt_above_da
    for j in range(10):
        z=a[(p>=j/10)&(p<(j+1)/10)]
        calibration.append(dict(probability_bin=f'{j/10:.1f}-{(j+1)/10:.1f}',n=len(z),mean_probability=z.prob_rt_above_da.mean(),observed_positive_rate=(z.spread>0).mean() if len(z) else np.nan))
    daily=a.groupby(a.index.normalize()).agg(research_increment=('research_increment','sum'),adopted_increment=('adopted_increment','sum'),research_recovery=('research_recovery','sum'))
    return dict(grouped_metrics=pd.DataFrame(rows),day_block_comparisons=pd.DataFrame(blocks),probability_calibration=pd.DataFrame(calibration),daily_strategy=daily)
