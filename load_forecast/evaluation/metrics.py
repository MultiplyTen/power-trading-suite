import numpy as np
import pandas as pd
from sklearn.metrics import balanced_accuracy_score,brier_score_loss,log_loss

def regression(a,p):
    a=np.asarray(a);p=np.asarray(p);good=np.isfinite(a)&np.isfinite(p);a=a[good];p=p[good]
    if not len(a):return {'n':0}
    e=p-a;den=np.abs(a).sum()
    return dict(n=len(a),mae=float(np.abs(e).mean()),rmse=float(np.sqrt((e**2).mean())),wmape=float(np.abs(e).sum()/den) if den else None,total_bias=float(e.sum()),relative_bias=float(e.sum()/den) if den else None)

def classification(s,p):
    good=np.isfinite(s)&np.isfinite(p);s=np.asarray(s)[good];p=np.asarray(p)[good];y=(s>0).astype(int)
    if not len(y):return {'n':0}
    return dict(n=len(y),accuracy=float(((p>.5)==y).mean()),balanced_accuracy=float(balanced_accuracy_score(y,p>.5)),brier=float(brier_score_loss(y,p)),log_loss=float(log_loss(y,np.clip(p,.001,.999),labels=[0,1])))

def day_ci(values,index,samples=2000,seed=42):
    v=pd.Series(np.asarray(values),index=index).groupby(pd.DatetimeIndex(index).normalize()).sum(min_count=1).dropna().to_numpy()
    if len(v)<2:return [None,None]
    rng=np.random.default_rng(seed);a=v[rng.integers(0,len(v),(samples,len(v)))].mean(axis=1)
    return [float(x) for x in np.quantile(a,[.025,.975])]
