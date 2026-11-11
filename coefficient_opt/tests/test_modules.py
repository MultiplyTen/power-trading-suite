# -*- coding: utf-8 -*-
import unittest,tempfile,sys
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from coefficient_optimization import cvar,weighted_cvar,haircut,pnl,objective,risk,optimize,optimize_exact,scenario_weights,neighborhood
from price_input import read_prices,parse
from load_input import read_load,historical_load
from price_prediction import predict_day
import yaml

ROOT=Path(__file__).resolve().parents[1]
class Contracts(unittest.TestCase):
 def setUp(self):self.c=yaml.safe_load((ROOT/'config.yaml').read_text(encoding='utf-8'))
 def test_direction_and_penalty(self):
  f=np.full((10,24),10.);s=np.full_like(f,20)
  self.assertGreater(pnl(np.full(24,.8),f,f,s,self.c).mean(),0)
  self.assertLess(pnl(np.full(24,1.2),f,f,s,self.c).mean(),0)
  for k in [.8,1.2]:self.assertTrue((pnl(np.full(24,k),f,f,s,self.c,np.ones(24)*5)<=pnl(np.full(24,k),f,f,s,self.c)).all())
 def test_symmetric_haircut(self):np.testing.assert_allclose(haircut(np.array([-20,20,-2]),np.array([5,5,5]),1),[-15,15,0])
 def test_empirical_cvar_fraction(self):self.assertAlmostEqual(cvar([0,10,20],.5),50/3)
 def test_profit_recovery_rule(self):
  # 获利回收：多报超上带且实时更贵(spread=DA-RT<0)时，超带部分收益被回收，pnl低于零费率固定口径。
  f=np.full((10,24),10.);s=np.full_like(f,-20.)
  pr=dict(self.c,deviation_rule='profit_recovery');ff=dict(self.c,deviation_rule='fixed_fee',deviation_cost_per_mwh=0.0)
  q_high=np.full(24,1.5);q_band=np.full(24,1.2)
  # 超带3MWh/小时×20元×24小时=1440元/日的回收差。
  self.assertAlmostEqual(float((pnl(q_high,f,f,s,ff)-pnl(q_high,f,f,s,pr)).mean()),3*20.*24)
  # 恰在带边界（1.2倍）时无超带量，两口径一致。
  np.testing.assert_allclose(pnl(q_band,f,f,s,pr),pnl(q_band,f,f,s,ff))
 def test_auto_limit_cvar_floor(self):
  # 基准日均收益≈0时旧口径限额≈0；新口径限额=CVaR锚>0，不再把所有解误判不可行。
  f=np.ones((30,24))*10.;l=f*1.05
  spread=np.concatenate([np.full((15,24),4.),np.full((15,24),-4.)])
  k,r=optimize(f,l,spread,dict(self.c,daily_loss_limit='auto'),np.zeros(24))
  ref=pnl(np.ones(24),f,l,spread,self.c)
  self.assertAlmostEqual(ref.mean(),0.,places=6)
  self.assertGreater(r['daily_loss_limit'],0.)
  self.assertAlmostEqual(r['daily_loss_limit'],max(2*abs(float(ref.mean())),cvar(-ref,self.c['cvar_alpha'])))
 def test_weighted_cvar_consistency(self):
  loss=np.array([0.,5.,10.,20.])
  self.assertAlmostEqual(weighted_cvar(loss,.9),cvar(loss,.9))  # 等权与离散CVaR一致
  self.assertAlmostEqual(weighted_cvar(loss,.9,np.array([.7,.1,.1,.1])),20.)  # 手算：z=10或20处取最小
 def test_exact_solver_dominates_grid(self):
  # LP精确解目标>=网格坐标搜索解与k=1基准（可行域内全局最优），且满足平滑与系数域。
  rng=np.random.default_rng(11)
  f=np.full((45,24),10.)*rng.uniform(.9,1.1,(45,24));l=f*rng.uniform(.85,1.15,(45,24))
  s=rng.normal(0,40,(45,24));std=np.abs(rng.normal(10,3,24))+1
  k_g,r_g=optimize(f,l,s,self.c,std,limit=1e9)
  k_x,info=optimize_exact(f,l,s,self.c,std,1e9)
  self.assertGreaterEqual(info['objective'],r_g['objective']-1e-6)
  k1=objective(pnl(np.full(24,1.),f,l,s,self.c,std),self.c)
  self.assertGreaterEqual(info['objective'],k1-1e-6)
  self.assertLessEqual(np.abs(np.diff(k_x)).max(),self.c['smooth_limit']+1e-7)
  self.assertTrue(((k_x>=self.c['k_min']-1e-8)&(k_x<=self.c['k_max']+1e-8)).all())
 def test_exact_enforces_exceed_chance_constraint(self):
  # 12%场景日巨亏且均值收益显著为正：LP最优解必超限，MILP必须介入压回超限预算内。
  f=np.ones((50,24))*10.;l=f.copy()
  s=np.full((50,24),500.);s[:6]=-500.  # 6/50=12%>10%超限预算；少报在高价差日巨亏
  std=np.full(24,4.)
  k_x,info=optimize_exact(f,l,s,self.c,std,50.)
  self.assertIsNotNone(k_x)
  self.assertLessEqual(info['exceed_probability'],1-self.c['confidence']+1e-9)
  self.assertTrue(info['feasible'])
  self.assertTrue(info.get('exceed_enforced'))  # 必须走了MILP分支（若LP自身可行则此断言失败）
 def test_milp_infeasible_reports_min_exceed(self):
  # 负限额+15%场景日超限：任何k的最小加权超限=15%>10%预算，MILP不可行，
  # 辅助问题给出min_exceed=0.15参考值（量化距预算差距），并回退LP解如实标注。
  f=np.ones((40,24))*10.;l=f.copy()
  s=np.full((40,24),50.);s[:6]=-100.  # 6/40=15%场景日无论如何调k都超负限额
  std=np.full(24,2.)
  k,info=optimize_exact(f,l,s,self.c,std,limit=-1.0)
  self.assertFalse(info['feasible'])
  self.assertIsNotNone(info.get('min_exceed_probability'))
  self.assertAlmostEqual(info['min_exceed_probability'],0.15,places=6)
  self.assertFalse(info.get('exceed_enforced'))
 def test_scenario_weights_and_neighborhood(self):
  w=scenario_weights(np.array(['2026-01-01','2026-01-31'],dtype='datetime64[D]'),30)
  self.assertAlmostEqual(w.sum(),1.);self.assertGreater(w[1],w[0])
  self.assertIsNone(scenario_weights(np.array(['2026-01-01']),None))
  rng=np.random.default_rng(3)
  f=np.full((40,24),10.)*rng.uniform(.95,1.05,(40,24));l=f*1.02;s=rng.normal(0,30,(40,24));std=np.full(24,4.)
  k_x,info=optimize_exact(f,l,s,self.c,std,1e9)
  up,dn=neighborhood(k_x,f,l,s,self.c,std)
  fin=np.concatenate([up[np.isfinite(up)],dn[np.isfinite(dn)]])
  self.assertGreaterEqual(np.nanmin(fin),-1e-6)  # LP最优性：可行邻域不应改善目标
  self.assertTrue(np.isfinite(fin).any())
 def test_probability(self):
  self.assertFalse(risk([-100,-100,0,0,0,0,0,0,0,0],self.c,50)['feasible'])
  self.assertTrue(risk([-100,0,0,0,0,0,0,0,0,0],self.c,50)['feasible'])
 def test_joint_constraints(self):
  f=np.ones((30,24))*10;s=np.tile(np.r_[np.ones(12)*20,np.ones(12)*-20],(30,1));k,r=optimize(f,f,s,self.c,np.zeros(24),1000)
  self.assertLessEqual(abs(np.diff(k)).max(),.0500001);self.assertTrue(((k>=.8)&(k<=1.2)).all());self.assertTrue(r['feasible'])
  self.assertAlmostEqual(r['objective'],risk(pnl(k,f,f,s,self.c,np.zeros(24)),self.c,1000)['objective'])
 def test_cache_no_parse_and_update(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'p.xlsx';db=Path(d)/'c.db';f=pd.DataFrame({'目标时刻':pd.date_range('2026-01-01',periods=24,freq='h'),'日前价格':1.,'实时价格':2.});f.to_excel(p,index=False)
   a,_=read_prices(p,db)
   with patch('price_input.parse',side_effect=AssertionError('不得重解析')):b,_=read_prices(p,db)
   pd.testing.assert_frame_equal(a,b);f.loc[0,'日前价格']=9;f=f.iloc[:-1];f.to_excel(p,index=False);a,_=read_prices(p,db);self.assertEqual(a.iloc[0].da,9);self.assertEqual(len(a),23)
 def test_two_sheets(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'p.xlsx'
   with pd.ExcelWriter(p) as w:
    for n in ['日前','实时']:pd.DataFrame({'日期':['2026-01-01'],'小时':[0],'价格':[4]}).to_excel(w,sheet_name=n,index=False)
   self.assertEqual(parse(p).shape,(1,2))
 def test_missing_load_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'p.xlsx';pd.DataFrame({'小时':range(23),'负荷预测':1}).to_excel(p,sheet_name='负荷预测',index=False)
   with self.assertRaises(ValueError):read_load(None,p,'负荷预测',pd.Timestamp('2026-01-01'))
 @unittest.skipUnless((ROOT/'data/price.xlsx').exists() and (ROOT.parent/'database'/'master.db').exists(),'依赖本地数据库与示例数据文件（发布版仓库自动跳过）')
 def test_companion_load(self):
  s,_=read_load(ROOT/'data/predict_YYYYMMDD.xlsx',ROOT/'data/price.xlsx','负荷预测',pd.Timestamp('2026-09-02'));self.assertEqual(len(s),24);self.assertTrue((s>=0).all())
 def test_no_future_target_leakage(self):
  idx=pd.date_range('2025-01-01',periods=200*24,freq='h');p=pd.DataFrame({'da':np.sin(np.arange(len(idx))/24)+100,'rt':np.cos(np.arange(len(idx))/24)+100},index=idx);x=pd.DataFrame({'a':np.arange(len(idx))},index=idx);day=idx[-24]
  a=predict_day(p,x,day,self.c);p.loc[p.index>day-pd.Timedelta(days=2)+pd.Timedelta(hours=23)]=999999;b=predict_day(p,x,day,self.c);pd.testing.assert_frame_equal(a,b)
 def test_load_baseline_is_causal(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'l.csv';idx=pd.date_range('2026-01-01',periods=40*24,freq='h');f=pd.DataFrame({'timestamp':idx,'actual_load':np.arange(len(idx))+1});f.to_csv(p,index=False);_,a,_=historical_load(p,idx);f.loc[f.index>=35*24,'actual_load']=999999;f.to_csv(p,index=False);_,b,_=historical_load(p,idx);pd.testing.assert_series_equal(a.loc[:idx[36*24]],b.loc[:idx[36*24]])
if __name__=='__main__':unittest.main()
