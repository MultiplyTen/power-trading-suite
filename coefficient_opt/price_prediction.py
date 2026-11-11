# -*- coding: utf-8 -*-
"""每日滚动 Ridge；全部预处理只拟合训练集，默认 D-2 信息边界。"""
import logging
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.pipeline import make_pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
from sklearn.ensemble import ExtraTreesRegressor
from calendar_features import calendar

def features(prices,load,weather,c):
    x=calendar(prices.index)
    for lag in sorted(set([c['lag_days'],7])):
        for col in ['da','rt']:x[f'{col}_lag{lag}']=prices[col].shift(24*lag)
        x[f'spread_lag{lag}']=x[f'da_lag{lag}']-x[f'rt_lag{lag}']
    if c.get('use_load_feature',True):x['load_forecast']=load
    if c.get('use_system_load_feature',False):
        from database.raw_store import system_feature
        from database.store import DB
        path=Path(__file__).resolve().parent/c.get('db_path','../database/master.db')
        v=system_feature(prices.index,path,c.get('system_load_lag_days',2))
        x['system_load_lag']=v
        x['system_load_missing']=v.isna().astype(int)
    result=x.join(weather) if c.get('use_weather',True) else x
    if c.get('use_disclosure_features',False):
        from load_forecast.evaluation.disclosure import read_disclosure_features
        disclosure=read_disclosure_features({
            '_root':str(Path(__file__).resolve().parent),
            'db_path':c.get('db_path','../database/master.db'),
        })
        if not disclosure.empty:result=result.join(disclosure,how='left')
    return result

def predict_day(prices,x,day,c):
    last=day-pd.Timedelta(days=c['lag_days'])+pd.Timedelta(hours=23)
    train=(prices.index<=last)&(prices.index>=last-pd.Timedelta(days=c['training_days']))
    idx=pd.date_range(day,periods=24,freq='h');out=pd.DataFrame(index=idx)
    for col in ['da','rt']:
        valid=train&prices[col].notna().to_numpy()
        if valid.sum()<90*24:raise ValueError(f'{day.date()} 的 {col} 训练数据不足 90 天有效小时')
        m=make_pipeline(SimpleImputer(strategy='median',keep_empty_features=True),StandardScaler(),(Ridge(alpha=c['ridge_alpha']) if c.get('model_name','ridge')=='ridge' else ExtraTreesRegressor(n_estimators=40,max_depth=6,min_samples_leaf=12,random_state=42,n_jobs=1)))
        m.fit(x.loc[valid],prices.loc[valid,col]);out[col+'_hat']=m.predict(x.loc[idx])
    out['training_end']=last;out['decision_cutoff']=day-pd.Timedelta(days=1)+pd.Timedelta(hours=c.get('decision_cutoff_hour',12),minutes=c.get('decision_cutoff_minute',30))
    return out

def rolling(prices,x,start,end,c):
    parts=[]
    for i,day in enumerate(pd.date_range(start,end,freq='D')):
        parts.append(predict_day(prices,x,day,c))
        if i%15==0:logging.info('逐日价格回测已推进至 %s',day.date())
    return pd.concat(parts)

def metrics(oos,c):
    rows=[]
    for col in ['da','rt']:
        f=oos[[col,col+'_hat',col+'_lag'+str(c['lag_days'])]].dropna();ref=f.iloc[:,2]
        rows.append({'对象':{'da':'日前价格','rt':'实时价格'}[col],'MAE_元每MWh':float(abs(f[col]-f[col+'_hat']).mean()),'方向准确率':float((np.sign(f[col]-ref)==np.sign(f[col+'_hat']-ref)).mean()),'方向定义':f'相对 D-{c["lag_days"]} 同小时','样本小时':len(f)})
    valid=oos[['da','rt','da_hat','rt_hat']].dropna();a=valid.da-valid.rt;p=valid.da_hat-valid.rt_hat
    rows.append({'对象':'日前减实时价差','方向准确率':float((np.sign(a)==np.sign(p)).mean()),'方向定义':'价差正负，零单独一类','样本小时':len(valid)})
    return pd.DataFrame(rows)
