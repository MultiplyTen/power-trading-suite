# -*- coding: utf-8 -*-
"""隔离加载各项目的真实模型接口；输出逐日预测，供建议模块配对比较。"""
import sys,json,copy,hashlib
from pathlib import Path
import numpy as np
import pandas as pd
import yaml
job=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'));root=Path(job['project_root']);sys.path.insert(0,str(root));c=yaml.safe_load((root/'config.yaml').read_text(encoding='utf-8'));sys.path.insert(0,str(root.parent))
from database.trading import prices as db_prices, actual_load as db_actual_load
reference_c=copy.deepcopy(c)
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
  from reference_risk import risk_scenarios,evaluate_reference
  history=pd.read_csv(job['history'],index_col='timestamp',parse_dates=True);rows=[]
  reference_path=job.get('reference_risk');reference_rows=None;reference_sha='self'
  if job.get('parameter') and not reference_path:raise ValueError('风险候选缺少原配置基准风险证据')
  if reference_path:
   reference_sha=hashlib.sha256(Path(reference_path).read_bytes()).hexdigest()
   if reference_sha!=job.get('reference_risk_sha256'):raise ValueError('原配置基准风险输出哈希不匹配')
   reference_rows=pd.read_csv(reference_path,parse_dates=['timestamp']).set_index('timestamp')
   if reference_rows.index.has_duplicates:raise ValueError('基准风险证据重复自然日')
  for day in pd.date_range(start,end):
   own=risk_scenarios(history,day,c);f,l,spread,std,t,_=own;k,a=optimize(f,l,spread,c,std)
   # 仅研究回测：无可行解退回中性；此曲线不是实际成交或采用记录。
   evaluated_k=k if a['feasible'] else np.ones(24)
   profit=pnl(evaluated_k,t.load_hat.to_numpy()[None,:],t.actual_load.to_numpy()[None,:],(t.da-t.rt).to_numpy()[None,:],c)[0]
   frozen=own if not job.get('parameter') else risk_scenarios(history,day,reference_c)
   limit=a['daily_loss_limit'] if reference_rows is None else float(reference_rows.loc[day,'reference_limit'])
   reference=evaluate_reference(evaluated_k,frozen,reference_c,limit,pnl)
   if reference_rows is not None:
    previous=reference_rows.loc[day]
    for key in ['reference_scenario_hash','reference_config_hash','reference_weighting']:
     if reference[key]!=previous[key]:raise ValueError('候选与原配置基准的场景/参数版本不一致：'+key)
    for key in ['reference_confidence','reference_error_shrink','reference_window_days','reference_scenario_days']:
     if abs(float(reference[key])-float(previous[key]))>1e-10:raise ValueError('候选风险比较的原约束不一致：'+key)
   rows.append({'timestamp':day,'profit':profit,'feasible':a['feasible'],'limit':a['daily_loss_limit'],
                'scenario_days':len(f),'period_hours':len(t),'reference_baseline_sha256':reference_sha,**reference})
  pd.DataFrame(rows).to_csv(dest,index=False)
 else:raise ValueError('未知回测任务')
except Exception as e:
 dest.with_suffix('.error.json').write_text(json.dumps({'error':str(e)},ensure_ascii=False),encoding='utf-8');raise
