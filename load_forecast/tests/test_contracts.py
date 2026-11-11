import copy,hashlib,json,tempfile,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from openpyxl import Workbook,load_workbook
from configuration import read_config,validate
from inputs.excel_reader import normalize
from inputs.quality import readiness
from features.load import baseline,make_features
from models.base import LinearPlugin
from strategy.optimizer import gain,recovery,decide,daily_constraints
from reports.writeback import append_record
from reports.excel import write_book
from reports.html_report import render
from weather.features import read_weather,VARS
from evaluation.rolling_backtest import walk

ROOT=Path(__file__).resolve().parents[1]

class Contracts(unittest.TestCase):
    def setUp(self):self.c=read_config(ROOT/'config.historical.yaml')
    def normalize(self,f,**kw):return normalize(f,'portfolio','MWh','interval_energy',**kw)
    def test_24_wide_matches_long(self):
        a={'日期':['2026-08-01']};a.update({str(h):[h] for h in range(24)})
        w,_=self.normalize(pd.DataFrame(a));l,_=self.normalize(pd.DataFrame({'date':['2026-08-01']*24,'hour':range(24),'value':range(24)}))
        np.testing.assert_array_equal(w.value,l.value);np.testing.assert_array_equal(w.valid_time,l.valid_time)
    def test_96_energy_and_power_and_midnight(self):
        f={'日期':['2026-08-01']};f.update({f'{m//60:02d}:{m%60:02d}':[1.] for m in range(15,1441,15)})
        a,n=self.normalize(pd.DataFrame(f),label='interval_end');b,_=normalize(pd.DataFrame(f),'system','MW','interval_mean_power',label='interval_end')
        self.assertEqual(len(n),96);self.assertEqual(len(a),24);self.assertTrue((a.value==4).all());self.assertTrue((b.value==1).all());self.assertEqual(a.valid_time.iloc[-1],pd.Timestamp('2026-08-01 23:00'))
    def test_missing_quarter_not_three_quarter_energy(self):
        f=pd.DataFrame({'date':['2026-08-01']*3,'time':['00:00','00:15','00:30'],'value':[1,1,1]});a,_=self.normalize(f);self.assertTrue(a.value.isna().all())
    def test_zero_negative_price_and_price_aggregation(self):
        f=pd.DataFrame({'date':['2026-08-01']*4,'time':['00:00','00:15','00:30','00:45'],'value':[0,-20,-30,-10]})
        with self.assertRaisesRegex(ValueError,'15分钟价格'):normalize(f,'da','CNY_per_MWh','price')
        a,_=normalize(f,'da','CNY_per_MWh','price',price_method='simple_mean_proxy');self.assertEqual(a.value.iloc[0],-15)
        f=f.iloc[:1].copy();a,_=self.normalize(f);self.assertEqual(a.value.iloc[0],0)
    def test_alias_conflicts_and_wrong_columns(self):
        with self.assertRaisesRegex(ValueError,'冲突列'):self.normalize(pd.DataFrame({'date':['2026-08-01'],'日期':['2026-08-01'],'time':[0],'value':[1]}))
        with self.assertRaisesRegex(ValueError,'缺少日期'):self.normalize(pd.DataFrame({'wrong':[1]}))
    def test_duplicate_conflict_and_version(self):
        f=pd.DataFrame({'date':['2026-08-01']*2,'hour':[0,0],'value':[1,2]})
        with self.assertRaisesRegex(ValueError,'冲突重复'):self.normalize(f)
        f['version']=['v1','v2']
        with self.assertRaisesRegex(ValueError,'多版本'):self.normalize(f)
    def test_unit_conversion(self):
        a,_=normalize(pd.DataFrame({'date':['2026-08-01'],'hour':[0],'value':[1000]}),'c','kWh','interval_energy');self.assertEqual(a.value.iloc[0],1)
    def test_hand_baseline(self):
        idx=pd.date_range('2026-05-01',periods=90*24,freq='h');s=pd.Series(3.,index=idx);s.iloc[-48:]=99999
        self.assertAlmostEqual(baseline(s,self.c).iloc[-24],3)
    def test_future_actual_invariance_and_save_load(self):
        idx=pd.date_range('2026-04-01','2026-07-05 23:00',freq='h');s=pd.Series(30+np.sin(np.arange(len(idx))/24),index=idx);w=pd.DataFrame(1.,index=idx,columns=VARS);w.temperature_2m=28
        f,x=make_features(s,w,self.c,26);p,m,_=walk(f,x,self.c,'2026-07-01','2026-07-01',100,'baseline_ridge')
        changed=s.copy();changed.loc['2026-06-30':]=900000;f2,x2=make_features(changed,w,self.c,26);p2,_,_=walk(f2,x2,self.c,'2026-07-01','2026-07-01',100,'baseline_ridge')
        np.testing.assert_allclose(p.loc['2026-07-01'],p2.loc['2026-07-01'])
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'model.joblib';m.save(path);n=LinearPlugin.load(path,'2026-06-29 23:00');np.testing.assert_allclose(m.predict(x.tail(24)),n.predict(x.tail(24)))
            with self.assertRaisesRegex(ValueError,'晚于'):LinearPlugin.load(path,'2026-06-01')
    def test_recovery_and_gain_hand_cases(self):
        c=self.c['strategy'];self.assertEqual(recovery(130,100,10,c),100);self.assertEqual(gain(130,100,10,c),200);self.assertEqual(gain(70,100,-10,c),200);self.assertEqual(gain(90,100,10,c),-100)
        self.assertEqual(gain(100,100,10,c)-gain(100,100,10,c),0)
    def test_optimizer_neutral_and_invalid(self):
        empty=pd.DataFrame(columns=['load_error','spread_error']);c=self.c['strategy']
        for f,p,s in [(100,.51,20),(100,.8,-20),(100,.8,20)]:self.assertEqual(decide(f,p,s,empty,c,True)['adopted_k'],1)
        self.assertTrue(np.isnan(decide(np.nan,.8,20,empty,c,True)['declaration_quantity']))
        hist=pd.DataFrame({'load_error':[0.]*30,'spread_error':[0.]*30});r=decide(100,.9,20,hist,c,True);self.assertAlmostEqual(r['adopted_k'],1.2);self.assertAlmostEqual(r['declaration_quantity'],120)
    def test_daily_budget(self):
        a=pd.DataFrame(dict(research_k=[1.2]*24,adopted_k=[1.2]*24,load_hat=[100]*24,tail_loss=[0]*24,declaration_quantity=[120]*24,expected_increment=[10]*24,reason=['']*24),index=pd.date_range('2026-08-01',periods=24,freq='h'));c=dict(self.c['strategy'],max_daily_adjustment_energy=100);r=daily_constraints(a,c);self.assertTrue(r.adopted_k.eq(1).all())
    def test_missing_d2_and_new_customer(self):
        t=pd.DataFrame(dict(entity_id=['new'],valid_time=[pd.Timestamp('2026-08-01')],value=[0.],available_time=[pd.NaT]));tables={k:t.copy() for k in ['portfolio','customers','system_load','day_ahead','real_time']};self.assertTrue(readiness(self.c,tables,pd.Timestamp('2026-08-03')))
    def test_archive_weather_rejected(self):
        from unittest.mock import patch
        with patch('weather.features.dataset_frame',return_value=pd.DataFrame({'valid_time':['2026-08-01'],'mode':['archive']})):
            with self.assertRaisesRegex(ValueError,'实况'):read_weather(self.c,pd.Timestamp('2026-08-01'))
    def test_excel_backup_idempotence_and_complex_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'input.xlsx';wb=Workbook();wb.active.title='实际数据';wb.active.append(['keep',0]);wb.save(p);digest=hashlib.sha256(p.read_bytes()).hexdigest();f=pd.DataFrame({'value':[1.]},index=pd.date_range('2026-08-01',periods=1))
            a=append_record(p,f,'run1',temp);self.assertEqual(a['status'],'appended');self.assertEqual(hashlib.sha256((Path(temp)/a['backup']).read_bytes()).hexdigest(),digest)
            self.assertEqual(append_record(p,f,'run1',temp)['status'],'idempotent');wb=load_workbook(p);self.assertEqual(wb['实际数据']['B1'].value,0);self.assertEqual(wb['预测记录'].max_row,2)
            self.assertEqual(append_record(Path(temp)/'macro.xlsm',f,'run2',temp)['status'],'separate_file')
    def test_config_incremental_rejected(self):
        c=copy.deepcopy(self.c);c['training']['mode']='incremental'
        with self.assertRaisesRegex(ValueError,'不支持增量'):validate(c)
    def test_day_type_six_granularity(self):
        from features.load import day_type_code
        from features.calendar import calendar
        idx=pd.date_range('2026-05-11',periods=112*24,freq='h')  # 周一起112天，含节假日与调休
        code,major=day_type_code(calendar(idx))
        at=lambda day:code.iloc[idx.get_loc(pd.Timestamp(day))]
        self.assertEqual(at('2026-05-11'),0)  # 周一
        self.assertEqual(at('2026-05-13'),1)  # 周三（周二至四）
        self.assertEqual(at('2026-05-15'),2)  # 周五
        self.assertEqual(at('2026-05-16'),3)  # 周六
        s=pd.Series(10.,index=idx);dow=idx.dayofweek
        s[dow==0]=20.;s[dow>=5]=5.  # 周一20、其他工作日10、周末5
        c6=copy.deepcopy(self.c);c6['training']['day_type_granularity']='six'
        cb=copy.deepcopy(self.c);cb['training']['day_type_granularity']='binary'
        b6=baseline(s,c6);bb=baseline(s,cb)
        mon=idx[dow==0][-24*3:]  # 最后三个周一
        m6=b6[mon].mean();mb=bb[mon].mean()
        # 六类：周一硬匹配样本充足不软借，基准≈20；binary混入低值工作日显著更低。
        self.assertGreater(m6,18.);self.assertLess(mb,14.);self.assertGreater(m6-mb,5.)
        # 周末两类均应≈5
        wk=idx[dow>=5][-24*3:]
        self.assertAlmostEqual(b6[wk].mean(),5.,delta=1.);self.assertAlmostEqual(bb[wk].mean(),5.,delta=1.)
    def test_rolling_relative_windows(self):
        from evaluation.rolling_backtest import resolve_windows
        c2=copy.deepcopy(self.c);c2['backtest'].update(window_mode='rolling_relative',test_days=31,validation_days=30,selection_days=30)
        original_test_end=c2['backtest']['test_end']
        r=resolve_windows(c2,pd.Timestamp('2026-10-15'))
        b=r['backtest']
        self.assertEqual(b['test_end'],'2026-10-15');self.assertEqual(b['test_start'],'2026-09-15')
        self.assertEqual(b['validation_end'],'2026-09-14');self.assertEqual(b['validation_start'],'2026-08-16')
        self.assertEqual(b['model_selection_end'],'2026-08-15');self.assertEqual(b['model_selection_start'],'2026-07-17')
        # 原config不被修改（深拷贝）
        self.assertEqual(c2['backtest']['test_end'],original_test_end)
        # fixed模式原样返回
        c3=copy.deepcopy(self.c);c3['backtest']['window_mode']='fixed'
        self.assertIs(resolve_windows(c3,pd.Timestamp('2026-10-15')),c3)
    def test_spike_two_part_features_causal(self):
        from evaluation.rolling_backtest import price_features
        from weather.features import VARS
        days=150;idx=pd.date_range('2026-01-05',periods=days*24,freq='h')
        da=pd.Series(100.,index=idx);rt=pd.Series(95.,index=idx)
        spike_days=[d.normalize() for d in pd.date_range('2026-02-01',periods=days-30,freq='10D')]
        da[idx.normalize().isin(spike_days)]=300.
        tables={'day_ahead':pd.DataFrame({'valid_time':idx,'value':da.to_numpy()}),
                'real_time':pd.DataFrame({'valid_time':idx,'value':rt.to_numpy()}),
                'system_load':pd.DataFrame({'valid_time':idx,'value':np.full(len(idx),80000.)})}
        sy=pd.Series(80000.,index=idx)  # 固定规格系统OOS预测（非拟合实际值）
        wx=pd.DataFrame(25.,index=idx,columns=VARS)
        c2=copy.deepcopy(self.c);c2['training']['spike_two_part']=True;c2['disclosure']={'use_features':False}
        f,x,spikes=price_features(tables,sy,wx,c2,idx[-1])
        self.assertIn('da',spikes)  # 尖峰标签与阈值已生成
        lagh=c2['run']['observation_lag_days']*24
        q=da.shift(lagh).rolling(90*24,min_periods=28*24).quantile(.9)
        q1=spikes['da']['quantile'].dropna();q2=q.dropna()
        common=q1.index.intersection(q2.index)
        self.assertGreater(len(common),2700)  # 150天中约120天有效（扣shift滞后+min_periods28天）
        np.testing.assert_allclose(q1.loc[common],q2.loc[common])  # 阈值只用D-lag及更早观测（因果）
        lab=spikes['da']['label']
        self.assertTrue(set(lab.dropna().unique())<={0.,1.})  # 标签值域
        self.assertTrue(lab.iloc[:28*24].isna().all())  # 历史不足28天时无标签
        # 未来尖峰不得泄入阈值：把最后10天（999）全部改动，end提前15天重算，历史阈值应完全不变
        da2=da.copy();da2.iloc[-240:]=999.
        tables2={**tables,'day_ahead':pd.DataFrame({'valid_time':idx,'value':da2.to_numpy()})}
        f2,x2,sp2=price_features(tables2,sy,wx,c2,idx[-24*15])  # end提前15天，窗口不触及999段
        q1=spikes['da']['quantile'].dropna();q2=sp2['da']['quantile'].dropna()
        common=q1.index.intersection(q2.index)
        np.testing.assert_allclose(q2.loc[common],q1.loc[common])

    def test_excel_chart_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'prediction.xlsx';f=pd.DataFrame({'da_hat':np.arange(24.),'rt_hat':np.arange(24.)+10},index=pd.date_range('2026-09-02',periods=24,freq='h'));write_book(p,{'明日24点总览':f});wb=load_workbook(p)
            self.assertEqual(wb['明日24点总览'].max_row,25);self.assertEqual(len(wb['预测价格图']._charts[0].series),2);self.assertEqual(wb['预测价格图']._charts[0].series[0].tx.v,'日前预测_元每MWh')

if __name__=='__main__':unittest.main(verbosity=2)
