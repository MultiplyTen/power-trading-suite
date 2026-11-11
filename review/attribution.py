# -*- coding: utf-8 -*-
"""三因素Shapley归因；效率性恒等式可复算，不宣称因果识别。"""
import json
import numpy as np
import pandas as pd

def gain(q,l,da,rt,c):
 """(Q-L)*(RT-DA)-偏差考核。config_json 含 deviation_rule=profit_recovery 时按获利回收
 （±20%带外按方向价差回收，2026-10-14业务确认）；旧记录无该键保持固定费率口径以便复现。"""
 upper=np.maximum(q-c.get('deviation_upper_ratio',1.2)*l,0);lower=np.maximum(c.get('deviation_lower_ratio',.8)*l-q,0)
 if c.get('deviation_rule','fixed_fee')=='profit_recovery':
  # 多报超带且实时更贵：回收upper*(RT-DA)；少报超带且日前更贵：回收lower*(DA-RT)。
  penalty=upper*np.maximum(rt-da,0)+lower*np.maximum(da-rt,0)
 else:penalty=(upper+lower)*c.get('deviation_cost_per_mwh',0)
 return (q-l)*(rt-da)-penalty

def decompose(f):
 rows=[]
 for _,r in f.iterrows():
  c=json.loads(r.config_json);F=r.load;L=r.actual_load;k=r.k
  if not all(np.isfinite(v) for v in [F,L,k,r.da,r.rt,r.actual_da,r.actual_rt]):continue
  # 三因素Shapley：A=系数选择(F→F·k) B=实际负荷(F→L) C=实际价格(预测→实际)。
  # φ_i=Σ_S |S|!(3-|S|-1)!/3!·[v(S∪i)−v(S)]；效率性恒等式：φA+φB+φC≡v(ABC)−v(∅)。
  # 旧版路径分解(系数→负荷→价格的固定替换顺序)的顺序偏差在Shapley全排列平均下消除。
  v0 =gain(F  ,F,r.da,        r.rt,        c)
  vA =gain(F*k,F  ,r.da,        r.rt,        c);vB=gain(F  ,L,r.da,        r.rt,        c);vC=gain(F  ,F,r.actual_da,r.actual_rt,c)
  vAB=gain(F*k,L  ,r.da,        r.rt,        c);vAC=gain(F*k,F,r.actual_da,r.actual_rt,c);vBC=gain(F  ,L,r.actual_da,r.actual_rt,c)
  vABC=gain(F*k,L,r.actual_da,r.actual_rt,c)
  phi_A=(vA-v0)/3+(vAB-vB)/6+(vAC-vC)/6+(vABC-vBC)/3
  phi_B=(vB-v0)/3+(vAB-vA)/6+(vBC-vC)/6+(vABC-vAC)/3
  phi_C=(vC-v0)/3+(vAC-vA)/6+(vBC-vB)/6+(vABC-vAB)/3
  rows.append({'目标时刻':r.target,'系数选择贡献':phi_A,'负荷误差贡献':phi_B,'价格误差贡献':phi_C,
   '预计收益':vA,'收益预测误差':vABC-vA,'实际收益':vABC,'中性收益':vBC,'超额收益':vABC-vBC,
   '分解残差':vABC-v0-(phi_A+phi_B+phi_C),'实际日前采购基准成本':L*r.actual_da})
 return pd.DataFrame(rows)
