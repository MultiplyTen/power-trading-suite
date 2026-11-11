# -*- coding: utf-8 -*-
"""独立重读交付物并复算关键约束与指标。"""
from pathlib import Path
import json,zipfile
import numpy as np
import pandas as pd
from openpyxl import load_workbook
ROOT=Path(__file__).resolve().parent
p=ROOT/'output';a=json.loads((p/'audit_20260902.json').read_text(encoding='utf-8'));f=pd.read_csv(p/'walkforward_20260902.csv.gz',parse_dates=['timestamp','training_end','decision_cutoff'])
assert len(f)==90*24 and not f.timestamp.duplicated().any()
assert (f.training_end<f.decision_cutoff).all()
assert pd.Timestamp(a['训练窗口'][1])<pd.Timestamp(a['独立验证窗口'][0])-pd.Timedelta(days=1)
x=p/'coefficients_20260902.xlsx';wb=load_workbook(x,data_only=False)
assert len(wb.sheetnames)==8
for ws in wb:
 for row in ws:
  assert not any(c.data_type=='e' for c in row)
c=pd.read_excel(x,sheet_name='24小时系数');assert len(c)==24 and c['小时'].tolist()==list(range(24));assert (c['实际采用系数']==1).all()
np.testing.assert_allclose(c['研究申报电量_MWh'],c['研究系数']*c['预测负荷_MWh'])
assert c['研究系数'].between(.8,1.2).all();assert abs(np.diff(c['研究系数'])).max()<=.05000001
assert np.max(abs((c['研究系数']-.8)/.01-np.round((c['研究系数']-.8)/.01)))<1e-6
m=pd.read_excel(x,sheet_name='价格预测指标');test=f[f.timestamp>=pd.Timestamp(a['独立验证窗口'][0])]
for i,col in enumerate(['da','rt']):assert abs(m.iloc[i]['MAE_元每MWh']-abs(test[col]-test[col+'_hat']).mean())<1e-8
k=pd.read_excel(x,sheet_name='冻结验证系数')['冻结系数'].to_numpy();q=test.load_hat.to_numpy().reshape(-1,24)*k;l=test.actual_load.to_numpy().reshape(-1,24);spread=(test.rt-test.da).to_numpy().reshape(-1,24)
manual=((q-l)*spread).sum(axis=1) # 当前示例额外费率为0。
d=pd.read_excel(x,sheet_name='独立验证逐日收益');np.testing.assert_allclose(manual,d['实际收益']);assert len(d)==30
with zipfile.ZipFile(x) as z:assert z.testzip() is None
result={'status':'passed','sheets':8,'hourly_oos_rows':len(f),'coefficient_rows':24,'holdout_days':30,'quantity_and_pnl_recomputed':True,'time_embargo_checked':True,'formula_errors':0,'zip_integrity':'passed'}
(p/'delivery_checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
