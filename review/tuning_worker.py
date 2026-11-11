# -*- coding: utf-8 -*-
"""隔离加载各项目的真实模型接口；输出逐日预测，供建议模块配对比较。"""
import sys,json,copy
from pathlib import Path
import numpy as np
import pandas as pd
import yaml
job=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'));root=Path(job['project_root']);sys.path.insert(0,str(root));c=yaml.safe_load((root/'config.yaml').read_text(encoding='utf-8'));sys.path.insert(0,str(root.parent))
from database.trading import prices as db_prices, actual_load as db_actual_load
if job.get('parameter'):
 obj=c;parts=job['parameter'].split('.')
 for key in parts[:-1]:obj=obj[key]
 obj[parts[-1]]=job['value']
start=pd.Timestamp(job['start']);end=pd.Timestamp(job['end']);dest=Path(job['output'])
try:
 if job['task']=='load':
  from configuration import read_config
  from inputs.quality import load_inputs
  from weather.features import read_weather,groups
  from features.load import make_features
  from evaluation.rolling_backtest import dense,walk
  c['_root']=str(root);c['weather']['enable_network']=False
  if c['run']['observation_lag_days']<job['lag']+1:raise ValueError('负荷模型可用滞后小于实际回填滞后，不能作同口径调参比较')
  tables,_,_=load_inputs(c);h,_,_=read_weather(c,end);wx,_=groups(c,h,tables,end);s=dense(tables['portfolio'],end);spec=c['models']['portfolio'];c['training']['half_life_days']=spec.get('half_life_days',c['training']['half_life_days']);f,x=make_features(s,wx['portfolio'],c,spec['cooling_threshold'])
  if not spec.get('use_weather',True):x=x[[v for v in x if not v.startswith('wx_')]]
  p,_,trace=walk(f,x,c,start,end,spec['ridge_alpha'],spec['name'])
  result=pd.DataFrame({'actual':s,'prediction':p}).loc[start:end+pd.Timedelta(hours=23)];result.index.name='timestamp';result.to_csv(dest)
 elif job['task']=='price':
  from price_input import read_prices
  from load_input import historical_load
  from weather_features import get_weather
  from price_prediction import features,rolling
  c['network_enabled']=False
  if c['lag_days']<job['lag']+1:raise ValueError('价格特征滞后小于实际回填滞后，不能作同口径调参比较')
  prices,_=db_prices(root/c['db_path']);idx=pd.date_range(prices.index.min(),end+pd.Timedelta(hours=23),freq='h');prices=prices.reindex(idx)
  actual,load,_=db_actual_load(idx,c['lag_days'],root/c['db_path'])
  w=get_weather(c,idx,end,root);x=features(prices,load,w,c)
  begin=max(start-pd.Timedelta(days=125),prices.index.min().normalize()+pd.Timedelta(days=95+c['lag_days']))
  out=rolling(prices,x,begin,end,c).join(prices).join(actual.rename('actual_load')).join(load.rename('load_hat'));out['spread_error']=(out.da-out.rt)-(out.da_hat-out.rt_hat);out.index.name='timestamp';out.to_csv(dest)
 elif job['task']=='risk':
  from coefficient_optimization import optimize,pnl
  history=pd.read_csv(job['history'],index_col='timestamp',parse_dates=True);rows=[]
  for day in pd.date_range(start,end):
   last=day-pd.Timedelta(days=c['lag_days'])+pd.Timedelta(hours=23);h=history.loc[last.normalize()-pd.Timedelta(days=c['backtest_window_days']-1):last]
   required=['da','rt','da_hat','rt_hat','load_hat','actual_load','spread_error'];h=h[required]
   if len(h)!=24*c['backtest_window_days'] or h.isna().any().any():raise ValueError(f'{day.date()}：历史完整联合场景不足 {c["backtest_window_days"]} 天')
   t=history.loc[day:day+pd.Timedelta(hours=23)];f=np.broadcast_to(t.load_hat.to_numpy(),(len(h)//24,24));l=f+(h.actual_load-h.load_hat).to_numpy().reshape(-1,24);spread=(t.da_hat-t.rt_hat).to_numpy()+h.spread_error.to_numpy().reshape(-1,24);valid=(l>=0).all(axis=1)
   std=h.groupby(h.index.hour).spread_error.std().reindex(range(24)).to_numpy();k,a=optimize(f[valid],l[valid],spread[valid],c,std)
   # 无可行解只能退回中性，不能把不满足约束的研究解当执行收益。
   adopted=k if a['feasible'] else np.ones(24)
   profit=pnl(adopted,t.load_hat.to_numpy()[None,:],t.actual_load.to_numpy()[None,:],(t.da-t.rt).to_numpy()[None,:],c)[0]
   rows.append({'timestamp':day,'profit':profit,'feasible':a['feasible'],'limit':a['daily_loss_limit'],'scenario_days':len(h)//24})
  pd.DataFrame(rows).to_csv(dest,index=False)
 else:raise ValueError('未知回测任务')
except Exception as e:
 dest.with_suffix('.error.json').write_text(json.dumps({'error':str(e)},ensure_ascii=False),encoding='utf-8');raise
