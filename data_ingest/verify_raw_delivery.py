"""交付独立核对：源字节、数据库粒度、累计数量及导出数值。只读。"""
import hashlib,json,sqlite3
from pathlib import Path
from openpyxl import load_workbook
R=Path(__file__).resolve().parents[1]
def main():
 d=sqlite3.connect(R/'database/master.db');assert d.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
 expected={'用户明细':4608,'统调负荷':1440,'申报记录':408,'结算价对照':528,'运行用电量对照':264}
 counts={}
 for t,n in expected.items():
  actual=d.execute('SELECT count(*) FROM "'+t+'"').fetchone()[0];assert actual==n,(t,actual,n);counts[t]=actual
 assert not d.execute('SELECT date,hour FROM 用户明细 GROUP BY date,hour HAVING count(*)<>16').fetchall()
 delta=d.execute('SELECT max(abs(a.value-b.v)) FROM 负荷 a JOIN (SELECT date,hour,sum(value) v FROM 用户明细 GROUP BY date,hour)b USING(date,hour)').fetchone()[0];assert delta<1e-9
 assert d.execute('SELECT count(*) FROM 统调负荷 WHERE hour NOT BETWEEN 1 AND 96').fetchone()[0]==0
 batches=[]
 for p in (R/'data_ingest/data/processed').iterdir():
  digest=hashlib.sha256(p.read_bytes()).hexdigest();b=d.execute('SELECT sha256,payload FROM 接入批次 WHERE sha256=?',(digest,)).fetchone();assert b and hashlib.sha256(b[1]).hexdigest()==digest;batches.append(p.name)
 assert len(batches)==4
 book=load_workbook(R/'data_ingest/output/清洗入库数据_20260901_20260917.xlsx',read_only=True,data_only=True)
 for t,n in expected.items():assert book[t].max_row-1==n
 for t in ['负荷','日前价','实时价']:
  for row in list(book[t].values)[1:]:
   for h,v in enumerate(row[1:]):
    sql=d.execute('SELECT value FROM "'+t+'" WHERE date=? AND hour=?',(row[0],h)).fetchone();sql=sql[0] if sql else None
    if t!='负荷' and sql==0:sql=None
    assert (v is None and sql is None) or (v is not None and sql is not None and abs(v-sql)<1e-8),(t,row[0],h,v,sql)
 sheets=book.sheetnames;book.close();d.close()
 report=dict(integrity='ok',facts=counts,retail_sum_max_error=delta,original_files_verified=len(batches),export_sheets=sheets,export_values='passed')
 (R/'data_ingest/output/raw_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
