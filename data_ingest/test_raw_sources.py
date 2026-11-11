import unittest,io,datetime,sqlite3,tempfile,copy
from pathlib import Path
from unittest.mock import patch
from openpyxl import Workbook
from data_ingest.raw_sources import clean
from database.raw_store import ensure,commit_raw,system_feature
from database.trading import create_tables

C={'price_range':[-1000,5000],'system_load_range':[0,200000],'disclosure_rows':['统调负荷(MW)']}
TODAY=datetime.date(2026,9,16)
def raw(w):
 b=io.BytesIO();w.save(b);w.close();return b.getvalue()
def retail(bad=False):
 w=Workbook();s=w.active;s.append(['日期','用电企业编码','用电企业名称','合计']+[f'{h}时' for h in range(24)]);s.append(['2026-09-01','A','甲',24 if not bad else 30]+[1]*24);return w
class RawTests(unittest.TestCase):
 def test_retail_total_and_detail(self):
  r=clean('零售用户电量.xlsx',raw(retail()),C,TODAY);self.assertFalse(r['errors']);self.assertEqual(len(r['records']),48)
 def test_retail_bad_total(self):self.assertTrue(clean('零售用户电量.xlsx',raw(retail(True)),C,TODAY)['errors'])
 def test_zero_customer_retained(self):
  w=retail();w.active.append(['2026-09-01','B','乙',0]+[0]*24);r=clean('零售用户电量.xlsx',raw(w),C,TODAY);self.assertFalse(r['errors']);self.assertEqual(sum(x['value']==0 for x in r['records']),24)
 def test_duplicate_customer(self):
  w=retail();w.active.append(['2026-09-01','A','甲',24]+[1]*24);self.assertTrue(clean('零售用户电量.xlsx',raw(w),C,TODAY)['errors'])
 def test_future_actual_rejected(self):
  w=retail();w.active['A2']='2026-09-17';self.assertTrue(clean('零售用户电量.xlsx',raw(w),C,TODAY)['errors'])
 def test_unknown(self):self.assertTrue(clean('unknown.xlsx',raw(retail()),C,TODAY)['errors'])
 def test_price_blocks_and_date_style(self):
  w=Workbook();s=w.active;s.title='25年价格 ';s.append(['日前',None,'1月']);s.append([None,None,None]);s.append([None,'日期',1])
  for h in range(1,25):s.append([None,h,0 if h==1 else -50 if h==2 else 337.89])
  s.cell(5,3).number_format='yyyy-mm-dd'
  for h in range(1,25):s.append(['实时' if h==1 else None,h,100])
  r=clean('广东现货价格.xlsx',raw(w),C,TODAY);self.assertFalse(r['errors'],r['errors']);self.assertEqual(len(r['records']),47);self.assertEqual(next(x['value'] for x in r['records'] if x['kind']=='日前价' and x['hour']==1),-50)
 def test_missing_retail_hour(self):
  w=retail();w.active['E2']=None;self.assertTrue(clean('零售用户电量.xlsx',raw(w),C,TODAY)['errors'])
 def test_bad_dimensions(self):
  import zipfile,re
  b=raw(retail());out=io.BytesIO()
  with zipfile.ZipFile(io.BytesIO(b)) as z,zipfile.ZipFile(out,'w') as target:
   for n in z.namelist():
    v=z.read(n)
    if n.startswith('xl/worksheets/'):v=re.sub(b'<dimension ref="[^"]+"',b'<dimension ref="A1:A1"',v)
    target.writestr(n,v)
  r=clean('零售用户电量.xlsx',out.getvalue(),C,TODAY);self.assertFalse(r['errors']);self.assertEqual(len(r['records']),48)
 def test_zip_empty_days_and_mismatch(self):
  import zipfile
  w=Workbook();s=w.active;s.title='负荷实际信息(2026-09-01)';s.append(['类型','通道名称']+[f'{i//4:02d}:{i%4*15:02d}' for i in range(96)]);s.append(['实际','统调负荷(MW)']+[100]*96)
  b=io.BytesIO()
  with zipfile.ZipFile(b,'w') as z:z.writestr('披露中心-中调实际信息披露(2026-09-02).xlsx',raw(w))
  r=clean('披露中心.zip',b.getvalue(),C,TODAY);self.assertTrue(r['errors'])
 def test_atomicity_and_idempotency(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'master.db';db=sqlite3.connect(p)
   db.executescript('CREATE TABLE runs(id TEXT,kind TEXT,published_at TEXT,model_version TEXT,config_json TEXT,source TEXT,sha TEXT,status TEXT);CREATE TABLE predictions(run_id TEXT,target TEXT,load REAL,da REAL,rt REAL,k REAL,research_k REAL);CREATE TABLE actuals(target TEXT,metric TEXT,value REAL,available_at TEXT,evidence TEXT,source_sha TEXT,ingested_at TEXT,PRIMARY KEY(target,metric,source_sha));')
   create_tables(db);ensure(db);db.commit();db.close()
   b=raw(retail());r=clean('零售用户电量.xlsx',b,C,TODAY)
   with patch('database.raw_store.backup',return_value=p):
    # v1.1起同SHA256批次幂等短路：重跑同字节文件直接跳过（already_imported），不做值级unchanged统计。
    commit_raw(p,'零售用户电量.xlsx',b,r);again=commit_raw(p,'零售用户电量.xlsx',b,r);self.assertEqual(again['status'],'already_imported');self.assertEqual(again['changes'],{})
    db=sqlite3.connect(p);before=db.execute('SELECT sum(value) FROM 负荷').fetchone();db.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON 负荷 WHEN NEW.hour=23 BEGIN SELECT RAISE(ABORT,'test rollback');END");db.commit()
    # 原子性场景须用不同字节的新文件（值+1）绕过同哈希短路，触发更新失败后验证整批回滚。
    w2=Workbook();s2=w2.active;s2.append(['日期','用电企业编码','用电企业名称','合计']+[f'{h}时' for h in range(24)]);s2.append(['2026-09-01','A','甲',48]+[2]*24)
    b2=raw(w2);changed=clean('零售用户电量.xlsx',b2,C,TODAY)
    with self.assertRaises(sqlite3.IntegrityError):commit_raw(p,'零售用户电量.xlsx',b2,changed)
    self.assertEqual(db.execute('SELECT sum(value) FROM 负荷').fetchone(),before);self.assertEqual(db.execute('SELECT value FROM 用户明细 LIMIT 1').fetchone()[0],1);db.close()
 def test_one_bad_file_does_not_block_other_file(self):
  from data_ingest.main import initialize_empty
  from data_ingest.inbox import run
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);c=dict(C,db_path=root/'master.db',inbox_dir=root/'inbox',processed_dir=root/'processed',export_dir=root/'output',log_path=root/'import_log.csv')
   initialize_empty(c,True);c['inbox_dir'].mkdir()
   (c['inbox_dir']/'a_unknown.txt').write_text('invalid')
   (c['inbox_dir']/'z零售用户电量.xlsx').write_bytes(raw(retail()))
   with patch('database.raw_store.backup',return_value=root/'backup.db'):result=run(c)
   self.assertEqual(result['status'],'rejected');self.assertEqual([x['status'] for x in result['files']],['rejected','completed'])
   self.assertTrue((c['inbox_dir']/'a_unknown.txt').exists());self.assertEqual(len(list(c['processed_dir'].iterdir())),1)
   db=sqlite3.connect(c['db_path']);self.assertEqual(db.execute('select count(*) from 负荷').fetchone()[0],24);db.close()
 def test_system_feature_receipt_and_complete_hour(self):
  import pandas as pd
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'master.db';db=sqlite3.connect(p);create_tables(db);ensure(db)
   for hour in range(1,8):db.execute('INSERT INTO 统调负荷 VALUES(?,?,?,?,?,?,?,?,?,?,?)',('2026-09-01',hour,'','',100,'2026-09-02','source','2026-09-02 08:00:00','receipt','sha','cell'))
   db.commit();db.close();idx=pd.date_range('2026-09-03',periods=2,freq='h');a=system_feature(idx,p);self.assertEqual(a.iloc[0],100);self.assertTrue(pd.isna(a.iloc[1]))
   db=sqlite3.connect(p);db.execute("UPDATE 统调负荷 SET available_at='2026-09-03 08:00:00'");db.commit();db.close();self.assertTrue(system_feature(idx,p).isna().all())
if __name__=='__main__':unittest.main()
