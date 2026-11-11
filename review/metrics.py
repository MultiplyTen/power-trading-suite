# -*- coding: utf-8 -*-
import numpy as np
import pandas as pd
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database.runtime import day_details
from attribution import decompose,gain
import json

def cvar(loss,alpha=.9):
 x=np.asarray(loss);z=np.quantile(x,alpha);return float(z+np.maximum(x-z,0).mean()/(1-alpha))
def analyze(frame,baseline=None):
 if frame.empty:return {'summary':pd.DataFrame(),'hourly':pd.DataFrame(),'calendar':pd.DataFrame(),'daily':pd.DataFrame(),'attribution':pd.DataFrame(),'sensitivity':pd.DataFrame(),'weakness':'没有已齐全的预测—实际对账记录'}
 load=frame[frame.kind.eq('load')].copy();price=frame[frame.kind.eq('coefficient')].copy();rows=[];groups=[]
 if not load.empty:
  load['error']=load.load-load.actual_load;valid=load.actual_load.ne(0);load['ape']=np.where(valid,abs(load.error)/load.actual_load.abs(),np.nan)
  denom=float(load.actual_load.abs().sum())
  # 主指标改为WMAPE（Σ|误差|/Σ|实际|，零实际不炸分母）；MAPE保留作对照。
  rows.extend([{'指标':'负荷WMAPE','值':float(load.error.abs().sum()/denom) if denom>0 else None,'有效小时':len(load)},{'指标':'负荷MAPE','值':load.ape.mean(),'有效小时':int(valid.sum())},{'指标':'负荷RMSE','值':np.sqrt(np.mean(load.error**2)),'有效小时':len(load)},{'指标':'零实际负荷排除MAPE小时','值':int((~valid).sum())}])
  load['日类型']=['工作日' if day_details(t.date())[0] else '法定节假日' if day_details(t.date())[1] else '周末' for t in load.target]
  for key,g in load.groupby('日类型'):
   gd=float(g.actual_load.abs().sum());groups.append({'日类型':key,'WMAPE':float(g.error.abs().sum()/gd) if gd>0 else None,'MAPE':g.ape.mean(),'平均偏差':g.error.mean(),'样本小时':len(g)})
 hourly=[]
 for h in range(24):
  a=load[load.target.dt.hour.eq(h)];b=price[price.target.dt.hour.eq(h)];r={'小时':h,'负荷平均偏差':a.error.mean() if len(a) else np.nan,'负荷样本数':len(a)}
  if len(b):r.update({'价格样本数':len(b),'日前MAE':abs(b.da-b.actual_da).mean(),'实时MAE':abs(b.rt-b.actual_rt).mean(),'价差MAE':abs((b.da-b.rt)-(b.actual_da-b.actual_rt)).mean(),'价差方向准确率':float((np.sign(b.da-b.rt)==np.sign(b.actual_da-b.actual_rt)).mean())})
  hourly.append(r)
 if not price.empty:
  rows.append({'指标':'价格有效样本小时','值':len(price),'有效小时':len(price)})
  acc=float((np.sign(price.da-price.rt)==np.sign(price.actual_da-price.actual_rt)).mean())
  # 无条件多数基准：预测恒定指向对账窗口内实际价差的多数方向，其准确率=max(P(日前>实时),P(实时>日前))。
  actual_spread=price.actual_da-price.actual_rt
  majority=max(float((actual_spread>0).mean()),float((actual_spread<0).mean()))
  if baseline is None:baseline=majority
  rows.extend([{'指标':'价差方向准确率','值':acc},{'指标':'无条件多数基准准确率','值':majority},{'指标':'相比多数基准变化','值':acc-baseline},{'指标':'日前MAE','值':abs(price.da-price.actual_da).mean()},{'指标':'实时MAE','值':abs(price.rt-price.actual_rt).mean()},{'指标':'价差MAE','值':abs((price.da-price.rt)-(price.actual_da-price.actual_rt)).mean()}])
 attributed=decompose(price);daily=pd.DataFrame();sens=[]
 if not attributed.empty:
  attributed['日期']=pd.to_datetime(attributed['目标时刻']).dt.normalize()
  counts=attributed.groupby('日期').size();complete=counts[counts.eq(24)].index;attributed=attributed[attributed['日期'].isin(complete)]
  if len(attributed):
   daily=attributed.drop(columns='目标时刻').groupby('日期').sum(numeric_only=True).reset_index();daily['累计收益']=daily['实际收益'].cumsum();daily['回撤']=daily['累计收益']-np.maximum.accumulate(np.r_[0,daily['累计收益']])[1:]
   cost=daily['实际日前采购基准成本'].sum();rows.extend([{'指标':'累计收益','值':daily['实际收益'].sum()},{'指标':'日均收益','值':daily['实际收益'].mean()},{'指标':'日胜率','值':daily['实际收益'].gt(0).mean()},{'指标':'最大回撤金额','值':-daily['回撤'].min()},{'指标':'CVaR90日损失','值':cvar(-daily['实际收益'])},{'指标':'累计超额收益','值':daily['超额收益'].sum()},{'指标':'收益率_相对实际日前采购成本','值':daily['实际收益'].sum()/cost if cost>0 else np.nan}])
   for k in [.8,.9,1,1.1,1.2]:
    vals=[]
    for d,g in price.groupby(price.target.dt.normalize()):
     if len(g)!=24:continue
     vals.append(sum(gain(r.load*k,r.actual_load,r.actual_da,r.actual_rt,json.loads(r.config_json)) for _,r in g.iterrows()))
    sens.append({'系数':k,'累计收益':sum(vals),'日均收益':np.mean(vals),'CVaR90日损失':cvar(-np.array(vals))})
 weakness='数据不足'
 if not attributed.empty:
  cols=['负荷误差贡献','价格误差贡献','系数选择贡献'];weakness=min(cols,key=lambda col:attributed[col].sum())+'（按三因素Shapley值累计负贡献；效率性保证三因素之和=实际收益−中性预测收益；仍不等同因果结论）'
 return {'summary':pd.DataFrame(rows),'hourly':pd.DataFrame(hourly),'calendar':pd.DataFrame(groups),'daily':daily,'attribution':attributed,'sensitivity':pd.DataFrame(sens),'weakness':weakness}
