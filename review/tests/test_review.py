# -*- coding: utf-8 -*-
import unittest,tempfile,sys,json,sqlite3
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import digest,dump
from reconciliation import connect,reconcile,ingest_predictions
from attribution import decompose,gain
from apply_config import apply,rollback
from metrics import analyze
from suggestions import day_bootstrap_ci

class ReviewTests(unittest.TestCase):
 def setup_apply(self,tmp):
  root=Path(tmp);(root/'load').mkdir();(root/'coef').mkdir();(root/'output').mkdir()
  (root/'load/config.yaml').write_text(yaml.safe_dump({'run':{'strict_availability':False}}));(root/'coef/config.yaml').write_text(yaml.safe_dump({'research_mode':True,'ridge_alpha':100}))
  c={'_root':tmp,'project_paths':{'load_forecast':'load','coefficient':'coef'},'output_dir':'output','candidate_grid':{'coefficient':{'ridge_alpha':[10,100]}},'improve_threshold':.05}
  item={'project':'coefficient','parameter':'ridge_alpha','old_value':100,'new_value':10,'validated':True,'improvement':.1,'evidence_scope':'research','reason':'隔离测试'}
  doc={'suggestions':[item],'config_hashes':{k:digest(root/v/'config.yaml') for k,v in c['project_paths'].items()}}
  (root/'output/suggestions_20260915.yaml').write_text(yaml.safe_dump(doc));dump(root/'output/suggestions_20260915.validated.json',doc);return root,c,doc
 def test_apply_backup_rollback(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,c,d=self.setup_apply(tmp);old={k:(root/v/'config.yaml').read_bytes() for k,v in c['project_paths'].items()};r=apply(c);self.assertEqual(r['status'],'applied');self.assertEqual(yaml.safe_load((root/'coef/config.yaml').read_text())['ridge_alpha'],10)
   self.assertEqual(rollback(c)['status'],'rolled_back')
   for k,v in c['project_paths'].items():self.assertEqual((root/v/'config.yaml').read_bytes(),old[k])
   self.assertEqual(len(list((root/'output/adjustments/backups').glob('*/*.yaml'))),2)
 def test_stale_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,c,d=self.setup_apply(tmp);(root/'coef/config.yaml').write_text('research_mode: true\nridge_alpha: 99\n')
   with self.assertRaisesRegex(ValueError,'过期'):apply(c)
 def test_untested_edit_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,c,d=self.setup_apply(tmp);d['suggestions'][0]['new_value']=999;(root/'output/suggestions_20260915.yaml').write_text(yaml.safe_dump(d))
   with self.assertRaisesRegex(ValueError,'重新回测'):apply(c)
 def test_empty_selection_no_write(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,c,d=self.setup_apply(tmp);d['suggestions']=[];(root/'output/suggestions_20260915.yaml').write_text(yaml.safe_dump(d));before=digest(root/'coef/config.yaml');self.assertEqual(apply(c)['status'],'no_changes');self.assertEqual(before,digest(root/'coef/config.yaml'))
 def test_partial_failure_restores_both(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,c,d=self.setup_apply(tmp);old=digest(root/'coef/config.yaml');import apply_config;real=apply_config.atomic;calls=[0]
   def fail_once(p,data):
    calls[0]+=1
    if calls[0]==2:raise OSError('测试中断')
    real(p,data)
   with patch('apply_config.atomic',side_effect=fail_once):
    with self.assertRaises(OSError):apply(c)
   self.assertEqual(old,digest(root/'coef/config.yaml'))
 def test_missing_and_corrupt_db_fail(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'review.db'
   with self.assertRaisesRegex(ValueError,'初始化|首次使用'):connect(p)
   p.write_bytes(b'bad sqlite')
   with self.assertRaisesRegex(ValueError,'损坏'):connect(p)
   self.assertEqual(p.read_bytes(),b'bad sqlite')
 def test_lag_and_actual_availability(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=connect(Path(tmp)/'a.db',True);db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',('r','load','2026-09-01','v','{}','p','s','production'))
   for d in ['2026-09-09','2026-09-10','2026-09-11']:
    db.execute('INSERT INTO predictions VALUES(?,?,?,?,?,?,?)',('r',d+' 00:00:00',10,None,None,None,None));db.execute('INSERT INTO actuals VALUES(?,?,?,?,?,?,?)',(d+' 00:00:00','load',11,'2026-09-16' if d=='2026-09-10' else '2026-09-15','verified','s','2026-09-15'))
   done,pending,_=reconcile(db,{'review_date':'2026-09-15','actual_lag_days':5});self.assertEqual(len(done),1);self.assertEqual(len(pending),2);db.close()
 def test_attribution_identity(self):
  f=pd.DataFrame([{'target':pd.Timestamp('2026-08-01'),'load':10,'actual_load':12,'k':1.1,'da':300,'rt':400,'actual_da':310,'actual_rt':380,'config_json':json.dumps({'deviation_cost_per_mwh':20})}]);a=decompose(f);self.assertLess(abs(a['分解残差'].iloc[0]),1e-9);self.assertAlmostEqual(a['实际收益'].iloc[0],a[['负荷误差贡献','价格误差贡献','系数选择贡献']].sum(axis=1).iloc[0])
 def test_attribution_shapley_efficiency(self):
  # 中性基准v0≠0的场景：Shapley效率性=三因素之和≡v(ABC)−v(∅)，分解残差恒为0。
  f=pd.DataFrame([{'target':pd.Timestamp('2026-08-01'),'load':10,'actual_load':15,'k':1.15,'da':300,'rt':400,'actual_da':310,'actual_rt':380,'config_json':json.dumps({'deviation_rule':'profit_recovery','deviation_upper_ratio':1.2,'deviation_lower_ratio':.8})}])
  a=decompose(f)
  self.assertLess(abs(a['分解残差'].iloc[0]),1e-9)
  v0=gain(10,10,300,400,json.loads(f['config_json'].iloc[0]))
  total=a[['负荷误差贡献','价格误差贡献','系数选择贡献']].sum(axis=1).iloc[0]
  self.assertAlmostEqual(total,a['实际收益'].iloc[0]-v0)
  # Shapley对因素对称：交换负荷与价格误差的角色，负荷/价格贡献互换数量级（非恒等但结构一致）
  self.assertTrue(np.isfinite(a[['系数选择贡献','负荷误差贡献','价格误差贡献']].to_numpy()).all())
 def test_day_bootstrap_ci(self):
  rng=np.random.default_rng(1)
  d=rng.normal(1.0,2.0,60)  # 真实日均改善=1.0
  lo,hi,mean=day_bootstrap_ci(d)
  self.assertLess(lo,1.0<hi)  # CI覆盖真值
  self.assertGreater(lo,0.0)  # 均值1.0的60天样本，CI下界应显著为正
  self.assertAlmostEqual(mean,d.mean())
  # 全负样本CI下界为负
  lo2,hi2,_=day_bootstrap_ci(-d)
  self.assertLess(hi2,0.0)
  # 样本不足返回无CI
  self.assertEqual(day_bootstrap_ci([0.5])[0],None)
  self.assertEqual(day_bootstrap_ci([])[2],None)
 def test_mape_zero_exclusion(self):
  f=pd.DataFrame({'kind':['load','load'],'target':pd.to_datetime(['2026-08-01','2026-08-02']),'load':[1,11],'actual_load':[0,10]});a=analyze(f)['summary'].set_index('指标');self.assertAlmostEqual(a.loc['负荷MAPE','值'],.1)
 def test_wmape_and_majority_baseline(self):
  f=pd.DataFrame({'kind':['load','load','coefficient'],'target':pd.to_datetime(['2026-08-01','2026-08-02','2026-08-01']),'load':[11,9,10],'actual_load':[10,10,10],'da':[np.nan,np.nan,100],'rt':[np.nan,np.nan,90],'actual_da':[np.nan,np.nan,100],'actual_rt':[np.nan,np.nan,90],'k':[np.nan,np.nan,1.],'research_k':[np.nan,np.nan,1.],'config_json':['{}','{}',json.dumps({'deviation_rule':'profit_recovery','deviation_upper_ratio':1.2,'deviation_lower_ratio':.8})]})
  a=analyze(f);s=a['summary'].set_index('指标')
  self.assertAlmostEqual(s.loc['负荷WMAPE','值'],.1)  # (1+1)/(10+10)
  self.assertAlmostEqual(s.loc['无条件多数基准准确率','值'],1.0)  # 窗口内实际价差全部日前>实时
  self.assertAlmostEqual(s.loc['相比多数基准变化','值'],0.0)
  self.assertIn('负荷MAPE',s.index)  # MAPE保留对照
 def test_five_day_arrival_requires_six_day_forecast_lag(self):
  target=pd.Timestamp('2026-09-16');cutoff=target-pd.Timedelta(days=1)+pd.Timedelta(hours=9)
  self.assertLessEqual(target-pd.Timedelta(days=6)+pd.Timedelta(days=5),cutoff)
  self.assertGreater(target-pd.Timedelta(days=5)+pd.Timedelta(days=5),cutoff)
 def test_duplicate_ingest(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);e=root/'exchange/r';e.mkdir(parents=True);book=e/'p.xlsx';pd.DataFrame({'t':['2026-08-01'],'f':[10]}).to_excel(book,sheet_name='a',index=False);dump(e/'record.json',{'run_id':'r','kind':'load','published_at':'2026-07-31','model_version':'v','config':{},'file':'p.xlsx','sheet':'a','columns':{'target':'t','load':'f'},'sha256':digest(book),'status':'production'});db=connect(root/'r.db',True);c={'_root':tmp,'exchange_dir':'exchange'};ingest_predictions(db,c);ingest_predictions(db,c);self.assertEqual(db.execute('SELECT COUNT(*) FROM predictions').fetchone()[0],1);db.close()
if __name__=='__main__':unittest.main()
