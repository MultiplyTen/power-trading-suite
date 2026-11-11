# -*- coding: utf-8 -*-
import unittest,tempfile,datetime,sqlite3,sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from openpyxl import Workbook,load_workbook
from data_ingest.main import initialize_empty,import_template
from data_ingest.validation import validate,headers,SHEETS
from data_ingest.importer import commit
from data_ingest.exporter import write_export
from database.trading import open_db,forecast_load,record,prices,market_frame,sync_actuals

class IngestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.c={'db_path':self.root/'master.db','template_path':self.root/'input.xlsx','log_path':self.root/'log.csv','export_dir':self.root,'price_range':[0,5000],'load_unit':'MWh'}
        initialize_empty(self.c,True);self.today=datetime.date(2026,9,16)
    def tearDown(self):self.temp.cleanup()
    def book(self,points=24):
        w=Workbook();w.remove(w.active)
        for s in SHEETS:w.create_sheet(s).append(headers(24 if s=='负荷预测' else points))
        return w
    def save(self,w):w.save(self.c['template_path']);w.close()
    def inspect(self):return validate(self.c['template_path'],self.c,self.today)
    def ingest(self):
        e,c,s=self.inspect();self.assertFalse(e,e);return commit(self.c['db_path'],self.c['template_path'],c,s)
    def values(self,s):
        with open_db(self.c['db_path']) as d:return d.execute('SELECT date,hour,value FROM "'+s+'" ORDER BY date,hour').fetchall()
    def test_blank_keeps_existing_and_partial_sheet_allowed(self):
        w=self.book();w['负荷'].append(['2026-09-01',1,2]);self.save(w);self.ingest()
        w=self.book();w['负荷'].append(['2026-09-01',None,3]);self.save(w);r=self.ingest()
        self.assertEqual([x[2] for x in self.values('负荷')],[1,3]);self.assertEqual(r['sheets']['负荷']['updated'],1)
    def test_quarter_energy_sum_and_price_mean(self):
        w=self.book(96);w['负荷'].append(['2026-09-01',1,2,3,4]);w['日前价'].append(['2026-09-01',10,20,30,40]);self.save(w);self.ingest()
        self.assertEqual(self.values('负荷')[0][2],10);self.assertEqual(self.values('日前价')[0][2],25)
    def test_kwh_conversion(self):
        self.c['load_unit']='kWh';w=self.book(96);w['负荷'].append(['2026-09-01',250,250,250,250]);self.save(w);self.ingest();self.assertEqual(self.values('负荷')[0][2],1)
    def test_incomplete_quarter_hour_rejected(self):
        w=self.book(96);w['负荷'].append(['2026-09-01',1,None,1,1]);self.save(w);e,_,_=self.inspect();self.assertIn('填齐4个',e[0]['message']);self.assertEqual(self.values('负荷'),[])
    def test_all_errors_have_locations(self):
        w=self.book();w['负荷'].append(['2026-09-32',-1,'错']);w['日前价'].append(['2026-09-17',5001]);w['实时价']['B1']='被改';self.save(w)
        e,_,_=self.inspect();self.assertGreaterEqual(len(e),6);self.assertTrue(all(x['row']>=1 and x['column']>=1 for x in e))
    def test_duplicate_dates_even_disjoint_hours_rejected(self):
        w=self.book();w['负荷'].append(['2026-09-01',1]);w['负荷'].append(['2026-09-01',None,2]);self.save(w);self.assertTrue(any('重复' in x['message'] for x in self.inspect()[0]))
    def test_manual_forecast_future_allowed(self):
        w=self.book();w['负荷预测'].append(['2026-09-17',*range(1,25)]);self.save(w);self.ingest();s,source=forecast_load(pd.Timestamp('2026-09-17'),self.c['db_path']);self.assertEqual(len(s),24);self.assertIn('手填',source['source'])
    def test_forecast_requires_24_complete_hours(self):
        w=self.book();w['负荷预测'].append(['2026-09-17',1]);self.save(w);self.ingest()
        with self.assertRaises(ValueError):forecast_load(pd.Timestamp('2026-09-17'),self.c['db_path'])
    def test_reimport_preserves_receipt_and_versions(self):
        w=self.book();w['负荷'].append(['2026-09-01',1]);self.save(w);self.ingest()
        with open_db(self.c['db_path']) as d:before=d.execute('SELECT updated_at FROM 负荷').fetchone();n=d.execute('SELECT count(*) FROM 接入修订').fetchone()[0]
        result=self.ingest()
        with open_db(self.c['db_path']) as d:self.assertEqual(before,d.execute('SELECT updated_at FROM 负荷').fetchone());self.assertEqual(n,d.execute('SELECT count(*) FROM 接入修订').fetchone()[0])
        self.assertEqual(result['sheets']['负荷']['unchanged'],1)
    def test_failure_in_last_sheet_rolls_back_all(self):
        w=self.book();w['负荷'].append(['2026-09-01',1]);w['实时价'].append(['2026-09-01',10]);self.save(w)
        with open_db(self.c['db_path']) as d:d.execute("CREATE TRIGGER fail_price BEFORE INSERT ON 实时价 BEGIN SELECT RAISE(ABORT,'模拟磁盘写入失败'); END;")
        with self.assertRaises(sqlite3.IntegrityError):self.ingest()
        self.assertEqual(self.values('负荷'),[])
        with open_db(self.c['db_path']) as d:self.assertEqual(d.execute('SELECT count(*) FROM 接入修订').fetchone()[0],0)
    def test_export_range_and_roundtrip(self):
        w=self.book();w['负荷'].append(['2026-09-01',1]);w['负荷'].append(['2026-09-02',2]);self.save(w);self.ingest()
        out=self.root/'export.xlsx';write_export(self.c['db_path'],out,include_extra=False,start='2026-09-02',end='2026-09-02')
        with_sql=self.values('负荷');e,c,s=validate(out,self.c,self.today);self.assertFalse(e);self.assertEqual(len(c),1);commit(self.c['db_path'],out,c,s);self.assertEqual(with_sql,self.values('负荷'))
        book=load_workbook(out);self.assertEqual(book.sheetnames,SHEETS);self.assertEqual(book['负荷'].max_row,2);book.close()
    def test_model_records_precede_manual_fallback(self):
        w=self.book();w['负荷预测'].append(['2026-09-17',*([1]*24)]);self.save(w);self.ingest()
        f=pd.DataFrame({'load':[2.]*24},index=pd.date_range('2026-09-17',periods=24,freq='h'));rid=record('load',f,{'x':1},self.c['db_path'])
        s,source=forecast_load(pd.Timestamp('2026-09-17'),self.c['db_path']);self.assertTrue(s.eq(2).all());self.assertEqual(source['run_id'],rid)
        s,_=forecast_load(pd.Timestamp('2026-09-17'),self.c['db_path'],manual_only=True);self.assertTrue(s.eq(1).all())
    def test_missing_db_not_created_implicitly(self):
        p=self.root/'absent.db'
        with self.assertRaises(ValueError):open_db(p)
        self.assertFalse(p.exists())
    def test_corrupt_db_preserved(self):
        p=self.root/'bad.db';p.write_bytes(b'bad')
        with self.assertRaises(Exception):open_db(p)
        self.assertEqual(p.read_bytes(),b'bad')
    def test_initialization_requires_confirmation(self):
        self.c['db_path']=self.root/'new.db'
        with self.assertRaises(ValueError):initialize_empty(self.c,False)
        self.assertFalse(self.c['db_path'].exists())
    def test_existing_db_cannot_be_reinitialized(self):
        with self.assertRaises(ValueError):initialize_empty(self.c,True)
    def test_formula_and_zero_load_rejected(self):
        w=self.book();w['负荷'].append(['2026-09-01',0,'=1+2']);self.save(w);self.assertEqual(len(self.inspect()[0]),2)
    def test_negative_price_requires_explicit_range(self):
        w=self.book();w['日前价'].append(['2026-09-01',-10]);self.save(w);self.assertTrue(self.inspect()[0]);self.c['price_range']=[-100,5000];self.assertFalse(self.inspect()[0])
    def test_invalid_batch_logs_all_sheets_without_data_change(self):
        w=self.book();w['负荷'].append(['2026-09-01',-1]);w['日前价'].append(['2026-09-01',100]);self.save(w)
        result=import_template(self.c);self.assertEqual(result['status'],'rejected');self.assertEqual(self.values('日前价'),[]);self.assertTrue(self.c['log_path'].exists())
    def test_absent_template_is_created_without_import(self):
        result=import_template(self.c);self.assertEqual(result['status'],'template_created');self.assertTrue(self.c['template_path'].exists());self.assertEqual(self.values('负荷'),[])
    def test_new_values_immediately_visible_to_model_readers(self):
        w=self.book();w['负荷'].append(['2026-09-01',10]);w['日前价'].append(['2026-09-01',123]);w['实时价'].append(['2026-09-01',234]);self.save(w);self.ingest()
        f,_=prices(self.c['db_path']);self.assertEqual(f.iloc[0].da,123);self.assertEqual(f.iloc[0].rt,234)
        self.assertEqual(market_frame('负荷','portfolio','MWh',self.c['db_path']).iloc[0].value,10)
        w['负荷']['B2']=11;self.save(w);self.ingest()
        with open_db(self.c['db_path']) as d:
            rows=d.execute("SELECT value,available_at FROM actuals WHERE metric='load'").fetchall();self.assertEqual(len(rows),2);self.assertTrue(all(pd.Timestamp(t)>pd.Timestamp('2026-09-01') for _,t in rows))
    def test_review_reconciles_without_reading_excel(self):
        from unittest.mock import patch
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'review'))
        from reconciliation import reconcile
        w=self.book();w['负荷'].append(['2026-09-01',*([1]*24)]);self.save(w);self.ingest()
        f=pd.DataFrame({'load':[1.]*24},index=pd.date_range('2026-09-01',periods=24,freq='h'));record('load',f,{},self.c['db_path'])
        with patch('pandas.read_excel',side_effect=AssertionError('不应读Excel')),open_db(self.c['db_path']) as d:
            sync_actuals(d);done,_,_=reconcile(d,{'review_date':str(datetime.date.today()),'actual_lag_days':5});self.assertEqual(len(done),24)
    def test_manual_forecast_96_columns_rejected(self):
        w=self.book();w.remove(w['负荷预测']);w.create_sheet('负荷预测').append(headers(96));self.save(w);self.assertTrue(self.inspect()[0])

if __name__=='__main__':unittest.main(verbosity=2)
